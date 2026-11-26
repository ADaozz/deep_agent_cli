"""Pre-merge security boundary & interaction hardening regressions (Items 1-7)."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import threading
import time

from cryptography.fernet import Fernet
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
import pytest

from agent.cli.app import CliApplication
from agent.cli.interactions import InteractionController
from agent.cli.rendering import render_interaction
from agent.execution.backend import BubblewrapBackend, select_backend
from agent.execution.ownership import (
    reset_execution_thread_id,
    reset_root_session_id,
    set_execution_thread_id,
    set_root_session_id,
)
from agent.execution.policy import SandboxConfig
from agent.execution.process import _OutputCollector
from agent.mcp.adapter import LoadedServer, build_transport, mcp_secret_consumer
from agent.mcp.config import MCPAuth, MCPAuthError, MCPServerConfig
from agent.mcp.contracts import ServerStatus
from agent.mcp.manager import MCPManager
from agent.redaction import SecretRef
from agent.runtime.runner import AgentRunner
from agent.secrets import SecretStore
from tests.conftest import scripted_model


def _require_bwrap(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    try:
        sel = select_backend(SandboxConfig(workspace=tmp_path, max_output_bytes=4096))
    except Exception as exc:
        if os.environ.get("REQUIRE_BWRAP_TEST") == "1":
            raise
        pytest.skip(f"bubblewrap unavailable: {exc}")
    return sel.backend


# ---------------------------------------------------------------------------
# Item 1: Atomic cross-session sandbox isolation + surviving background children
# ---------------------------------------------------------------------------


def test_concurrent_cross_session_sandbox_launches_cannot_both_pass_isolation(tmp_path: Path) -> None:
    backend = _require_bwrap(tmp_path)
    executor: BubblewrapBackend = backend.executor
    store = SecretStore(tmp_path / "cfg", environ={})
    store.set_secret("RACE_TOKEN", "race_secret_val_12345", scope="sandbox", storage="session", session_id="sess-a")
    backend.set_secret_provider(
        store.sandbox_env_for_session,
        lambda: store.redaction_refs(),
        (store.acquire_redaction_hold, store.release_redaction_hold),
    )

    try:
        # Warm the offline persistent Worker so both threads race on the exact same PersistentSandbox.
        t0 = set_execution_thread_id("sess-a")
        r0 = set_root_session_id("sess-a")
        try:
            warm = backend.execute("true", timeout=10)
            assert warm.exit_code == 0
        finally:
            reset_root_session_id(r0)
            reset_execution_thread_id(t0)

        barrier = threading.Barrier(2)

        def run_as(session_id: str):
            t_tok = set_execution_thread_id(session_id)
            r_tok = set_root_session_id(session_id)
            try:
                barrier.wait(timeout=5)
                return backend.execute("sleep 0.6; echo DONE", timeout=10)
            finally:
                reset_root_session_id(r_tok)
                reset_execution_thread_id(t_tok)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(run_as, "sess-a")
            fut_b = pool.submit(run_as, "sess-b")
            res_a = fut_a.result(timeout=15)
            res_b = fut_b.result(timeout=15)

        # Atomic launch boundary guarantees at most one succeeds while the other is rejected!
        succeeded = [r for r in (res_a, res_b) if r.exit_code == 0]
        rejected = [r for r in (res_a, res_b) if r.exit_code != 0]
        assert len(succeeded) == 1
        assert len(rejected) == 1
        assert "/proc boundary" in rejected[0].output
    finally:
        executor.pool.shutdown("test finished")


def test_surviving_background_subprocess_blocks_cross_session_sharing_both_directions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Direction 1: Session A with secrets spawns a detached `nohup sleep 20 &` and its shell exits;
    # ExecutionRegistry has ZERO active sessions, yet Session B MUST still be rejected!
    backend1 = _require_bwrap(tmp_path / "d1")
    store1 = SecretStore(tmp_path / "cfg1", environ={})
    store1.set_secret("BG_SECRET", "bg_orphan_secret_999", scope="sandbox", storage="session", session_id="sess-a")
    backend1.set_secret_provider(store1.sandbox_env_for_session, lambda: store1.redaction_refs())
    try:
        ta = set_execution_thread_id("sess-a")
        ra = set_root_session_id("sess-a")
        try:
            launched = backend1.execute("nohup sleep 20 >/dev/null 2>&1 &", timeout=10)
            assert launched.exit_code == 0
            assert not launched.running
            # Confirm ExecutionRegistry reports zero active sessions for sess-a!
            assert backend1.list_active_executions("sess-a") == []
        finally:
            reset_root_session_id(ra)
            reset_execution_thread_id(ta)

        tb = set_execution_thread_id("sess-b")
        rb = set_root_session_id("sess-b")
        try:
            blocked = backend1.execute("echo should-not-run", timeout=10)
            assert blocked.exit_code != 0
            assert "surviving background" in blocked.output and "/proc boundary" in blocked.output
        finally:
            reset_root_session_id(rb)
            reset_execution_thread_id(tb)
    finally:
        backend1.executor.pool.shutdown("dir1 finished")

    # Direction 2: Session B WITHOUT secrets leaves a surviving `nohup sleep 20 &` and exits;
    # Session A WITH secrets MUST refuse injecting secrets into that shared Worker!
    backend2 = _require_bwrap(tmp_path / "d2")
    store2 = SecretStore(tmp_path / "cfg2", environ={})
    store2.set_secret("BG_SECRET_2", "bg_orphan_secret_888", scope="sandbox", storage="session", session_id="sess-a")
    backend2.set_secret_provider(store2.sandbox_env_for_session, lambda: store2.redaction_refs())
    try:
        tb = set_execution_thread_id("sess-b")
        rb = set_root_session_id("sess-b")
        try:
            spy = backend2.execute("nohup sleep 20 >/dev/null 2>&1 &", timeout=10)
            assert spy.exit_code == 0
        finally:
            reset_root_session_id(rb)
            reset_execution_thread_id(tb)

        ta = set_execution_thread_id("sess-a")
        ra = set_root_session_id("sess-a")
        try:
            blocked = backend2.execute("echo secret-must-not-inject", timeout=10)
            assert blocked.exit_code != 0
            assert "surviving background" in blocked.output and "/proc boundary" in blocked.output
        finally:
            reset_root_session_id(ra)
            reset_execution_thread_id(ta)
    finally:
        backend2.executor.pool.shutdown("dir2 finished")

    # Direction 3: Fail-closed when PID namespace idle verification cannot confirm cleanliness.
    # Leave a secret-bearing orphan so taint remains after CommandSession end, then force
    # verify_pid_namespace_idle to fail closed for the cross-session launch.
    backend3 = _require_bwrap(tmp_path / "d3")
    store3 = SecretStore(tmp_path / "cfg3", environ={})
    store3.set_secret("FC_SECRET", "fc_secret_777", scope="sandbox", storage="session", session_id="sess-a")
    backend3.set_secret_provider(store3.sandbox_env_for_session, lambda: store3.redaction_refs())
    try:
        ta = set_execution_thread_id("sess-a")
        ra = set_root_session_id("sess-a")
        try:
            assert backend3.execute("nohup sleep 20 >/dev/null 2>&1 &", timeout=10).exit_code == 0
        finally:
            reset_root_session_id(ra)
            reset_execution_thread_id(ta)

        sandbox = backend3.executor.pool.sandbox(False)
        assert sandbox is not None
        assert "sess-a" in sandbox._secret_tainted_sessions
        monkeypatch.setattr(sandbox, "verify_pid_namespace_idle", lambda **kwargs: False)

        tb = set_execution_thread_id("sess-b")
        rb = set_root_session_id("sess-b")
        try:
            blocked = backend3.execute("echo fail-closed", timeout=10)
            assert blocked.exit_code != 0
            assert "/proc boundary" in blocked.output
        finally:
            reset_root_session_id(rb)
            reset_execution_thread_id(tb)
    finally:
        backend3.executor.pool.shutdown("dir3 finished")


# ---------------------------------------------------------------------------
# Item 2: MCP stdio argv fingerprint binding, remove revocation & reload
# ---------------------------------------------------------------------------


def test_mcp_stdio_argv_change_remove_readd_and_reload_revoke_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SecretStore(tmp_path, environ={})
    store.set_secret("STDIO_DB_PASS", "super_db_password_42", scope="host", storage="persistent")

    safe_srv = MCPServerConfig(
        name="db",
        transport="stdio",
        command="python3",
        args=("safe_server.py",),
        env_passthrough=("STDIO_DB_PASS",),
    )
    tampered_argv_srv = MCPServerConfig(
        name="db",
        transport="stdio",
        command="python3",
        args=("-c", "import os; print(os.environ['STDIO_DB_PASS'])"),
        env_passthrough=("STDIO_DB_PASS",),
    )
    assert mcp_secret_consumer(safe_srv) != mcp_secret_consumer(tampered_argv_srv)

    manager = MCPManager(tmp_path / "mcp.json", secret_store=store)
    seen_env: list[dict[str, str]] = []

    class _Client:
        def is_connected(self) -> bool:
            return True

        async def close(self) -> None:
            pass

    def fake_open(server, bridge, *, secret_store=None, **kwargs):
        refs: list[SecretRef] = []
        tr = build_transport(server, refs, oauth_callback_timeout=5, secret_store=secret_store)
        seen_env.append(dict(tr.env or {}))
        return LoadedServer(_Client(), [], [])

    monkeypatch.setattr("agent.mcp.manager.open_server", fake_open)
    try:
        # 1) User /mcp add authorizes exact safe_srv fingerprint
        res = manager.add(safe_srv, authorize_secrets=True)
        assert res.ok
        assert seen_env[-1]["STDIO_DB_PASS"] == "super_db_password_42"

        # 2) Same name & same command ("python3") with different argv MUST be denied!
        with pytest.raises(MCPAuthError, match="not authorized"):
            build_transport(tampered_argv_srv, [], oauth_callback_timeout=5, secret_store=store)

        # 3) Removing server revokes its authorization in SecretStore
        removed = manager.remove("db")
        assert removed.ok
        meta = store.get_metadata("STDIO_DB_PASS")
        assert meta is not None and mcp_secret_consumer(safe_srv) not in meta.consumers

        # 4) Unapproved re-add (even with identical safe_srv OR tampered argv) MUST be denied!
        readd_tampered = manager.add(tampered_argv_srv, authorize_secrets=False)
        assert not readd_tampered.ok
        assert manager.get("db").status is ServerStatus.AUTH_REQUIRED
        manager.remove("db")

        readd_same = manager.add(safe_srv, authorize_secrets=False)
        assert not readd_same.ok
        assert manager.get("db").status is ServerStatus.AUTH_REQUIRED
        manager.remove("db")

        # 5) Re-authorize safe_srv, then mutate mcp.json argv on disk and reload -> old auth revoked, new denied!
        assert manager.add(safe_srv, authorize_secrets=True).ok
        data = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
        data["servers"]["db"]["args"] = ["-c", "print('tampered')"]
        (tmp_path / "mcp.json").write_text(json.dumps(data), encoding="utf-8")
        manager.load()
        meta_after_reload = store.get_metadata("STDIO_DB_PASS")
        assert meta_after_reload is not None and mcp_secret_consumer(safe_srv) not in meta_after_reload.consumers
        assert not manager.test("db").ok
        assert manager.get("db").status is ServerStatus.AUTH_REQUIRED
    finally:
        manager.close()


# ---------------------------------------------------------------------------
# Items 3 & 6: Pinned redaction on store read errors, zero per-chunk decrypt,
#              and retired secret ref lifecycle reclamation
# ---------------------------------------------------------------------------


def test_execution_redaction_survives_store_corruption_and_avoids_per_chunk_decrypt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _require_bwrap(tmp_path)
    store = SecretStore(tmp_path / "cfg", environ={})
    secret_val = "pers_pinned_secret_value_554433"
    store.set_secret("PINNED_KEY", secret_val, scope="sandbox", storage="persistent")
    backend.set_secret_provider(
        store.sandbox_env_for_session,
        lambda: store.redaction_refs(session_id="sess-pin"),
        (store.acquire_redaction_hold, store.release_redaction_hold),
    )

    # 1) Verify zero Fernet.decrypt calls per streaming output chunk once loaded!
    decrypt_calls = {"count": 0}
    orig_decrypt = Fernet.decrypt

    def counting_decrypt(self_fernet, token, ttl=None):
        decrypt_calls["count"] += 1
        return orig_decrypt(self_fernet, token, ttl=ttl)

    # Warm cache once
    assert any(r.value == secret_val for r in store.redaction_refs(session_id="sess-pin"))
    monkeypatch.setattr(Fernet, "decrypt", counting_decrypt)
    collector = _OutputCollector(
        tool_call_id="tc-perf",
        max_output_bytes=4096,
        workspace_root=tmp_path,
        redaction_provider=lambda: store.redaction_refs(session_id="sess-pin"),
    )
    try:
        for i in range(50):
            collector.append(f"chunk-{i}:{secret_val}\n".encode("utf-8"))
        collector.flush()
    finally:
        collector.close()
    assert decrypt_calls["count"] == 0
    assert secret_val not in collector.tail_text()
    assert "$PINNED_KEY" in collector.tail_text()
    monkeypatch.setattr(Fernet, "decrypt", orig_decrypt)

    # 2) Real bwrap process: corrupt disk entry AND make redaction_provider raise while running ->
    #    injected secret is STILL redacted via start-time pinned refs, and retired refs reclaim after exit!
    t_tok = set_execution_thread_id("sess-pin")
    r_tok = set_root_session_id("sess-pin")
    try:
        bg = backend.execute(
            "read go; python3 -u -c 'import os, sys; v = os.environ[\"PINNED_KEY\"]; "
            "sys.stdout.write(\"OUT:\" + v[:10]); sys.stdout.flush(); "
            "sys.stdout.write(v[10:] + \"\\n\"); sys.stdout.flush()'",
            stdin_mode="pipe",
            yield_time=0.2,
            timeout=15,
        )
        assert bg.running and bg.execution_id

        # While process is running: rotate + delete + corrupt disk entry + verify hold blocks premature prune!
        store.set_secret("PINNED_KEY", "rotated_val_112233", scope="sandbox", storage="persistent", overwrite=True)
        store.delete_secret("PINNED_KEY")
        assert store.retired_count >= 2
        assert store.prune_retired() == 0  # Hold is active -> MUST NOT drop retired refs yet!

        # Corrupt entries dir / break dynamic provider completely while process is running
        (tmp_path / "cfg" / "secrets" / "entries" / "PINNED_KEY.enc").write_bytes(b"corrupted-ciphertext")
        monkeypatch.setattr(store, "redaction_refs", lambda **kwargs: (_ for _ in ()).throw(OSError("disk dead")))

        done = backend.write_stdin(bg.execution_id, chars="go\n", yield_time=3.0)
        assert not done.running
        assert secret_val not in done.output
        assert "OUT:$PINNED_KEY" in done.output

        # No orphans: deferred hold reclaims via sandbox idle check after pump exit.
        deadline = time.monotonic() + 3.0
        while store.retired_count > 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert store.retired_count == 0
    finally:
        reset_root_session_id(r_tok)
        reset_execution_thread_id(t_tok)
        backend.executor.pool.shutdown("pin test finished")


def test_sandbox_injection_fail_closed_while_redaction_keeps_last_known(tmp_path: Path) -> None:
    """Injection uses fresh verified reads; redaction may keep last-known cache."""
    store = SecretStore(tmp_path / "cfg", environ={})
    secret_val = "inject_fail_closed_secret_aabbcc"
    store.set_secret("INJECT_KEY", secret_val, scope="sandbox", storage="persistent")

    # Warm fail-open cache used by redaction.
    assert any(r.value == secret_val for r in store.redaction_refs())
    assert store.sandbox_env_for_session("sess-inject")["INJECT_KEY"] == secret_val

    # Corrupt on-disk entry (auth/value no longer verifiable).
    entry = tmp_path / "cfg" / "secrets" / "entries" / "INJECT_KEY.enc"
    entry.write_bytes(b"corrupted-ciphertext-not-fernet")

    # Redaction still masks via last-known cache.
    assert any(r.value == secret_val for r in store.redaction_refs())
    # New-process injection must NOT reuse the expired cache.
    assert store.sandbox_env_for_session("sess-inject") == {}

    backend = _require_bwrap(tmp_path / "inj")
    backend.set_secret_provider(
        store.sandbox_env_for_session,
        lambda: store.redaction_refs(session_id="sess-inject"),
        (store.acquire_redaction_hold, store.release_redaction_hold),
    )
    t_tok = set_execution_thread_id("sess-inject")
    r_tok = set_root_session_id("sess-inject")
    try:
        result = backend.execute(
            'python3 -c "import os; print(os.environ.get(\'INJECT_KEY\', \'MISSING\'))"',
            timeout=10,
        )
        assert result.exit_code == 0
        assert "MISSING" in result.output
        assert secret_val not in result.output
    finally:
        reset_root_session_id(r_tok)
        reset_execution_thread_id(t_tok)
        backend.executor.pool.shutdown("inject fail-closed finished")


def test_retired_redaction_hold_waits_for_orphan_idle_or_sandbox_stop(tmp_path: Path) -> None:
    """Secret-bearing nohup orphans keep redaction holds until idle/taint clear or stop."""
    backend = _require_bwrap(tmp_path)
    store = SecretStore(tmp_path / "cfg", environ={})
    secret_val = "orphan_hold_secret_778899"
    store.set_secret("ORPHAN_HOLD", secret_val, scope="sandbox", storage="persistent")
    backend.set_secret_provider(
        store.sandbox_env_for_session,
        lambda: store.redaction_refs(),
        (store.acquire_redaction_hold, store.release_redaction_hold),
    )

    t_tok = set_execution_thread_id("sess-orphan-hold")
    r_tok = set_root_session_id("sess-orphan-hold")
    try:
        launched = backend.execute("nohup sleep 30 >/dev/null 2>&1 &", timeout=10)
        assert launched.exit_code == 0
        assert not launched.running
        assert backend.list_active_executions("sess-orphan-hold") == []

        store.set_secret(
            "ORPHAN_HOLD", "rotated_orphan_hold_001", scope="sandbox", storage="persistent", overwrite=True,
        )
        store.delete_secret("ORPHAN_HOLD")
        assert store.retired_count >= 1
        # Shell ended, but orphan still holds env — prune must stay blocked.
        time.sleep(0.15)
        assert store.prune_retired() == 0
        assert store.retired_count >= 1

        # Namespace teardown releases deferred holds (orphans die with the sandbox).
        backend.executor.pool.shutdown("orphan hold test finished")
        deadline = time.monotonic() + 3.0
        while store.retired_count > 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert store.retired_count == 0
    finally:
        reset_root_session_id(r_tok)
        reset_execution_thread_id(t_tok)
        try:
            backend.executor.pool.shutdown("orphan hold cleanup")
        except Exception:  # noqa: BLE001
            pass


def test_sandbox_lost_does_not_release_redaction_holds_until_destroy_confirmed(
    tmp_path: Path,
) -> None:
    """lost_reason / control-channel loss must not clear secret taint or retired refs."""
    backend = _require_bwrap(tmp_path)
    store = SecretStore(tmp_path / "cfg", environ={})
    secret_val = "lost_hold_secret_445566"
    store.set_secret("LOST_HOLD", secret_val, scope="sandbox", storage="persistent")
    backend.set_secret_provider(
        store.sandbox_env_for_session,
        lambda: store.redaction_refs(),
        (store.acquire_redaction_hold, store.release_redaction_hold),
    )

    t_tok = set_execution_thread_id("sess-lost-hold")
    r_tok = set_root_session_id("sess-lost-hold")
    try:
        launched = backend.execute("nohup sleep 30 >/dev/null 2>&1 &", timeout=10)
        assert launched.exit_code == 0
        store.set_secret(
            "LOST_HOLD", "rotated_lost_hold_002", scope="sandbox", storage="persistent", overwrite=True,
        )
        store.delete_secret("LOST_HOLD")
        assert store.retired_count >= 1
        time.sleep(0.15)
        assert store.prune_retired() == 0

        sandbox = backend.executor.pool.sandbox(False)
        assert sandbox is not None
        assert "sess-lost-hold" in sandbox._secret_tainted_sessions
        assert sandbox._deferred_redaction_releases

        # Simulate control-channel loss: host can no longer manage, but orphans may remain.
        sandbox._mark_lost("simulated control channel closed")
        assert sandbox._destroy_confirmed is False
        assert sandbox.try_reclaim_secret_taint() is False
        assert store.prune_retired() == 0
        assert store.retired_count >= 1
        assert sandbox._secret_tainted_sessions
        assert sandbox._deferred_redaction_releases
        lost_releases: list[str] = []
        sandbox.defer_redaction_release(lambda: lost_releases.append("lost"))
        assert lost_releases == []
        assert sandbox._deferred_redaction_releases

        # Confirmed destroy (wait for sandbox process exit) may release holds.
        sandbox.stop("lost-hold destroy confirmed")
        deadline = time.monotonic() + 3.0
        while store.retired_count > 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert store.retired_count == 0
        assert not sandbox._secret_tainted_sessions
        assert not sandbox._deferred_redaction_releases
        assert sandbox._destroy_confirmed is True
        assert lost_releases == ["lost"]
    finally:
        reset_root_session_id(r_tok)
        reset_execution_thread_id(t_tok)
        try:
            backend.executor.pool.shutdown("lost-hold cleanup")
        except Exception:  # noqa: BLE001
            pass


def test_late_redaction_release_after_confirmed_destroy_runs_exactly_once(
    tmp_path: Path,
) -> None:
    """A release deferred only after confirmed destroy still executes (once), not forever."""
    backend = _require_bwrap(tmp_path)
    store = SecretStore(tmp_path / "cfg", environ={})
    store.acquire_redaction_hold()
    store.set_secret(
        "LATE_HOLD", "late_hold_secret_135790", scope="sandbox", storage="persistent",
    )
    try:
        launched = backend.execute("true", timeout=10)
        assert launched.exit_code == 0
        store.set_secret(
            "LATE_HOLD", "rotated_late_hold_246801",
            scope="sandbox", storage="persistent", overwrite=True,
        )
        store.delete_secret("LATE_HOLD")
        assert store.retired_count >= 1
        assert store.prune_retired() == 0

        sandbox = backend.executor.pool.sandbox(False)
        assert sandbox is not None
        sandbox.stop("late-hold destroy confirmed")
        assert sandbox._destroy_confirmed is True
        assert sandbox.process is not None and sandbox.process.poll() is not None

        # CommandSession finishes after stop() returned; its deferred release must
        # reuse the confirmed-destroy result instead of waiting forever.
        releases: list[int] = []

        def release_once() -> None:
            releases.append(store.release_redaction_hold())

        sandbox.defer_redaction_release(release_once)
        assert releases and releases[0] >= 1
        assert store.retired_count == 0
        assert not sandbox._deferred_redaction_releases

        later: list[str] = []
        sandbox.defer_redaction_release(lambda: later.append("later"))
        assert later == ["later"]
        assert len(releases) == 1

        # Re-confirming destroy must not replay either already-run release.
        sandbox.stop("late-hold destroy repeated")
        assert len(releases) == 1
        assert later == ["later"]
    finally:
        try:
            backend.executor.pool.shutdown("late-hold cleanup")
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Item 4: Unified Human Input host storage guidance
# ---------------------------------------------------------------------------


def test_human_input_host_secret_clearly_states_persistent_requirement_for_global_mcp() -> None:
    payload = {
        "interactionId": "mcp-hint",
        "title": "MCP Token",
        "question": "Provide GitHub MCP token",
        "fields": [{
            "id": "tok",
            "type": "secret",
            "label": "GitHub Token",
            "required": True,
            "secret_name": "GITHUB_TOKEN",
            "secret_scope": "host",
        }],
    }
    # 1) Without trusted MCP consumer match: do not blindly force persistence from host scope or model text;
    #    show single masked input first, then separate concise storage step stating global MCP requires encrypted storage.
    unbound = InteractionController.human(payload)
    masked_view = render_interaction(unbound, 120)
    assert "Choose storage in the next step" in masked_view
    assert "This session only" not in masked_view
    assert unbound.accept_secret("ghp_demo") is False
    choice_view = render_interaction(unbound, 120)
    assert "Global MCP requires encrypted storage on this device." in choice_view
    assert "This session only" in choice_view
    assert "Store encrypted on this device" in choice_view

    # 2) With trusted global MCP consumer match: disclose encrypted persistence before submit,
    #    never claim global MCP supports session storage, and require explicit confirmation (default Cancel).
    bound = InteractionController.human(payload, secret_consumers={"GITHUB_TOKEN": ("github",)})
    bound_masked = render_interaction(bound, 120)
    assert "Stored encrypted on this device for future sessions (GITHUB_TOKEN · MCP: github)." in bound_masked
    assert "Available only for this session" not in bound_masked
    assert bound.accept_secret("ghp_demo") is False
    confirm_view = render_interaction(bound, 120)
    assert "Confirm encrypted storage" in confirm_view
    assert "Cancel" in confirm_view
    assert "Store encrypted on this device" in confirm_view
    # Default Cancel prevents model from bypassing user confirmation to persist.
    assert bound.accept() is True
    assert bound.persistence_declined is True
    assert bound.secret_submissions() == {}


# ---------------------------------------------------------------------------
# Item 5: Non-blocking, FIFO-ordered async /stdin in TUI + cancel + exit
# ---------------------------------------------------------------------------


def test_tui_stdin_non_blocking_ordered_cancel_and_process_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        thread_id="stdin-async-t",
    )
    ack_gate = threading.Event()
    first_entered = threading.Event()
    received: list[tuple[str, str, str]] = []

    def stub_send(execution_id: str, *, chars: str = "", action: str = "write", write_timeout: float = 15.0) -> None:
        if action == "write" and chars == "line1\n":
            first_entered.set()
            ack_gate.wait(timeout=5.0)
        received.append((execution_id, action, chars))

    monkeypatch.setattr(runner, "send_execution_input", stub_send)

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app.attach_process_input("exec-1", "pipe")

            # 1) Rapid submissions return immediately (do NOT block event loop even while line1 awaits ACK!)
            t0 = time.monotonic()
            for line in ("line1", "line2", "line3"):
                app.buffer.text = line
                app.run_dispatch._submit_buffer("steer")
            elapsed = time.monotonic() - t0
            assert elapsed < 0.2

            # Event loop remains responsive while line1 is blocked waiting for ACK
            await asyncio.sleep(0.05)
            assert first_entered.is_set()
            assert received == []  # line1 still blocked, line2/line3 strictly queued behind it!

            # Release ACK gate and drain -> strict FIFO delivery!
            ack_gate.set()
            await app.flush_process_input()
            assert received == [
                ("exec-1", "write", "line1\n"),
                ("exec-1", "write", "line2\n"),
                ("exec-1", "write", "line3\n"),
            ]

            # 2) Cancel while a write ACK is stalled dispatches immediately and drops queued unsent writes!
            received.clear()
            ack_gate.clear()
            first_entered.clear()
            app.buffer.text = "line1"
            app.run_dispatch._submit_buffer("steer")
            app.buffer.text = "unsent-line2"
            app.run_dispatch._submit_buffer("steer")
            await asyncio.sleep(0.05)
            assert first_entered.is_set()

            # Trigger cancel (Ctrl+C on PIPE) while line1 is stalled -> cancel runs without waiting for ack_gate!
            assert app.send_attached_input(action="cancel") is True
            await asyncio.sleep(0.08)
            assert ("exec-1", "cancel", "") in received
            # Release gate so background worker finishes cleanly; unsent-line2 MUST have been dropped!
            ack_gate.set()
            await app.flush_process_input()
            assert all(chars != "unsent-line2\n" for _, _, chars in received)

            # 3) Process exit (KeyError) auto-detaches and drops remaining queued writes!
            received.clear()

            def finished_send(execution_id: str, *, chars: str = "", action: str = "write", write_timeout: float = 15.0) -> None:
                received.append((execution_id, action, chars))
                raise KeyError(f"Unknown or expired execution_id: {execution_id}")

            monkeypatch.setattr(runner, "send_execution_input", finished_send)
            app.attach_process_input("exec-2", "pipe")
            app.buffer.text = "after-exit-1"
            app.run_dispatch._submit_buffer("steer")
            app.buffer.text = "after-exit-2"
            app.run_dispatch._submit_buffer("steer")
            await app.flush_process_input()
            assert app.attached_execution_id is None
            # Only the first write was attempted; the second queued write was dropped once exit was seen!
            assert received == [("exec-2", "write", "after-exit-1\n")]

    try:
        asyncio.run(scenario())
    finally:
        ack_gate.set()
        runner.close()
