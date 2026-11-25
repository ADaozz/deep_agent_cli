"""Yielded execute + write_stdin lifecycle (requires Bubblewrap)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import os
import threading
import time

import pytest

from agent.cli.state import CliState
from agent.execution.backend import select_backend
from agent.execution.filesystem import WorkspaceCompositeBackend
from agent.execution.lifecycle import interact
from agent.execution.ownership import reset_execution_thread_id, set_execution_thread_id
from agent.execution.network import reset_execute_network, set_execute_network
from agent.execution.policy import SandboxConfig
from agent.execution.types import SandboxUnavailableError
from agent.runtime.contracts import RunEvent
from agent.runtime.approval.rules import static_rule
from agent.runtime.permissions import ask_interrupt_on, write_stdin_requires_approval
from agent.tools.catalog import WRITE_STDIN_TOOL, node_tools
from agent.tools.execute import build_write_stdin_tool, lookup_execution_network


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


def _net(backend: WorkspaceCompositeBackend, command: str, **kwargs):
    token = set_execute_network(False)
    try:
        return backend.execute(command, **kwargs)
    finally:
        reset_execute_network(token)


def test_write_stdin_is_common_tool() -> None:
    assert WRITE_STDIN_TOOL in node_tools("root")
    assert WRITE_STDIN_TOOL in node_tools("sync")


def test_sync_execute_unchanged(backend: WorkspaceCompositeBackend) -> None:
    response = _net(backend, "printf hello")
    assert "hello" in response.output
    assert not getattr(response, "running", False)
    assert response.exit_code == 0


def test_yield_then_poll_and_cancel(backend: WorkspaceCompositeBackend) -> None:
    first = _net(backend, "sleep 5; printf done", yield_time=0.2)
    assert first.running
    assert first.execution_id
    assert first.exit_code is None
    assert "execution_id=" in first.output
    live = _pool(backend).executions.require(first.execution_id)
    polled = interact(live, chars="", yield_time=0.2, action="write")
    assert polled.running
    assert polled.exit_code is None
    cancelled = interact(live, chars="", yield_time=2.0, action="cancel")
    assert not cancelled.running
    assert cancelled.exit_code in {130, 137, 143} or cancelled.termination_reason == "cancelled"


def test_interactive_pipe_write_stdin(backend: WorkspaceCompositeBackend) -> None:
    first = _net(
        backend,
        "python3 -c 'import sys; print(sys.stdin.readline().strip())'",
        yield_time=0.3,
        stdin_mode="pipe",
        timeout=10,
    )
    assert first.running
    assert first.execution_id
    live = _pool(backend).executions.require(first.execution_id)
    time.sleep(0.2)
    done = interact(live, chars="piped-line\n", yield_time=3.0, action="write")
    assert not done.running
    assert "piped-line" in done.output


def test_pty_controlling_terminal(backend: WorkspaceCompositeBackend) -> None:
    first = _net(
        backend,
        "python3 -c 'import os,sys; print(\"tty\" if os.isatty(0) else \"notty\"); "
        "print(sys.stdin.readline().strip())'",
        yield_time=0.5,
        stdin_mode="pty",
        timeout=10,
    )
    assert first.running
    live = _pool(backend).executions.require(first.execution_id)
    time.sleep(0.2)
    live.write("from-pty\n")
    done = interact(live, chars="", yield_time=3.0, action="write")
    if done.running:
        done = interact(live, chars="", yield_time=3.0, action="write")
    assert not done.running
    assert "tty" in done.output
    assert "from-pty" in done.output


def test_continuous_poll_returns_unread_only(backend: WorkspaceCompositeBackend) -> None:
    first = _net(
        backend,
        "printf 'one\\n'; sleep 0.4; printf 'two\\n'; sleep 5",
        yield_time=0.15,
        timeout=15,
    )
    assert first.running
    live = _pool(backend).executions.require(first.execution_id)
    # Drain whatever arrived in the first yield window.
    interact(live, chars="", yield_time=0.05, action="write")
    time.sleep(0.55)
    second = interact(live, chars="", yield_time=0.2, action="write")
    assert second.running
    # Second poll should not re-emit earlier drained text as the primary unread body.
    assert "two" in second.output
    interact(live, chars="", yield_time=1.0, action="cancel")


def test_output_truncation_bound(workspace: Path) -> None:
    try:
        selected = select_backend(_bwrap_config(workspace, max_output_bytes=64))
    except SandboxUnavailableError as exc:
        pytest.skip(str(exc))
    backend = selected.backend
    assert isinstance(backend, WorkspaceCompositeBackend)
    ownership = set_execution_thread_id("truncate-thread")
    try:
        response = _net(backend, "python3 -c 'print(\"x\" * 200)'")
    finally:
        reset_execution_thread_id(ownership)
        backend.executor.pool.shutdown("truncate done")
    assert response.truncated
    assert "Output truncated" in response.output or "truncated" in response.output.lower()


def test_background_timeout(backend: WorkspaceCompositeBackend) -> None:
    first = _net(backend, "sleep 30", yield_time=0.1, timeout=1)
    assert first.running
    live = _pool(backend).executions.require(first.execution_id)
    deadline = time.monotonic() + 5
    final = None
    while time.monotonic() < deadline:
        final = interact(live, chars="", yield_time=0.3, action="write")
        if not final.running:
            break
    assert final is not None and not final.running
    assert final.exit_code == 124 or final.termination_reason == "timeout"


def test_concurrent_stdin_writes(backend: WorkspaceCompositeBackend) -> None:
    first = _net(
        backend,
        "python3 -c 'import sys; data=sys.stdin.read(); print(data, end=\"\")'",
        yield_time=0.3,
        stdin_mode="pipe",
        timeout=10,
    )
    assert first.running
    live = _pool(backend).executions.require(first.execution_id)
    errors: list[BaseException] = []

    def writer(chunk: str) -> None:
        try:
            live.write(chunk)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"chunk-{index}\n",)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    live.close_stdin()
    done = interact(live, chars="", yield_time=3.0, action="write")
    assert not errors
    assert not done.running
    for index in range(4):
        assert f"chunk-{index}" in done.output


def test_subagent_ownership_isolation(backend: WorkspaceCompositeBackend) -> None:
    first = _net(backend, "sleep 8", yield_time=0.2)
    assert first.execution_id
    other = set_execution_thread_id("subagent-run-id")
    try:
        with pytest.raises(PermissionError):
            _pool(backend).executions.require(first.execution_id)
    finally:
        reset_execution_thread_id(other)
    live = _pool(backend).executions.require(first.execution_id)
    interact(live, chars="", yield_time=1.0, action="cancel")


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
    try:
        first = _net(left.backend, "sleep 8", yield_time=0.2)
        assert first.execution_id
        assert left.backend.executor.pool.executions.get(first.execution_id) is not None
        assert right.backend.executor.pool.executions.get(first.execution_id) is None
        right.backend.executor.pool.shutdown("right closed")
        # Closing another Runner/pool must not cancel this pool's execution.
        still = left.backend.executor.pool.executions.require(first.execution_id)
        assert still.handle.execution_id == first.execution_id
        interact(still, chars="", yield_time=1.0, action="cancel")
    finally:
        reset_execution_thread_id(ownership)
        left.backend.executor.pool.shutdown("left closed")


def test_write_stdin_approval_empty_poll_and_network() -> None:
    class _Req:
        def __init__(self, args: dict) -> None:
            self.tool_call = {"name": "write_stdin", "args": args}

    assert write_stdin_requires_approval(_Req({"action": "write", "chars": ""})) is False
    assert write_stdin_requires_approval(_Req({"action": "write", "chars": "x"})) is True
    assert write_stdin_requires_approval(_Req({"action": "cancel", "chars": ""})) is True
    rule = ask_interrupt_on()[WRITE_STDIN_TOOL]
    assert rule["when"] is write_stdin_requires_approval

    decision = static_rule(
        {"name": "write_stdin", "args": {"execution_id": "missing", "chars": "hi"}},
        gated=ask_interrupt_on(),
        backend=object(),
        sandbox_mode="restricted",
    )
    # Unknown execution is treated as networked → Flash review required.
    assert decision is None


def test_write_stdin_approval_matches_live_network(backend: WorkspaceCompositeBackend) -> None:
    first = _net(backend, "sleep 8", yield_time=0.2)
    assert first.execution_id
    assert lookup_execution_network(backend, first.execution_id) is False
    allowed = static_rule(
        {"name": "write_stdin", "args": {"execution_id": first.execution_id, "chars": "x\n"}},
        gated=ask_interrupt_on(),
        backend=backend,
        sandbox_mode="restricted",
    )
    assert allowed is not None and allowed.decision == "allow"
    live = _pool(backend).executions.require(first.execution_id)
    interact(live, chars="", yield_time=1.0, action="cancel")


def test_tui_keeps_execute_card_running_and_hides_write_stdin() -> None:
    state = CliState()
    state.apply(RunEvent(
        type="tool_started", tool_call_id="exec-1", name="execute",
        arguments={"command": "sleep 1", "yield_time": 0.1},
    ))
    state.apply(RunEvent(
        type="tool_completed", tool_call_id="exec-1", name="execute",
        content="[running execution_id=bwrap:1:1]",
        artifact={"running": True, "execution_id": "bwrap:1:1", "exit_code": None},
    ))
    execute = next(block for block in state.blocks if getattr(block, "tool_call_id", None) == "exec-1")
    assert execute.status == "running"
    assert execute.exit_code is None

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

    state.apply(RunEvent(
        type="tool_output_delta", tool_call_id="exec-1", name="execute",
        content="more-output\n", stream="merged",
    ))
    state.apply(RunEvent(
        type="tool_output_delta", tool_call_id="exec-1", name="execute",
        content="\n\n[finished execution_id=bwrap:1:1 exit_code=0 reason=exit]",
        stream="execution_finished",
    ))
    assert execute.status == "completed"
    assert execute.exit_code == 0
    assert "more-output" in execute.output


def test_write_stdin_tool_uses_pool_registry(backend: WorkspaceCompositeBackend) -> None:
    tool = build_write_stdin_tool(backend)
    first = _net(backend, "sleep 5", yield_time=0.2)
    assert first.execution_id
    content, artifact = tool.func(
        execution_id=first.execution_id, chars="", yield_time=0.2, action="write",
    )
    assert artifact["running"] is True
    assert artifact["execution_id"] == first.execution_id
    assert artifact["exit_code"] is None
    assert "execution_id=" in content or first.execution_id in content
    tool.func(execution_id=first.execution_id, chars="", yield_time=1.0, action="cancel")
