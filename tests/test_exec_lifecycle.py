"""Yielded execute + write_stdin lifecycle (requires Bubblewrap)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import os
import threading
import time

import pytest
from langchain_core.messages import AIMessage

from agent.cli.state import CliState
from agent.execution.backend import select_backend
from agent.execution.filesystem import WorkspaceCompositeBackend
from agent.execution.lifecycle import write_stdin_is_side_effect
from agent.execution.ownership import reset_execution_thread_id, set_execution_thread_id
from agent.execution.policy import SandboxConfig
from agent.execution.types import SandboxUnavailableError
from agent.runtime.contracts import RunEvent
from agent.runtime.approval.rules import static_rule
from agent.runtime.cancellation import set_output_emitter
from agent.runtime.permissions import ask_interrupt_on, write_stdin_requires_approval
from agent.tools.catalog import WRITE_STDIN_TOOL, node_tools
from agent.tools.execute import build_execute_tool, build_write_stdin_tool, lookup_execution_network


pytestmark = pytest.mark.sandbox


def _bwrap_config(workspace: Path, *, timeout_seconds: int = 30, max_output_bytes: int = 1_000_000) -> SandboxConfig:
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


def _pool(backend: WorkspaceCompositeBackend):
    return backend.executor.pool


def test_write_stdin_is_common_tool() -> None:
    assert WRITE_STDIN_TOOL in node_tools("root")
    assert WRITE_STDIN_TOOL in node_tools("sync")


def test_sync_execute_via_tool(backend: WorkspaceCompositeBackend) -> None:
    tool = build_execute_tool(backend)
    content, artifact = tool.func(command="printf hello", network=False)
    assert "hello" in content
    assert artifact["running"] is False
    assert artifact["exit_code"] == 0


def test_yield_poll_cancel_via_tools(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    content, artifact = execute.func(
        command="sleep 5; printf done", network=False, yield_time=0.2,
    )
    assert artifact["running"] is True
    assert artifact["execution_id"]
    assert artifact["exit_code"] is None
    assert "execution_id=" in content
    execution_id = str(artifact["execution_id"])
    polled, poll_art = write.func(execution_id=execution_id, chars="", yield_time=0.2)
    assert poll_art["running"] is True
    assert poll_art["exit_code"] is None
    cancelled, cancel_art = write.func(execution_id=execution_id, chars="", yield_time=2.0, action="cancel")
    assert cancel_art["running"] is False
    assert cancel_art["exit_code"] in {130, 137, 143} or cancel_art.get("termination_reason") == "cancelled"
    assert "Cancelled" in cancelled or cancel_art.get("termination_reason") == "cancelled"


def test_interactive_pipe_via_tools(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    _content, artifact = execute.func(
        command="python3 -c 'import sys; print(sys.stdin.readline().strip())'",
        network=False, yield_time=0.3, interactive=True, timeout=10,
    )
    assert artifact["running"] is True
    execution_id = str(artifact["execution_id"])
    time.sleep(0.2)
    done, done_art = write.func(execution_id=execution_id, chars="piped-line\n", yield_time=3.0)
    assert done_art["running"] is False
    assert "piped-line" in done


def test_pty_dev_tty_and_foreground_group(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    command = (
        "python3 -c '"
        "import os,sys;\n"
        "line=sys.stdin.readline().strip();\n"
        "print(\"tty\" if os.isatty(0) else \"notty\");\n"
        "print(\"devtty\" if os.path.exists(\"/dev/tty\") else \"nodevtty\");\n"
        "print(\"fg\" if os.tcgetpgrp(0)==os.getpgrp() else \"bg\");\n"
        "print(line)\n"
        "'"
    )
    _content, artifact = execute.func(
        command=command, network=False, yield_time=0.5, tty=True, timeout=10,
    )
    assert artifact["running"] is True
    execution_id = str(artifact["execution_id"])
    time.sleep(0.2)
    done, done_art = write.func(execution_id=execution_id, chars="from-pty\n", yield_time=3.0)
    if done_art["running"]:
        done, done_art = write.func(execution_id=execution_id, chars="", yield_time=3.0)
    assert done_art["running"] is False
    assert "tty" in done
    assert "devtty" in done
    assert "fg" in done
    assert "from-pty" in done


def test_pty_ctrl_c_interrupts_foreground(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    _content, artifact = execute.func(
        command="python3 -c 'import time; time.sleep(30)'",
        network=False, yield_time=0.4, tty=True, timeout=15,
    )
    assert artifact["running"] is True
    execution_id = str(artifact["execution_id"])
    time.sleep(0.2)
    done, done_art = write.func(execution_id=execution_id, chars="\x03", yield_time=3.0)
    if done_art["running"]:
        done, done_art = write.func(execution_id=execution_id, chars="", yield_time=2.0, action="cancel")
    assert done_art["running"] is False
    assert done_art["exit_code"] not in (None,) and done_art["exit_code"] != 0


def test_retained_terminal_result_after_finish(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    _content, artifact = execute.func(
        command="printf 'early\\n'; sleep 0.3; printf 'late\\n'",
        network=False, yield_time=0.15, timeout=10,
    )
    assert artifact["running"] is True
    execution_id = str(artifact["execution_id"])
    deadline = time.monotonic() + 5
    final = None
    while time.monotonic() < deadline:
        text, art = write.func(execution_id=execution_id, chars="", yield_time=0.3)
        final = (text, art)
        if not art["running"]:
            break
    assert final is not None
    text, art = final
    assert art["running"] is False
    assert art["exit_code"] == 0
    # Final poll returns unread output + exit info, not a fabricated full replay requirement.
    assert "Exit code: 0" in text or art["exit_code"] == 0
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
        _content, artifact = execute.func(
            command="python3 -c 'print(\"A\"*80); import time; time.sleep(0.4); print(\"B\"*20)'",
            network=False, yield_time=0.2, timeout=10,
        )
        assert artifact["running"] is True
        execution_id = str(artifact["execution_id"])
        time.sleep(0.6)
        text, art = write.func(execution_id=execution_id, chars="", yield_time=1.0)
        if art["running"]:
            text, art = write.func(execution_id=execution_id, chars="", yield_time=2.0)
        assert art["running"] is False
        assert art.get("truncated") is True or "B" in text or "truncated" in text.lower()
    finally:
        reset_execution_thread_id(ownership)
        backend.executor.pool.shutdown("truncate done")


def test_background_timeout_via_tools(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    _content, artifact = execute.func(command="sleep 30", network=False, yield_time=0.1, timeout=1)
    assert artifact["running"] is True
    execution_id = str(artifact["execution_id"])
    deadline = time.monotonic() + 5
    final = None
    while time.monotonic() < deadline:
        _text, art = write.func(execution_id=execution_id, chars="", yield_time=0.3)
        final = art
        if not art["running"]:
            break
    assert final is not None and final["running"] is False
    assert final["exit_code"] == 124 or final.get("termination_reason") == "timeout"


def test_concurrent_stdin_writes_via_tools(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    _content, artifact = execute.func(
        command="python3 -c 'import sys; data=sys.stdin.read(); print(data, end=\"\")'",
        network=False, yield_time=0.3, interactive=True, timeout=10,
    )
    execution_id = str(artifact["execution_id"])
    errors: list[BaseException] = []
    results: list[str] = []

    def writer(chunk: str) -> None:
        token = set_execution_thread_id("test-thread")
        try:
            text, _art = write.func(execution_id=execution_id, chars=chunk, yield_time=0.05)
            results.append(text)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            reset_execution_thread_id(token)

    threads = [threading.Thread(target=writer, args=(f"chunk-{index}\n",)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not errors
    done, done_art = write.func(execution_id=execution_id, chars="", yield_time=0.2, action="close")
    if done_art["running"]:
        done, done_art = write.func(execution_id=execution_id, chars="", yield_time=3.0)
    assert done_art["running"] is False
    for index in range(4):
        assert f"chunk-{index}" in done


def test_subagent_ownership_isolation(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    _content, artifact = execute.func(command="sleep 8", network=False, yield_time=0.2)
    execution_id = str(artifact["execution_id"])
    other = set_execution_thread_id("subagent-run-id")
    try:
        with pytest.raises(PermissionError):
            write.func(execution_id=execution_id, chars="", yield_time=0.1)
    finally:
        reset_execution_thread_id(other)
    write.func(execution_id=execution_id, chars="", yield_time=1.0, action="cancel")


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
        _content, artifact = execute.func(command="sleep 8", network=False, yield_time=0.2)
        execution_id = str(artifact["execution_id"])
        assert left.backend.executor.pool.executions.get(execution_id) is not None
        assert right.backend.executor.pool.executions.get(execution_id) is None
        right.backend.executor.pool.shutdown("right closed")
        write.func(execution_id=execution_id, chars="", yield_time=1.0, action="cancel")
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
    _content, artifact = execute.func(command="sleep 8", network=False, yield_time=0.2)
    execution_id = str(artifact["execution_id"])
    assert lookup_execution_network(backend, execution_id) is False
    allowed = static_rule(
        {"name": "write_stdin", "args": {"execution_id": execution_id, "chars": "x\n"}},
        gated=ask_interrupt_on(),
        backend=backend,
        sandbox_mode="restricted",
    )
    assert allowed is not None and allowed.decision == "allow"
    write.func(execution_id=execution_id, chars="", yield_time=1.0, action="cancel")


def test_tui_structured_finish_and_race_with_running_complete() -> None:
    state = CliState()
    events: list[RunEvent] = []

    def capture(tool_call_id: str, content: str = "", stream: str = "merged", *, result=None, name: str = "") -> None:
        events.append(RunEvent(
            type="tool_output_delta", tool_call_id=tool_call_id, content=content,
            stream=stream, result=result, name=name or "execute",
        ))
        state.apply(events[-1])

    set_output_emitter(capture)
    try:
        state.apply(RunEvent(
            type="tool_started", tool_call_id="exec-1", name="execute",
            arguments={"command": "sleep 1", "yield_time": 0.1},
        ))
        # Finish arrives before yielded tool_completed.
        capture(
            "exec-1", "", stream="execution_finished",
            result={"execution_id": "bwrap:1:1", "running": False, "exit_code": 0, "termination_reason": None},
        )
        execute = next(block for block in state.blocks if getattr(block, "tool_call_id", None) == "exec-1")
        assert execute.status == "completed"
        assert execute.exit_code == 0
        state.apply(RunEvent(
            type="tool_completed", tool_call_id="exec-1", name="execute",
            content="[running execution_id=bwrap:1:1]",
            artifact={"running": True, "execution_id": "bwrap:1:1", "exit_code": None},
        ))
        assert execute.status == "completed"
        assert execute.exit_code == 0

        before = len(state.blocks)
        state.apply(RunEvent(
            type="tool_started", tool_call_id="stdin-1", name="write_stdin",
            arguments={"execution_id": "bwrap:1:1", "chars": ""},
        ))
        state.apply(RunEvent(
            type="tool_completed", tool_call_id="stdin-1", name="write_stdin",
            content="poll", artifact={"running": True, "execution_id": "bwrap:1:1"},
        ))
        assert len(state.blocks) == before
    finally:
        set_output_emitter(None)


def test_prestart_write_acks_only_after_drain(backend: WorkspaceCompositeBackend) -> None:
    execute = build_execute_tool(backend)
    write = build_write_stdin_tool(backend)
    _content, artifact = execute.func(
        command="python3 -c 'import sys; print(sys.stdin.read(), end=\"\")'",
        network=False, yield_time=0.05, interactive=True, timeout=10,
    )
    execution_id = str(artifact["execution_id"])
    # Write immediately after yield; worker must not ACK success until bytes are delivered.
    _out, _art = write.func(execution_id=execution_id, chars="before-or-after\n", yield_time=0.05)
    done, done_art = write.func(execution_id=execution_id, chars="", yield_time=0.2, action="close")
    if done_art["running"]:
        done, done_art = write.func(execution_id=execution_id, chars="", yield_time=3.0)
    assert done_art["running"] is False
    assert "before-or-after" in done


def test_runtime_execute_yield_emits_structured_finish(workspace: Path) -> None:
    """Drive execute through AgentRunner so emitter/TUI wiring matches production."""
    from agent.bootstrap.factory import create_agent
    from agent.runtime.permissions import PermissionMode
    from agent.runtime.runner import AgentRunner
    from tests.conftest import scripted_model

    try:
        selected = select_backend(_bwrap_config(workspace))
    except SandboxUnavailableError as exc:
        pytest.skip(str(exc))
    backend = selected.backend
    assert isinstance(backend, WorkspaceCompositeBackend)

    ownership = set_execution_thread_id("runtime-thread")
    events: list[RunEvent] = []
    prepared = create_agent(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "name": "execute",
                "id": "call-yield",
                "args": {"command": "printf hi; sleep 0.2", "yield_time": 0.05, "network": False},
            }]),
            AIMessage(content="done"),
        ]),
        backend=backend,
        backend_selection=selected,
        skills=[],
        permission_mode=PermissionMode.ALLOW,
    )
    runner = AgentRunner(prepared=prepared, thread_id="exec-yield-runtime")
    try:
        result = runner.invoke("run yield", on_event=events.append)
        assert result.status == "completed"
        completed = [
            event for event in events
            if event.type == "tool_completed" and event.name == "execute"
        ]
        assert completed
        assert isinstance(completed[0].artifact, dict)
        assert completed[0].artifact.get("execution_id")
        # Allow background finish to arrive shortly after the tool returns running.
        deadline = time.monotonic() + 3
        finished = [
            event for event in events
            if event.type == "tool_output_delta" and event.stream == "execution_finished"
        ]
        while not finished and time.monotonic() < deadline:
            time.sleep(0.05)
            finished = [
                event for event in events
                if event.type == "tool_output_delta" and event.stream == "execution_finished"
            ]
        assert finished, "expected structured execution_finished RunEvent"
        assert isinstance(finished[-1].result, dict)
        assert finished[-1].result.get("running") is False
        assert finished[-1].result.get("execution_id")
    finally:
        reset_execution_thread_id(ownership)
        runner.close()
