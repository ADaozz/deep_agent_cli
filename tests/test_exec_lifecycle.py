"""Yielded execute + write_stdin lifecycle (requires Bubblewrap)."""

from __future__ import annotations

from collections.abc import Iterator
import hashlib
import os
from pathlib import Path
import threading
import time
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from agent.bootstrap.config import Settings
from agent.bootstrap.factory import create_agent
from agent.cli.state import CliState, ToolBlock
from agent.execution.backend import select_backend
from agent.execution.filesystem import WorkspaceCompositeBackend
from agent.execution.lifecycle import ExecutionRegistry, write_stdin_is_side_effect
from agent.execution.ownership import reset_execution_thread_id, set_execution_thread_id
from agent.execution.policy import SandboxConfig
from agent.execution.pool import StdinWriteUncertainError
from agent.execution.types import SandboxUnavailableError
from agent.models.profile import ModelProfile
from agent.runtime.approval.rules import static_rule
from agent.runtime.cancellation import (
    ToolCancelContext,
    clear_cancel_context,
    set_cancel_context,
    set_output_emitter,
)
from agent.runtime.contracts import RunEvent, RunResult
from agent.runtime.permissions import PermissionMode, ask_interrupt_on, write_stdin_requires_approval
from agent.runtime.runner import AgentRunner
from agent.sessions.store import SessionStore
from agent.sessions.transcript import messages_to_transcript, settle_restored_tools
from agent.subagents.configuration import SubAgentConfig
from agent.tools.catalog import WRITE_STDIN_TOOL, node_tools
from agent.tools.execute import build_execute_tool, build_write_stdin_tool, lookup_execution_network
from tests.conftest import ScriptedToolModel, scripted_model


pytestmark = pytest.mark.sandbox

_TERMINAL_META_KEYS = (
    "execution_id",
    "running",
    "exit_code",
    "termination_reason",
    "truncated",
    "host_log_path",
    "agent_log_path",
    "log_error",
    "max_output_bytes",
)


def _bwrap_config(
    workspace: Path, *, timeout_seconds: int = 30, max_output_bytes: int = 1_000_000,
) -> SandboxConfig:
    import shutil

    executable = shutil.which("bwrap")
    if executable is None:
        if os.environ.get("REQUIRE_BWRAP_TEST"):
            pytest.fail("bubblewrap is required in CI")
        pytest.skip("UNSUPPORTED SANDBOX ENVIRONMENT [not_installed]: bubblewrap is not installed")
    return SandboxConfig(
        workspace=workspace,
        bwrap_path=executable,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )


def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise TimeoutError("condition not reached within timeout")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def backend(workspace: Path) -> Iterator[WorkspaceCompositeBackend]:
    try:
        selected = select_backend(_bwrap_config(workspace))
    except SandboxUnavailableError as exc:
        if os.environ.get("REQUIRE_BWRAP_TEST"):
            pytest.fail(str(exc))
        pytest.skip(f"UNSUPPORTED SANDBOX ENVIRONMENT [{exc.kind}]: {exc}")
    assert isinstance(selected.backend, WorkspaceCompositeBackend)
    ownership = set_execution_thread_id("test-thread")
    yield selected.backend
    reset_execution_thread_id(ownership)
    selected.backend.executor.pool.shutdown("test finished")


def test_write_stdin_is_common_tool() -> None:
    assert WRITE_STDIN_TOOL in node_tools("root")
    assert WRITE_STDIN_TOOL in node_tools("sync")


def test_sync_execute_via_tool(backend: WorkspaceCompositeBackend) -> None:
    tool = build_execute_tool(backend)
    content, artifact = tool.func(command="printf hello", network=False)
    assert content == "hello"
    assert artifact["running"] is False
    assert artifact["exit_code"] == 0
    assert artifact["termination_reason"] is None


def test_sync_execute_cancelled_by_user_returns_cancelled_130(
    backend: WorkspaceCompositeBackend,
) -> None:
    execute = build_execute_tool(backend)
    ctx = ToolCancelContext(
        tool_name="execute", tool_call_id="sync-cancel-1", cancel_event=threading.Event(),
    )
    events: list[dict[str, Any]] = []

    def capture(
        tool_call_id: str, content: str = "", stream: str = "merged",
        *, result: Any = None, name: str = "", **_kwargs: Any,
    ) -> None:
        if stream == "execution_finished" and isinstance(result, dict):
            events.append(result)

    def trigger_cancel() -> None:
        time.sleep(0.25)
        ctx.request_cancel()

    set_output_emitter(capture)
    set_cancel_context(ctx)
    canceller = threading.Thread(target=trigger_cancel, daemon=True)
    canceller.start()
    try:
        content, artifact = execute.func(command="sleep 30", network=False)
    finally:
        canceller.join(timeout=2.0)
        clear_cancel_context()
        set_output_emitter(None)

    assert artifact["running"] is False
    assert artifact["termination_reason"] == "cancelled"
    assert artifact["exit_code"] == 130
    assert "Cancelled by user." in content
    _wait_until(lambda: bool(events))
    assert events[-1]["termination_reason"] == "cancelled"
    assert events[-1]["exit_code"] == 130
    assert events[-1]["running"] is False


def test_yield_poll_cancel_via_tools(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    content, artifact = execute.func(
        command="sleep 30; printf done", network=False, yield_time=0.2,
    )
    assert artifact["running"] is True
    assert artifact["execution_id"]
    assert artifact["exit_code"] is None
    assert artifact["termination_reason"] is None
    assert f"[running execution_id={artifact['execution_id']}]" in content
    execution_id = str(artifact["execution_id"])
    polled, poll_art = write.func(execution_id=execution_id, chars="", yield_time=0.15)
    assert poll_art["running"] is True
    assert poll_art["exit_code"] is None
    assert f"[running execution_id={execution_id}]" in polled
    cancelled, cancel_art = write.func(
        execution_id=execution_id, chars="", yield_time=3.0, action="cancel",
    )
    assert cancel_art["running"] is False
    assert cancel_art["exit_code"] == 130
    assert cancel_art["termination_reason"] == "cancelled"
    assert "Cancelled by user." in cancelled


def test_yield_detaches_from_original_and_subsequent_turn_cancel(workspace: Path) -> None:
    try:
        selected = select_backend(_bwrap_config(workspace))
    except SandboxUnavailableError as exc:
        pytest.skip(str(exc))
    backend = selected.backend
    assert isinstance(backend, WorkspaceCompositeBackend)

    events: list[RunEvent] = []
    started_turn2 = threading.Event()

    def on_event(event: RunEvent) -> None:
        events.append(event)
        if event.type == "tool_started" and event.tool_call_id == "call-turn2-sync":
            started_turn2.set()

    prepared = create_agent(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "name": "execute",
                "id": "call-turn1-yield",
                "args": {"command": "sleep 1.2; printf 'survived-turn-cancel'", "yield_time": 0.15, "network": False},
            }]),
            AIMessage(content="background started"),
            AIMessage(content="", tool_calls=[{
                "name": "execute",
                "id": "call-turn2-sync",
                "args": {"command": "sleep 30", "network": False},
            }]),
            AIMessage(content="turn 2 done"),
        ]),
        backend=backend,
        backend_selection=selected,
        skills=[],
        permission_mode=PermissionMode.ALLOW,
    )
    runner = AgentRunner(prepared=prepared, thread_id="detach-cancel-thread", on_event=on_event)
    try:
        first = runner.invoke("start bg")
        assert first.status == "completed"
        turn1_completed = next(
            e for e in events if e.type == "tool_completed" and e.tool_call_id == "call-turn1-yield"
        )
        assert turn1_completed.artifact["running"] is True
        bg_execution_id = str(turn1_completed.artifact["execution_id"])

        def cancel_turn2() -> None:
            assert started_turn2.wait(5.0)
            time.sleep(0.2)
            runner.request_cancel()

        canceller = threading.Thread(target=cancel_turn2, daemon=True)
        canceller.start()
        second = runner.invoke("run sync to cancel")
        canceller.join(timeout=5.0)
        assert second.status == "cancelled"

        # Background process from Turn 1 must still be alive or finish naturally with exit 0.
        ownership = set_execution_thread_id("detach-cancel-thread")
        try:
            write = build_write_stdin_tool(backend)
            text, art = write.func(execution_id=bg_execution_id, chars="", yield_time=3.0)
            assert art["running"] is False
            assert art["exit_code"] == 0
            assert art["termination_reason"] is None
            assert "survived-turn-cancel" in text
        finally:
            reset_execution_thread_id(ownership)
    finally:
        runner.close()


def test_interactive_pipe_via_tools(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    _content, artifact = execute.func(
        command="python3 -c 'import sys; print(sys.stdin.readline().strip(), flush=True)'",
        network=False, yield_time=0.25, interactive=True, timeout=10,
    )
    assert artifact["running"] is True
    execution_id = str(artifact["execution_id"])
    done, done_art = write.func(execution_id=execution_id, chars="piped-line\n", yield_time=3.0)
    assert done_art["running"] is False
    assert done_art["exit_code"] == 0
    assert done_art["termination_reason"] is None
    assert done == "piped-line"


def test_pty_dev_tty_and_foreground_group(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    command = (
        "python3 -c '"
        "import os, sys\n"
        "print(\"READY\", flush=True)\n"
        "line = sys.stdin.readline().strip()\n"
        "fd = os.open(\"/dev/tty\", os.O_RDWR)\n"
        "os.close(fd)\n"
        "print(\"ISATTY_OK\" if os.isatty(0) else \"ISATTY_FAIL\", flush=True)\n"
        "print(\"OPEN_DEV_TTY_OK\", flush=True)\n"
        "print(\"FG_OK\" if os.tcgetpgrp(0) == os.getpgrp() else \"FG_FAIL\", flush=True)\n"
        "print(f\"ECHO:{line}\", flush=True)\n"
        "'"
    )
    first, artifact = execute.func(
        command=command, network=False, yield_time=0.5, tty=True, timeout=10,
    )
    assert artifact["running"] is True
    execution_id = str(artifact["execution_id"])
    seen = first
    deadline = time.monotonic() + 5.0
    while "READY" not in seen and time.monotonic() < deadline:
        chunk, art = write.func(execution_id=execution_id, chars="", yield_time=0.2)
        seen += chunk
        assert art["running"] is True
    assert "READY" in seen

    done, done_art = write.func(execution_id=execution_id, chars="from-pty\n", yield_time=3.0)
    assert done_art["running"] is False
    assert done_art["exit_code"] == 0
    assert done_art["termination_reason"] is None
    assert "ISATTY_OK" in done
    assert "OPEN_DEV_TTY_OK" in done
    assert "FG_OK" in done
    assert "ECHO:from-pty" in done


def test_pty_ctrl_c_interrupts_foreground(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    first, artifact = execute.func(
        command=(
            "python3 -c '"
            "import signal, time; "
            "signal.signal(signal.SIGINT, signal.SIG_DFL); "
            "print(\"READY\", flush=True); "
            "time.sleep(30)"
            "'"
        ),
        network=False, yield_time=0.5, tty=True, timeout=15,
    )
    assert artifact["running"] is True
    execution_id = str(artifact["execution_id"])
    seen = first
    deadline = time.monotonic() + 5.0
    while "READY" not in seen and time.monotonic() < deadline:
        chunk, art = write.func(execution_id=execution_id, chars="", yield_time=0.2)
        seen += chunk
        assert art["running"] is True
    assert "READY" in seen

    # Ctrl+C (\x03) alone must interrupt the foreground process — no fallback cancel.
    _done, done_art = write.func(execution_id=execution_id, chars="\x03", yield_time=3.0)
    assert done_art["running"] is False
    assert done_art["termination_reason"] is None
    assert done_art["exit_code"] == 130


def test_retained_terminal_result_and_incremental_polls(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    first, artifact = execute.func(
        command="printf 'first\\n'; sleep 0.35; printf 'second\\n'; sleep 0.35; printf 'third\\n'",
        network=False, yield_time=0.15, timeout=10,
    )
    assert artifact["running"] is True
    execution_id = str(artifact["execution_id"])
    chunks = [first]
    final_art: dict[str, Any] | None = None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        text, art = write.func(execution_id=execution_id, chars="", yield_time=0.25)
        chunks.append(text)
        if not art["running"]:
            final_art = art
            break
    assert final_art is not None
    assert final_art["running"] is False
    assert final_art["exit_code"] == 0
    assert final_art["termination_reason"] is None
    # Each line appears in exactly one incremental poll — no historical replay.
    for token in ("first", "second", "third"):
        occurrences = sum(chunk.count(token) for chunk in chunks)
        assert occurrences == 1, f"expected {token!r} once across polls, got {occurrences}: {chunks}"
    # Consumed terminal record is removed immediately.
    with pytest.raises(KeyError):
        write.func(execution_id=execution_id, chars="", yield_time=0.1)


def test_retained_terminal_result_after_natural_finish(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    _first, artifact = execute.func(
        command="sleep 0.25; printf 'finished-in-bg\\n'",
        network=False, yield_time=0.1, timeout=10,
    )
    assert artifact["running"] is True
    execution_id = str(artifact["execution_id"])
    # Wait for the process to finish naturally before polling.
    session = backend.executor.pool.executions.get(execution_id)
    assert session is not None
    _wait_until(lambda: session.is_done, timeout=5.0)
    # Worker handle must already be released while terminal result remains queryable.
    assert session.handle is None

    text, art = write.func(execution_id=execution_id, chars="", yield_time=0.1)
    assert art["running"] is False
    assert art["exit_code"] == 0
    assert art["termination_reason"] is None
    assert "finished-in-bg" in text
    with pytest.raises(KeyError):
        write.func(execution_id=execution_id, chars="", yield_time=0.1)


def test_continuous_poll_after_truncation(workspace: Path) -> None:
    try:
        selected = select_backend(_bwrap_config(workspace, max_output_bytes=64))
    except SandboxUnavailableError as exc:
        pytest.skip(str(exc))
    backend = selected.backend
    assert isinstance(backend, WorkspaceCompositeBackend)
    ownership = set_execution_thread_id("truncate-thread")
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    try:
        first, artifact = execute.func(
            command="python3 -c 'import time; print(\"A\"*80, flush=True); time.sleep(0.35); print(\"B\"*20, flush=True)'",
            network=False, yield_time=0.2, timeout=10,
        )
        assert artifact["running"] is True
        execution_id = str(artifact["execution_id"])
        assert "A" * 20 in first
        session = backend.executor.pool.executions.get(execution_id)
        assert session is not None
        _wait_until(lambda: session.is_done, timeout=5.0)

        text, art = write.func(execution_id=execution_id, chars="", yield_time=1.0)
        assert art["running"] is False
        assert art["exit_code"] == 0
        assert art["termination_reason"] is None
        assert art["truncated"] is True
        assert "B" * 20 in text
        assert "A" * 20 not in text
        assert art["host_log_path"] and Path(str(art["host_log_path"])).is_file()
        assert art["agent_log_path"]
        saved = Path(str(art["host_log_path"])).read_text(encoding="utf-8")
        assert "A" * 80 in saved
        assert "B" * 20 in saved
    finally:
        reset_execution_thread_id(ownership)
        backend.executor.pool.shutdown("truncate done")


def test_terminal_retention_eviction_policy(backend: WorkspaceCompositeBackend) -> None:
    now = [100.0]
    pool = backend.executor.pool
    pool.executions = ExecutionRegistry(max_terminal=2, terminal_ttl_seconds=10.0, clock=lambda: now[0])
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)

    # Start one long-running process that must NEVER be evicted.
    _running_out, running_art = execute.func(command="sleep 30", network=False, yield_time=0.1)
    running_id = str(running_art["execution_id"])

    finished_ids: list[str] = []
    for index in range(3):
        now[0] += 1.0
        _out, art = execute.func(
            command=f"sleep 0.15; printf 'done-{index}'", network=False, yield_time=0.05,
        )
        eid = str(art["execution_id"])
        session = pool.executions.get(eid)
        assert session is not None
        _wait_until(lambda s=session: s.is_done, timeout=5.0)
        finished_ids.append(eid)

    # Oldest unconsumed terminal record evicted by max_terminal=2; newest 2 + running survive.
    assert pool.executions.get(finished_ids[0]) is None
    assert pool.executions.get(finished_ids[1]) is not None
    assert pool.executions.get(finished_ids[2]) is not None
    assert pool.executions.get(running_id) is not None

    # Advance past TTL: remaining unconsumed terminal records expire lazily; running still survives.
    now[0] += 20.0
    assert pool.executions.get(finished_ids[1]) is None
    assert pool.executions.get(finished_ids[2]) is None
    assert pool.executions.get(running_id) is not None

    # Clean up the running execution.
    _out, cancel_art = write.func(execution_id=running_id, chars="", yield_time=2.0, action="cancel")
    assert cancel_art["running"] is False
    assert cancel_art["exit_code"] == 130


def test_background_timeout_via_tools(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    _content, artifact = execute.func(command="sleep 30", network=False, yield_time=0.1, timeout=1)
    assert artifact["running"] is True
    execution_id = str(artifact["execution_id"])
    deadline = time.monotonic() + 5.0
    final_text = ""
    final_art: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        text, art = write.func(execution_id=execution_id, chars="", yield_time=0.3)
        final_text, final_art = text, art
        if not art["running"]:
            break
    assert final_art is not None
    assert final_art["running"] is False
    assert final_art["exit_code"] == 124
    assert final_art["termination_reason"] == "timeout"
    assert "timed out after 1 seconds" in final_text


def test_multiple_writes_partial_write_and_eof_order(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    # Process pauses before reading so a >192KiB write hits BlockingIOError / partial write.
    command = (
        "python3 -c '"
        "import hashlib, sys, time; "
        "time.sleep(0.35); "
        "data = sys.stdin.buffer.read(); "
        "print(f\"LEN={len(data)} SHA={hashlib.sha256(data).hexdigest()}\", flush=True)"
        "'"
    )
    _content, artifact = execute.func(
        command=command, network=False, yield_time=0.1, interactive=True, timeout=15,
    )
    assert artifact["running"] is True
    execution_id = str(artifact["execution_id"])

    # Build ordered chunks whose total size exceeds Linux's 64KiB pipe buffer.
    chunks = [f"part-{i:02d}-" + ("X" * 32768) + "\n" for i in range(6)]
    chunks.append("FINAL-TAIL-BEFORE-EOF\n")
    expected_bytes = "".join(chunks).encode("utf-8")
    expected_sha = hashlib.sha256(expected_bytes).hexdigest()

    for chunk in chunks:
        _out, art = write.func(execution_id=execution_id, chars=chunk, yield_time=0.0)
        assert art["running"] is True
        assert art.get("termination_reason") is None

    # EOF must take effect strictly after all prior writes have been written.
    done, done_art = write.func(execution_id=execution_id, chars="", yield_time=5.0, action="close")
    assert done_art["running"] is False
    assert done_art["exit_code"] == 0
    assert done_art["termination_reason"] is None
    assert f"LEN={len(expected_bytes)} SHA={expected_sha}" in done


def test_delayed_ack_and_timeout_uncertain_without_false_confirm(
    backend: WorkspaceCompositeBackend, workspace: Path,
) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    gate = workspace / "start_reading.flag"
    # Hold reading stdin until the gate file appears so pipe fills and worker write blocks.
    command = (
        "python3 -c '"
        "import hashlib, os, sys, time\n"
        "gate = sys.argv[1]\n"
        "print(\"READY\", flush=True)\n"
        "while not os.path.exists(gate):\n"
        "    time.sleep(0.02)\n"
        "data = sys.stdin.buffer.read()\n"
        "print(f\"LEN={len(data)} SHA={hashlib.sha256(data).hexdigest()}\", flush=True)\n"
        f"' {backend.workspace_root}/start_reading.flag"
    )
    first, artifact = execute.func(
        command=command, network=False, yield_time=0.4, interactive=True, timeout=15,
    )
    assert artifact["running"] is True
    execution_id = str(artifact["execution_id"])
    seen = first
    deadline = time.monotonic() + 5.0
    while "READY" not in seen and time.monotonic() < deadline:
        chunk, art = write.func(execution_id=execution_id, chars="", yield_time=0.2)
        seen += chunk
        assert art["running"] is True
    assert "READY" in seen

    session = backend.executor.pool.executions.get(execution_id)
    assert session is not None and session.handle is not None
    handle = session.handle

    # Write 256KiB (> Linux pipe buffer) with a short ACK timeout -> must report uncertain outcome.
    first_payload = ("A" * 262144).encode("utf-8")
    with pytest.raises(StdinWriteUncertainError) as exc_info:
        handle.write_stdin(first_payload, timeout=0.15)
    timed_out_write_id = exc_info.value.write_id
    assert timed_out_write_id
    assert "unknown" in str(exc_info.value).lower()

    # Queue a second write while pipe is still blocked.
    second_payload = b"SECOND-PAYLOAD\n"
    second_result: dict[str, Any] = {}

    def run_second_write() -> None:
        try:
            handle.write_stdin(second_payload, timeout=5.0)
            second_result["ok"] = True
        except BaseException as exc:  # noqa: BLE001
            second_result["error"] = exc

    second_thread = threading.Thread(target=run_second_write, daemon=True)
    second_thread.start()
    _wait_until(lambda: bool(handle._pending_acks), timeout=2.0)

    # Injecting the delayed ACK for the timed-out first write_id MUST NOT confirm the second write.
    handle._complete_write(write_id=timed_out_write_id, ok=True)
    time.sleep(0.15)
    assert second_thread.is_alive(), "delayed ACK from earlier write_id falsely confirmed subsequent write"

    # Now let the child drain stdin; both writes complete without duplication or loss.
    gate.write_text("go", encoding="utf-8")
    second_thread.join(timeout=5.0)
    assert not second_thread.is_alive()
    assert second_result == {"ok": True}

    done, done_art = write.func(execution_id=execution_id, chars="", yield_time=5.0, action="close")
    assert done_art["running"] is False
    assert done_art["exit_code"] == 0
    expected = first_payload + second_payload
    assert f"LEN={len(expected)} SHA={hashlib.sha256(expected).hexdigest()}" in done


def test_subagent_real_execution_isolation_and_cleanup(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent.bootstrap.factory as factory

    try:
        selected = select_backend(_bwrap_config(workspace))
    except SandboxUnavailableError as exc:
        pytest.skip(str(exc))
    backend = selected.backend
    assert isinstance(backend, WorkspaceCompositeBackend)

    cfg = Settings(
        config_dir=tmp_path / "private",
        llm_profiles=(ModelProfile("default", "parent"), ModelProfile("child", "child")),
        subagents=SubAgentConfig(model="child", max_concurrent=2),
    )

    root_exec_id_holder: list[str] = []
    child_exec_id_holder: list[str] = []
    root_denied_child = threading.Event()
    subagent_error_messages: list[str] = []
    child_runs = iter(["normal", "cross_probe"])

    class ChildModel(ScriptedToolModel):
        mode: str = "normal"
        step: int = 0

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
            from langchain_core.outputs import ChatGeneration, ChatResult

            self.step += 1
            if self.mode == "normal":
                if self.step == 1:
                    return ChatResult(generations=[ChatGeneration(message=AIMessage(
                        content="",
                        tool_calls=[{
                            "name": "execute",
                            "id": "child-bg-exec",
                            "args": {"command": "sleep 20", "yield_time": 0.15, "network": False},
                        }],
                    ))])
                tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
                art = getattr(tool_msgs[-1], "artifact", None)
                assert isinstance(art, dict) and art.get("execution_id")
                child_eid = str(art["execution_id"])
                child_exec_id_holder.append(child_eid)
                # While SubAgent is alive, root session must NOT be able to access child's execution.
                with pytest.raises(PermissionError):
                    backend.executor.pool.executions.require(child_eid, thread_id="root-session-id")
                root_denied_child.set()
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="child finished"))])
            # Cross-probe child attempts to read root's execution -> PermissionError isolates it.
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content="",
                tool_calls=[{
                    "name": "write_stdin",
                    "id": "child-probe-root",
                    "args": {"execution_id": root_exec_id_holder[0], "chars": "", "yield_time": 0.1},
                }],
            ))])

    monkeypatch.setattr(
        factory, "build_chat_model",
        lambda profile, **kwargs: ChildModel(mode=next(child_runs), messages=iter([])),
    )

    class ParentModel(ScriptedToolModel):
        step: int = 0

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
            from langchain_core.outputs import ChatGeneration, ChatResult

            self.step += 1
            if self.step == 1:
                return ChatResult(generations=[ChatGeneration(message=AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "execute",
                        "id": "root-bg-exec",
                        "args": {"command": "sleep 20", "yield_time": 0.15, "network": False},
                    }],
                ))])
            if self.step == 2:
                tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
                art = getattr(tool_msgs[-1], "artifact", None)
                assert isinstance(art, dict) and art.get("execution_id")
                root_exec_id_holder.append(str(art["execution_id"]))
                return ChatResult(generations=[ChatGeneration(message=AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "start_task",
                        "id": "delegate-normal",
                        "args": {"description": "start child bg and finish", "run_in_background": False},
                    }],
                ))])
            if self.step == 3:
                return ChatResult(generations=[ChatGeneration(message=AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "start_task",
                        "id": "delegate-probe",
                        "args": {"description": "attempt cross-access to root execution", "run_in_background": False},
                    }],
                ))])
            tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
            if tool_msgs:
                subagent_error_messages.append(str(tool_msgs[-1].content))
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="parent done"))])

    prepared = create_agent(
        model=ParentModel(messages=iter([])),
        backend=backend,
        backend_selection=selected,
        settings=cfg,
        skills=[],
        permission_mode=PermissionMode.ALLOW,
    )
    runner = AgentRunner(prepared=prepared, thread_id="root-session-id", settings=cfg)
    try:
        result = runner.invoke("run isolation test")
        assert result.status == "completed"
        assert root_denied_child.is_set()
        assert subagent_error_messages
        assert "another session or agent" in subagent_error_messages[-1]
        assert len(root_exec_id_holder) == 1
        assert len(child_exec_id_holder) == 1
        root_eid = root_exec_id_holder[0]
        child_eid = child_exec_id_holder[0]
        # SubAgent ended -> its execution must be cancelled and removed from the registry.
        assert backend.executor.pool.executions.get(child_eid) is None
        # Root's own yielded execution must remain alive and unaffected.
        root_session = backend.executor.pool.executions.get(root_eid)
        assert root_session is not None and not root_session.is_done
        root_session.cancel()
        root_session.join_pump(timeout=3.0)
    finally:
        runner.close()


def test_registry_is_per_pool(workspace: Path) -> None:
    try:
        left = select_backend(_bwrap_config(workspace))
        right_ws = workspace / "right"
        right_ws.mkdir(exist_ok=True)
        right = select_backend(_bwrap_config(right_ws))
    except SandboxUnavailableError as exc:
        pytest.skip(str(exc))
    assert isinstance(left.backend, WorkspaceCompositeBackend)
    assert isinstance(right.backend, WorkspaceCompositeBackend)
    ownership = set_execution_thread_id("pool-owner")
    execute = build_execute_tool(left.backend)
    write = build_write_stdin_tool(left.backend)
    try:
        _content, artifact = execute.func(command="sleep 10", network=False, yield_time=0.2)
        execution_id = str(artifact["execution_id"])
        assert left.backend.executor.pool.executions.get(execution_id) is not None
        assert right.backend.executor.pool.executions.get(execution_id) is None
        right.backend.executor.pool.shutdown("right closed")
        assert left.backend.executor.pool.executions.get(execution_id) is not None
        _out, cancel_art = write.func(execution_id=execution_id, chars="", yield_time=2.0, action="cancel")
        assert cancel_art["running"] is False
        assert cancel_art["exit_code"] == 130
    finally:
        reset_execution_thread_id(ownership)
        left.backend.executor.pool.shutdown("left closed")


def test_write_stdin_approval_shared_semantics(backend: WorkspaceCompositeBackend) -> None:
    class _Req:
        def __init__(self, args: dict) -> None:
            self.tool_call = {"name": "write_stdin", "args": args}

    assert write_stdin_is_side_effect({"action": "write", "chars": ""}) is False
    assert write_stdin_is_side_effect({"action": "write", "chars": "x"}) is True
    assert write_stdin_requires_approval(_Req({"action": "write", "chars": ""})) is False
    assert write_stdin_requires_approval(_Req({"action": "cancel", "chars": ""})) is True
    assert ask_interrupt_on()[WRITE_STDIN_TOOL]["when"] is write_stdin_requires_approval

    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    _content, artifact = execute.func(command="sleep 10", network=False, yield_time=0.2)
    execution_id = str(artifact["execution_id"])
    assert lookup_execution_network(backend, execution_id) is False
    allowed = static_rule(
        {"name": "write_stdin", "args": {"execution_id": execution_id, "chars": "x\n"}},
        gated=ask_interrupt_on(),
        backend=backend,
        sandbox_mode="restricted",
    )
    assert allowed is not None and allowed.decision == "allow"
    _out, cancel_art = write.func(execution_id=execution_id, chars="", yield_time=2.0, action="cancel")
    assert cancel_art["exit_code"] == 130


def test_prestart_write_acks_only_after_drain(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    _content, artifact = execute.func(
        command="python3 -c 'import sys; print(sys.stdin.read(), end=\"\")'",
        network=False, yield_time=0.05, interactive=True, timeout=10,
    )
    execution_id = str(artifact["execution_id"])
    _out, write_art = write.func(execution_id=execution_id, chars="before-or-after\n", yield_time=0.05)
    assert write_art.get("termination_reason") is None
    done, done_art = write.func(execution_id=execution_id, chars="", yield_time=3.0, action="close")
    assert done_art["running"] is False
    assert done_art["exit_code"] == 0
    assert done == "before-or-after"


def test_tool_message_and_run_event_terminal_metadata_match(workspace: Path) -> None:
    try:
        selected = select_backend(_bwrap_config(workspace, max_output_bytes=32))
    except SandboxUnavailableError as exc:
        pytest.skip(str(exc))
    backend = selected.backend
    assert isinstance(backend, WorkspaceCompositeBackend)

    events: list[RunEvent] = []
    prepared = create_agent(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "name": "execute",
                "id": "call-trunc-fail",
                "args": {
                    "command": "python3 -c 'import sys; print(\"Z\"*64); sys.exit(7)'",
                    "network": False,
                },
            }]),
            AIMessage(content="done"),
        ]),
        backend=backend,
        backend_selection=selected,
        skills=[],
        permission_mode=PermissionMode.ALLOW,
    )
    runner = AgentRunner(prepared=prepared, thread_id="meta-match-thread")
    try:
        result = runner.invoke("run trunc fail", on_event=events.append)
        assert result.status == "completed"
        tool_completed = next(
            e for e in events if e.type == "tool_completed" and e.tool_call_id == "call-trunc-fail"
        )
        _wait_until(lambda: any(
            e.type == "tool_output_delta" and e.stream == "execution_finished" and e.tool_call_id == "call-trunc-fail"
            for e in events
        ))
        finished = next(
            e for e in events
            if e.type == "tool_output_delta" and e.stream == "execution_finished" and e.tool_call_id == "call-trunc-fail"
        )
        assert isinstance(tool_completed.artifact, dict)
        assert isinstance(finished.result, dict)
        for key in _TERMINAL_META_KEYS:
            assert tool_completed.artifact[key] == finished.result[key], f"mismatch on {key}"
        assert tool_completed.artifact["exit_code"] == 7
        assert tool_completed.artifact["termination_reason"] is None
        assert tool_completed.artifact["truncated"] is True
        assert tool_completed.artifact["host_log_path"]
        assert tool_completed.artifact["agent_log_path"]
    finally:
        runner.close()


def test_tui_background_execute_survives_turn_end_and_updates_on_real_finish(workspace: Path) -> None:
    try:
        selected = select_backend(_bwrap_config(workspace))
    except SandboxUnavailableError as exc:
        pytest.skip(str(exc))
    backend = selected.backend
    assert isinstance(backend, WorkspaceCompositeBackend)

    state = CliState(session_id="tui-bg-thread")
    events: list[RunEvent] = []

    def on_event(event: RunEvent) -> None:
        events.append(event)
        state.apply(event)

    prepared = create_agent(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "name": "execute",
                "id": "call-bg-card",
                "args": {"command": "printf 'step1\\n'; sleep 0.6; printf 'step2\\n'", "yield_time": 0.15, "network": False},
            }]),
            AIMessage(content="turn complete while running"),
        ]),
        backend=backend,
        backend_selection=selected,
        skills=[],
        permission_mode=PermissionMode.ALLOW,
    )
    runner = AgentRunner(prepared=prepared, thread_id="tui-bg-thread", on_event=on_event)
    try:
        result = runner.invoke("run bg card")
        assert result.status == "completed"
        # Turn ended (run_completed applied), but the yielded execute card MUST remain running.
        card = next(
            b for b in state.blocks if isinstance(b, ToolBlock) and b.tool_call_id == "call-bg-card"
        )
        assert card.status == "running"
        assert card.exit_code is None
        assert isinstance(card.artifact, dict) and card.artifact["running"] is True

        # Turn-level settle events must never clobber a live background execute card.
        state.apply(RunEvent(type="interaction_requested", result=RunResult(status="waiting_human")))
        assert card.status == "running"
        state.apply(RunEvent(type="run_cancelled"))
        assert card.status == "running"
        state.apply(RunEvent(type="run_failed", content="subsequent turn failed"))
        assert card.status == "running"

        # Wait for the real execution_finished RunEvent from the pump to settle the card.
        _wait_until(lambda: card.status != "running", timeout=5.0)
        assert card.status == "completed"
        assert card.is_error is False
        assert card.exit_code == 0
        assert card.artifact["running"] is False
        assert "step2" in card.output

        # Stale running tool_completed arriving after finish must never overwrite terminal state.
        state.apply(RunEvent(
            type="tool_completed", tool_call_id="call-bg-card", name="execute",
            content="[running execution_id=stale]",
            artifact={"running": True, "execution_id": card.artifact["execution_id"], "exit_code": None},
        ))
        assert card.status == "completed"
        assert card.exit_code == 0
        assert card.artifact["running"] is False

        # write_stdin must never create a visible card.
        before = len(state.blocks)
        state.apply(RunEvent(
            type="tool_started", tool_call_id="stdin-hidden", name="write_stdin",
            arguments={"execution_id": card.artifact["execution_id"], "chars": ""},
        ))
        state.apply(RunEvent(
            type="tool_completed", tool_call_id="stdin-hidden", name="write_stdin",
            content="", artifact={"running": False, "execution_id": card.artifact["execution_id"], "exit_code": 0},
        ))
        assert len(state.blocks) == before
    finally:
        runner.close()


def test_session_switch_isolates_background_output_and_restores_state(
    workspace: Path, tmp_path: Path,
) -> None:
    try:
        selected = select_backend(_bwrap_config(workspace))
    except SandboxUnavailableError as exc:
        pytest.skip(str(exc))
    backend = selected.backend
    assert isinstance(backend, WorkspaceCompositeBackend)

    store = SessionStore(tmp_path / "sessions.sqlite")
    state = CliState()
    events: list[RunEvent] = []

    def on_event(event: RunEvent) -> None:
        events.append(event)
        state.apply(event)

    prepared = create_agent(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "name": "execute",
                "id": "call-sess-a",
                "args": {"command": "sleep 0.45; printf 'ONLY-IN-SESSION-A\\n'", "yield_time": 0.1, "network": False},
            }]),
            AIMessage(content="session A started bg"),
        ]),
        backend=backend,
        backend_selection=selected,
        checkpointer=store.checkpointer,
        skills=[],
        permission_mode=PermissionMode.ALLOW,
    )
    runner = AgentRunner(prepared=prepared, session_store=store, on_event=on_event)
    try:
        session_a = runner.thread_id
        state.session_id = session_a
        first = runner.invoke("start bg in session A")
        assert first.status == "completed"
        card_a = next(
            b for b in state.blocks if isinstance(b, ToolBlock) and b.tool_call_id == "call-sess-a"
        )
        assert card_a.status == "running"
        exec_id_a = str(card_a.artifact["execution_id"])

        # Switch to Session B while Session A's process is still running.
        info_b = runner.new_session()
        state.clear()
        state.session_id = info_b.id
        state.apply(RunEvent(
            type="tool_started", tool_call_id="call-sess-b", name="execute",
            arguments={"command": "echo b"},
        ))
        card_b = next(
            b for b in state.blocks if isinstance(b, ToolBlock) and b.tool_call_id == "call-sess-b"
        )

        # Wait for Session A's background execution to finish in the background.
        session_obj = backend.executor.pool.executions.get(exec_id_a)
        assert session_obj is not None
        _wait_until(lambda: session_obj.is_done, timeout=5.0)
        time.sleep(0.1)

        # Session B must NOT receive any output or extra cards from Session A.
        assert "ONLY-IN-SESSION-A" not in card_b.output
        assert [b.tool_call_id for b in state.blocks if isinstance(b, ToolBlock)] == ["call-sess-b"]

        # Switch back to Session A and reconcile against live ExecutionRegistry.
        snap_a = runner.switch_session(session_a)
        state.load_transcript(snap_a.transcript)
        state.restore_executions(backend.executor.pool.executions, session_a)
        restored_a = next(
            b for b in state.blocks if isinstance(b, ToolBlock) and b.tool_call_id == "call-sess-a"
        )
        assert restored_a.status == "completed"
        assert restored_a.exit_code == 0
        assert restored_a.artifact["running"] is False
        assert "ONLY-IN-SESSION-A" in restored_a.output

        # Simulate CLI restart (no live registry record): must show interrupted, never Running.
        restarted_state = CliState()
        restarted_state.load_transcript(snap_a.transcript)
        restarted_state.restore_executions(None, session_a)
        restarted_a = next(
            b for b in restarted_state.blocks if isinstance(b, ToolBlock) and b.tool_call_id == "call-sess-a"
        )
        assert restarted_a.status == "interrupted"
        assert restarted_a.artifact["running"] is False
    finally:
        runner.close()
        store.close()
