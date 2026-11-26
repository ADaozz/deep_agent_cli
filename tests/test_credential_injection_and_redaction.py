"""Stage 4 tests: MCP & Sandbox credential injection, /proc boundary, streaming/log redaction."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import time

from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, HumanMessage
import pytest

from agent.bootstrap.config import Settings
from agent.execution.backend import BubblewrapBackend, select_backend
from agent.execution.ownership import (
    reset_execution_thread_id,
    reset_root_session_id,
    set_execution_thread_id,
    set_root_session_id,
)
from agent.execution.policy import SandboxConfig
from agent.execution.process import _OutputCollector
from agent.mcp.adapter import build_transport, mcp_secret_consumer
from agent.mcp.config import MCPAuth, MCPAuthError, MCPServerConfig
from agent.mcp.contracts import ServerStatus
from agent.mcp.manager import MCPManager
from agent.redaction import SecretRef, StreamingSecretRedactor
from agent.runtime.auto_approval import ApprovalJournal, AutoApprovalMiddleware
from agent.runtime.control import RunController
from agent.runtime.permissions import PermissionMode
from agent.runtime.runner import AgentRunner
from agent.secrets import SecretStore
from agent.subagents.log_store import SubAgentLogStore
from tests.conftest import scripted_model


# ---------------------------------------------------------------------------
# 1. MCP Credential Resolution (Section 17)
# ---------------------------------------------------------------------------


def test_mcp_http_bearer_and_stdio_resolve_authorized_secrets_and_env_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEGACY_ENV_TOKEN", "env-bearer-fallback-12345")
    monkeypatch.setenv("LEGACY_STDIO_ENV", "env-stdio-fallback-67890")
    store = SecretStore(tmp_path, environ=os.environ)

    http_srv = MCPServerConfig(
        name="github",
        transport="streamable_http",
        url="https://api.github.com/mcp",
        auth=MCPAuth(type="bearer", token_env="GITHUB_TOKEN"),
    )
    stdio_srv = MCPServerConfig(
        name="localdb",
        transport="stdio",
        command="python3",
        args=["server.py"],
        env_passthrough=("DB_SECRET", "LEGACY_STDIO_ENV"),
    )

    # Managed persistent host secrets authorized ONLY for their exact server+target
    store.set_secret(
        "GITHUB_TOKEN",
        "ghp_managed_secret_token_abc",
        scope="host",
        storage="persistent",
        consumers=(mcp_secret_consumer(http_srv),),
    )
    store.set_secret(
        "DB_SECRET",
        "db_managed_secret_pass_xyz",
        scope="host",
        storage="persistent",
        consumers=(mcp_secret_consumer(stdio_srv),),
    )

    # 1) Authorized HTTP bearer resolves from SecretStore and registers SecretRef for redaction
    http_refs: list[SecretRef] = []
    transport = build_transport(http_srv, http_refs, oauth_callback_timeout=5, secret_store=store)
    assert transport.auth is not None
    assert transport.auth.token.get_secret_value() == "ghp_managed_secret_token_abc"
    assert any(ref.value == "ghp_managed_secret_token_abc" and ref.name == "GITHUB_TOKEN" for ref in http_refs)

    # 2) Target URL change loses authorization -> MCPAuthError (no silent access!)
    evil_srv = MCPServerConfig(
        name="github",
        transport="streamable_http",
        url="https://attacker.example/steal",
        auth=MCPAuth(type="bearer", token_env="GITHUB_TOKEN"),
    )
    with pytest.raises(MCPAuthError, match="not authorized"):
        build_transport(evil_srv, [], oauth_callback_timeout=5, secret_store=store)

    # 3) Legacy os.environ fallback continues working for unmanaged env vars
    legacy_srv = MCPServerConfig(
        name="legacy",
        transport="streamable_http",
        url="https://legacy.example/mcp",
        auth=MCPAuth(type="bearer", token_env="LEGACY_ENV_TOKEN"),
    )
    legacy_transport = build_transport(legacy_srv, [], oauth_callback_timeout=5, secret_store=store)
    assert legacy_transport.auth.token.get_secret_value() == "env-bearer-fallback-12345"

    # 4) Authorized stdio env_passthrough resolves managed + legacy env vars and redacts managed ones
    stdio_refs: list[SecretRef] = []
    stdio_transport = build_transport(stdio_srv, stdio_refs, oauth_callback_timeout=5, secret_store=store)
    assert stdio_transport.env["DB_SECRET"] == "db_managed_secret_pass_xyz"
    assert stdio_transport.env["LEGACY_STDIO_ENV"] == "env-stdio-fallback-67890"
    assert any(ref.value == "db_managed_secret_pass_xyz" and ref.name == "DB_SECRET" for ref in stdio_refs)

    # 5) sandbox-scoped secret is NEVER usable by MCP (host consumer)
    store.set_secret("SANDBOX_ONLY_KEY", "sb-only-123", scope="sandbox", storage="persistent")
    bad_scope_srv = MCPServerConfig(
        name="badscope",
        transport="streamable_http",
        url="https://api.example/mcp",
        auth=MCPAuth(type="bearer", token_env="SANDBOX_ONLY_KEY"),
    )
    with pytest.raises(MCPAuthError, match="not authorized"):
        build_transport(bad_scope_srv, [], oauth_callback_timeout=5, secret_store=store)

    # 6) Session-scoped secret is NEVER cached/resolved by global persistent MCP and gives clear storage guidance
    store.set_secret(
        "SESSION_ONLY_TOK",
        "sess-only-999",
        scope="host",
        storage="session",
        session_id="sess-1",
        consumers=("host:*",),
    )
    sess_srv = MCPServerConfig(
        name="sessmcp",
        transport="streamable_http",
        url="https://api.example/mcp",
        auth=MCPAuth(type="bearer", token_env="SESSION_ONLY_TOK"),
    )
    with pytest.raises(MCPAuthError, match="Session memory only.*Persistent host credential"):
        build_transport(sess_srv, [], oauth_callback_timeout=5, secret_store=store)


def test_mcp_manager_authorization_and_credential_invalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SecretStore(tmp_path, environ={})
    store.set_secret("MCP_API_KEY", "mcp_tok_v1", scope="host", storage="persistent")

    manager = MCPManager(tmp_path / "mcp.json", secret_store=store)
    seen_tokens: list[str] = []

    class _FakeClient:
        def __init__(self) -> None:
            self.closed = False

        def is_connected(self) -> bool:
            return not self.closed

        async def close(self) -> None:
            self.closed = True

    def fake_open_server(server, bridge, *, secret_store=None, **kwargs):
        refs: list[SecretRef] = []
        transport = build_transport(server, refs, oauth_callback_timeout=5, secret_store=secret_store)
        seen_tokens.append(transport.auth.token.get_secret_value())
        from agent.mcp.adapter import LoadedServer
        return LoadedServer(_FakeClient(), [], [])

    monkeypatch.setattr("agent.mcp.manager.open_server", fake_open_server)

    try:
        # Unapproved Agent add (authorize_secrets=False) MUST fail with AUTH_REQUIRED
        unapproved = manager.add_from_fields(
            name="untrusted",
            transport="streamable_http",
            url="https://evil.example/mcp",
            auth_type="bearer",
            token_env="MCP_API_KEY",
            authorize_secrets=False,
        )
        assert not unapproved.ok
        assert manager.get("untrusted").status is ServerStatus.AUTH_REQUIRED
        assert seen_tokens == []

        # Explicit user /mcp add (authorize_secrets=True) authorizes ONLY this server+target
        approved = manager.add_from_fields(
            name="trusted",
            transport="streamable_http",
            url="https://good.example/mcp",
            auth_type="bearer",
            token_env="MCP_API_KEY",
            authorize_secrets=True,
        )
        assert approved.ok
        assert manager.get("trusted").status is ServerStatus.CONNECTED
        assert seen_tokens == ["mcp_tok_v1"]

        # Unrelated server still cannot access MCP_API_KEY
        assert not manager.test("untrusted").ok

        # Rotating credential invalidates and disconnects referencing server; reconnect sees v2
        store.set_secret("MCP_API_KEY", "mcp_tok_v2", scope="host", storage="persistent", overwrite=True)
        affected = manager.invalidate_credential("MCP_API_KEY")
        assert "trusted" in affected
        assert manager.get("trusted").status is ServerStatus.DISCONNECTED
        assert manager.toolset_dirty() is True
        reconnected = manager.test("trusted")
        assert reconnected.ok
        assert seen_tokens == ["mcp_tok_v1", "mcp_tok_v2"]

        # Deleting credential invalidates server and subsequent reconnect fails cleanly
        store.delete_secret("MCP_API_KEY")
        manager.invalidate_credential("MCP_API_KEY")
        assert manager.get("trusted").status is ServerStatus.DISCONNECTED
        after_delete = manager.test("trusted")
        assert not after_delete.ok
        assert manager.get("trusted").status is ServerStatus.AUTH_REQUIRED
    finally:
        manager.close()


# ---------------------------------------------------------------------------
# 2. Streaming, Truncation, Log, SubAgent & AutoApproval Redaction (Section 19)
# ---------------------------------------------------------------------------


def test_streaming_redactor_handles_cross_chunk_splits_and_zero_latency_normal_text() -> None:
    secret_val = "sk_live_split_boundary_secret_42"
    refs = (SecretRef(name="API_KEY", value=secret_val, raw=False),)
    redactor = StreamingSecretRedactor(lambda: refs)

    # Normal non-prefix chunk emits immediately
    assert redactor.feed("hello world\n") == "hello world\n"

    # Secret split across 3 chunks is never emitted in plaintext in any chunk
    out1 = redactor.feed("token=sk_live_")
    out2 = redactor.feed("split_boundary_")
    out3 = redactor.feed("secret_42!\n")
    out4 = redactor.flush()
    assert secret_val not in out1
    assert "sk_live_" not in out1
    assert "split_boundary_" not in out2
    assert out1 + out2 + out3 + out4 == "token=$API_KEY!\n"


def test_output_collector_redacts_before_log_truncation_and_streaming(tmp_path: Path) -> None:
    secret_val = "sb_secret_MUST_NEVER_HIT_DISK_OR_STREAM_777"
    refs = [SecretRef(name="SANDBOX_SECRET", value=secret_val, raw=False)]
    emitted: list[tuple[str, str]] = []

    def emitter(tool_call_id, content, stream="merged", **kwargs):
        emitted.append((stream, content))

    collector = _OutputCollector(
        tool_call_id="tc-1",
        max_output_bytes=80,
        workspace_root=tmp_path,
        emit=emitter,
        execution_id="exec-1",
        thread_id="t-1",
        redaction_provider=lambda: tuple(refs),
    )
    try:
        # Split the secret across chunks AND exceed max_output_bytes so exec log is written & truncated
        collector.append(b"header " + b"A" * 50 + b"\nval=" + secret_val[:14].encode("utf-8"))
        collector.append(secret_val[14:].encode("utf-8") + b"\n" + b"B" * 90 + b"\nend=" + secret_val.encode("utf-8") + b"\n")
        collector.flush()
    finally:
        collector.close()

    meta = collector.log_metadata()
    assert meta["truncated"] is True
    assert meta["host_log_path"] is not None
    log_text = Path(meta["host_log_path"]).read_text(encoding="utf-8")

    # Plaintext MUST NOT appear in log file, tail_text, pending text, OR any emitted delta/snapshot!
    assert secret_val not in log_text
    assert "$SANDBOX_SECRET" in log_text
    tail = collector.tail_text()
    assert secret_val not in tail
    assert "$SANDBOX_SECRET" in tail
    pending = collector.take_pending()
    assert secret_val not in pending
    assert all(secret_val not in chunk for _stream, chunk in emitted)
    assert any("$SANDBOX_SECRET" in chunk for _stream, chunk in emitted)


def test_subagent_log_store_and_auto_approval_redact_dynamic_managed_secrets(tmp_path: Path) -> None:
    secret_val = "managed_secret_dynamic_redaction_999"
    store = SecretStore(tmp_path, environ={})

    # SubAgentLogStore constructed BEFORE the secret exists still redacts it dynamically!
    log_store = SubAgentLogStore(tmp_path / "subagents", secrets=lambda: store.redaction_refs(session_id="s-dyn"))
    store.set_secret("DYN_KEY", secret_val, scope="sandbox", storage="session", session_id="s-dyn")

    run_id = "0" * 32
    log_store.append("s-dyn", run_id, "finish", status="completed", result=f"leaked={secret_val}")
    raw_disk = (tmp_path / "subagents").rglob("*.jsonl")
    files = list(raw_disk)
    assert files
    text = files[0].read_text(encoding="utf-8")
    assert secret_val not in text
    assert "$DYN_KEY" in text

    # AutoApprovalMiddleware.evidence redacts managed SecretStore values
    controller = RunController()
    controller.secret_refs_provider = lambda: store.redaction_refs(session_id="s-dyn")
    from agent.bootstrap.factory import AgentSpec
    from langgraph.checkpoint.memory import InMemorySaver

    middleware = AutoApprovalMiddleware(
        spec=AgentSpec(settings=Settings()),
        backend=StateBackend(),
        controller=controller,
        journal=ApprovalJournal(InMemorySaver()),
        gated={"execute": True},
    )
    state = {"messages": [HumanMessage(content=f"deploy using {secret_val}")]}
    call = {"id": "c1", "name": "execute", "args": {"command": f"echo {secret_val}"}}
    evidence = middleware.evidence(state, call, {"sandbox": "restricted"})
    assert evidence is not None
    assert secret_val not in repr(evidence)
    assert "$DYN_KEY" in repr(evidence)


# ---------------------------------------------------------------------------
# 3. Real Bubblewrap Sandbox Dynamic Injection, Isolation & SubAgent (Section 18)
# ---------------------------------------------------------------------------


def _require_bwrap_backend(tmp_path: Path):
    try:
        selection = select_backend(SandboxConfig(workspace=tmp_path, max_output_bytes=4096))
    except Exception as exc:
        if os.environ.get("REQUIRE_BWRAP_TEST") == "1":
            raise
        pytest.skip(f"bubblewrap unavailable: {exc}")
    return selection.backend


def test_real_bwrap_dynamic_injection_worker_persistence_isolation_and_retired_redaction(
    tmp_path: Path,
) -> None:
    backend = _require_bwrap_backend(tmp_path)
    executor: BubblewrapBackend = backend.executor
    store = SecretStore(tmp_path / "cfg", environ={})
    backend.set_secret_provider(store.sandbox_env_for_session, lambda: store.redaction_refs())

    tok_a = set_execution_thread_id("sess-a")
    root_a = set_root_session_id("sess-a")
    try:
        # 1) Start a live background command in sess-a BEFORE adding DEPLOY_TOKEN
        bg = backend.execute(
            "read line; echo BG_GOT:$line",
            stdin_mode="pipe",
            yield_time=0.2,
            timeout=15,
        )
        assert bg.running and bg.execution_id
        worker_before = executor.pool.sandbox(False)
        assert worker_before is not None
        pid_before = worker_before.host_pid

        # 2) Add sandbox secret and host secret dynamically while background command runs
        sec_v1 = "sb_deploy_token_v1_secret_abc"
        host_sec = "host_only_secret_must_never_enter_sandbox"
        store.set_secret("DEPLOY_TOKEN", sec_v1, scope="sandbox", storage="session", session_id="sess-a")
        store.set_secret("HOST_ONLY_SECRET", host_sec, scope="host", storage="session", session_id="sess-a")

        # 3) Subsequent command in sess-a receives DEPLOY_TOKEN (verified via sha256)
        #    NEVER receives HOST_ONLY_SECRET, Worker PID unchanged, Worker /proc/1/environ clean!
        expected_sha = hashlib.sha256(sec_v1.encode()).hexdigest()
        check_cmd = (
            "python3 -c 'import hashlib, os; "
            "v = os.environ.get(\"DEPLOY_TOKEN\", \"\"); "
            "h = os.environ.get(\"HOST_ONLY_SECRET\", \"\"); "
            "w = open(\"/proc/1/environ\", \"rb\").read() if os.access(\"/proc/1/environ\", os.R_OK) else b\"\"; "
            "print(\"SHA:\" + hashlib.sha256(v.encode()).hexdigest()); "
            "print(\"HOST_EMPTY:\" + str(h == \"\")); "
            "print(\"WORKER_HAS:\" + str(b\"DEPLOY_TOKEN\" in w)); "
            "print(\"RAW_ECHO:\" + v)'"
        )
        res = backend.execute(check_cmd, timeout=15)
        assert res.exit_code == 0
        assert f"SHA:{expected_sha}" in res.output
        assert "HOST_EMPTY:True" in res.output
        assert "WORKER_HAS:False" in res.output
        # Raw value echoed by command MUST be redacted before returning!
        assert sec_v1 not in res.output
        assert "RAW_ECHO:$DEPLOY_TOKEN" in res.output

        # Worker was NOT restarted and earlier background command is still alive!
        worker_after = executor.pool.sandbox(False)
        assert worker_after is not None and worker_after.host_pid == pid_before
        bg_done = backend.write_stdin(bg.execution_id, chars="hello\n", yield_time=2.0)
        assert not bg_done.running
        assert "BG_GOT:hello" in bg_done.output

        # 4) Cross-session /proc boundary guard:
        #    Start a secret-bearing command in sess-a; while active, sess-b is BLOCKED from sharing the Worker!
        live_sec = backend.execute("sleep 5", stdin_mode="pipe", yield_time=0.2, timeout=15)
        assert live_sec.running and live_sec.execution_id
        t_b = set_execution_thread_id("sess-b")
        r_b = set_root_session_id("sess-b")
        try:
            blocked = backend.execute("echo hi", timeout=5)
            assert blocked.exit_code != 0
            assert "shared sandbox" in blocked.output and "/proc boundary" in blocked.output
        finally:
            reset_root_session_id(r_b)
            reset_execution_thread_id(t_b)

        # Cancel sess-a's live command; now sess-b runs normally and does NOT see sess-a's secret!
        backend.write_stdin(live_sec.execution_id, action="cancel", yield_time=2.0)
        t_b2 = set_execution_thread_id("sess-b")
        r_b2 = set_root_session_id("sess-b")
        try:
            clean_b = backend.execute(
                "python3 -c 'import os; print(\"HAS:\" + str(\"DEPLOY_TOKEN\" in os.environ))'",
                timeout=10,
            )
            assert clean_b.exit_code == 0
            assert "HAS:False" in clean_b.output
        finally:
            reset_root_session_id(r_b2)
            reset_execution_thread_id(t_b2)

        # 5) Retired secret redaction: background command started with sec_v1 prints sec_v1 AFTER deletion
        ret_bg = backend.execute(
            "read go; echo RETIRED_OUT=$DEPLOY_TOKEN",
            stdin_mode="pipe",
            yield_time=0.2,
            timeout=15,
        )
        assert ret_bg.running and ret_bg.execution_id
        assert store.delete_secret("DEPLOY_TOKEN", session_id="sess-a") is True

        # New command no longer receives DEPLOY_TOKEN
        after_del = backend.execute(
            "python3 -c 'import os; print(\"HAS:\" + str(\"DEPLOY_TOKEN\" in os.environ))'",
            timeout=10,
        )
        assert after_del.exit_code == 0 and "HAS:False" in after_del.output

        # Trigger old running command to print its inherited env value -> STILL redacted!
        ret_done = backend.write_stdin(ret_bg.execution_id, chars="go\n", yield_time=2.0)
        assert not ret_done.running
        assert sec_v1 not in ret_done.output
        assert "RETIRED_OUT=$DEPLOY_TOKEN" in ret_done.output
    finally:
        reset_root_session_id(root_a)
        reset_execution_thread_id(tok_a)
        executor.pool.shutdown("test finished")


def test_real_bwrap_subagent_inherits_root_sandbox_secret_without_widening_scope(tmp_path: Path) -> None:
    try:
        probe_sel = select_backend(SandboxConfig(workspace=tmp_path, max_output_bytes=4096))
        probe_sel.backend.executor.pool.shutdown("probe")
    except Exception as exc:
        if os.environ.get("REQUIRE_BWRAP_TEST") == "1":
            raise
        pytest.skip(f"bubblewrap unavailable: {exc}")

    sb_val = "subagent_inherited_sandbox_secret_888"
    host_val = "root_host_secret_not_for_subagent_999"
    expected_sha = hashlib.sha256(sb_val.encode()).hexdigest()

    store = SecretStore(tmp_path / "cfg", environ={})
    store.set_secret("SUB_SANDBOX_KEY", sb_val, scope="sandbox", storage="session", session_id="root-sess-1")
    store.set_secret("ROOT_HOST_KEY", host_val, scope="host", storage="session", session_id="root-sess-1")

    model = scripted_model([
        # Root delegates to subagent
        AIMessage(content="", tool_calls=[{
            "id": "call-sub",
            "name": "start_task",
            "args": {"description": "check inherited sandbox secret"},
        }]),
        # Subagent runs execute inside bwrap and writes digest to workspace file so we verify real execution!
        AIMessage(content="", tool_calls=[{
            "id": "call-exec",
            "name": "execute",
            "args": {
                "command": (
                    "python3 -c 'import hashlib, os; "
                    "s = os.environ.get(\"SUB_SANDBOX_KEY\", \"\"); "
                    "h = os.environ.get(\"ROOT_HOST_KEY\", \"\"); "
                    "open(\"sub_probe.txt\", \"w\").write("
                    "\"SUB_SHA:\" + hashlib.sha256(s.encode()).hexdigest() + \"\\n\" + "
                    "\"HOST_EMPTY:\" + str(h == \"\") + \"\\n\"); "
                    "print(\"ECHO:\" + s)'"
                ),
            },
        }]),
        # Subagent final answer
        AIMessage(content="Subagent checked secret successfully."),
        # Root final answer
        AIMessage(content="Root completed."),
    ])
    runner = AgentRunner(
        model=model,
        sandbox_config=SandboxConfig(workspace=tmp_path, max_output_bytes=4096),
        thread_id="root-sess-1",
        secret_store=store,
        settings=Settings(config_dir=tmp_path / "cfg"),
    )
    runner.set_permission_mode(PermissionMode.ALLOW)
    try:
        result = runner.invoke("run subagent check")
        assert result.status == "completed"

        # Real subagent process ran inside bwrap with root's sandbox secret and WITHOUT root's host secret!
        probe_file = (tmp_path / "sub_probe.txt").read_text(encoding="utf-8")
        assert f"SUB_SHA:{expected_sha}" in probe_file
        assert "HOST_EMPTY:True" in probe_file

        # Verify SubAgent log recorded run and contains ZERO plaintext secrets
        log_store = runner.task_log_store()
        assert log_store is not None
        runs = log_store.list_runs("root-sess-1")
        assert len(runs) == 1
        for log_file in (tmp_path / "cfg" / "subagents").rglob("*.jsonl"):
            raw = log_file.read_text(encoding="utf-8")
            assert sb_val not in raw
            assert host_val not in raw
    finally:
        runner.close()


def test_human_approved_mcp_add_authorizes_target_while_unapproved_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SecretStore(tmp_path, environ={})
    store.set_secret("GH_TOKEN", "ghp_approved_only_12345", scope="host", storage="persistent")
    manager = MCPManager(tmp_path / "mcp.json", secret_store=store)
    seen: list[str] = []

    class _DummyClient:
        def is_connected(self) -> bool:
            return True

        async def close(self) -> None:
            pass

    def fake_open(server, bridge, *, secret_store=None, **kwargs):
        refs: list[SecretRef] = []
        transport = build_transport(server, refs, oauth_callback_timeout=5, secret_store=secret_store)
        seen.append(transport.auth.token.get_secret_value())
        from agent.mcp.adapter import LoadedServer
        return LoadedServer(_DummyClient(), [], [])

    monkeypatch.setattr("agent.mcp.manager.open_server", fake_open)
    model = scripted_model([
        AIMessage(content="", tool_calls=[{
            "id": "call-mcp-add",
            "name": "mcp_add",
            "args": {
                "name": "gh",
                "transport": "streamable_http",
                "url": "https://mcp.github.example/mcp",
                "auth_type": "bearer",
                "token_env": "GH_TOKEN",
            },
        }]),
        AIMessage(content="added"),
    ])
    runner = AgentRunner(
        model=model,
        backend=StateBackend(),
        thread_id="mcp-approve-t",
        mcp_manager=manager,
        secret_store=store,
    )
    try:
        waiting = runner.invoke("add gh mcp")
        assert waiting.status == "waiting_confirmation"
        done = runner.approve_tool("call-mcp-add")
        assert done.status == "completed"
        assert seen == ["ghp_approved_only_12345"]
        assert manager.get("gh").status is ServerStatus.CONNECTED
    finally:
        runner.close()
