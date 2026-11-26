"""Consistency tests for execution output projection, truncation, restore, and capabilities."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol
from langchain_core.messages import AIMessage, ToolMessage
import pytest

from agent.cli import rendering
from agent.cli.display import UiDisplayLimits
from agent.cli.rendering import TranscriptRenderer
from agent.cli.state import CliState, ToolBlock
from agent.execution.filesystem import WorkspaceCompositeBackend
from agent.execution.lifecycle import CommandSession, ExecutionRegistry, interact
from agent.execution.policy import SandboxConfig
from agent.execution.ports import interactive_backend, supports_interactive_execution
from agent.execution.process import _OutputCollector, execution_artifact_from_response
from agent.runtime.contracts import RunEvent
from agent.sessions.transcript import messages_to_transcript, project_execution_update


@pytest.fixture(autouse=True)
def _pin_spinner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rendering, "_spinner_tick", lambda: 4)


def _render_lines(state: CliState, width: int = 120) -> list[str]:
    renderer = TranscriptRenderer(limits=UiDisplayLimits(), show_header=False)
    fragments, _ = renderer.render(state, width)
    return [line.rstrip() for line in "".join(part for _, part in fragments).splitlines()]


class _FakeSandbox:
    id = "sbx-proj"

    def defer_redaction_release(self, cb: Any) -> None:
        cb()

    def try_reclaim_secret_taint(self) -> bool:
        return True


class _FakePool:
    def discard(self, _sandbox: Any, _reason: str) -> None:
        pass


class _ScriptedHandle:
    """Thread-safe scripted RemoteCommand handle for deterministic lifecycle tests."""

    def __init__(self, execution_id: str, events: list[tuple[str, Any]], *, stdin_mode: str = "pipe") -> None:
        import queue

        self.execution_id = execution_id
        self.stdin_mode = stdin_mode
        self.sandbox = _FakeSandbox()
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.written: list[bytes] = []
        self.killed = False
        self.released = False
        for item in events:
            self._queue.put(item)

    def push(self, kind: str, payload: Any) -> None:
        self._queue.put((kind, payload))

    def next_event(self, timeout: float | None = None) -> tuple[str, Any] | None:
        import queue

        try:
            return self._queue.get(timeout=0.02 if timeout is None else max(0.0, timeout))
        except queue.Empty:
            return None

    def write_stdin(self, data: bytes, *, timeout: float = 15.0) -> None:
        self.written.append(data)

    def close_stdin(self) -> None:
        pass

    def resize(self, rows: int, cols: int) -> None:
        pass

    def interrupt(self, *, timeout: float = 15.0) -> None:
        self.killed = True

    def kill(self) -> None:
        self.killed = True
        self._queue.put(("exit", 130))

    def release(self) -> None:
        self.released = True


# ---------------------------------------------------------------------------
# 1 & 2. Long output exceeding max_output_bytes & UTF-8 multibyte truncation
# ---------------------------------------------------------------------------


def test_output_collector_and_projection_bound_max_output_bytes_and_utf8(tmp_path: Path) -> None:
    emitted: list[tuple[str, str]] = []

    def emit(
        tool_call_id: str,
        content: str,
        stream: str = "merged",
        *,
        result: Any = None,
        name: str = "execute",
        thread_id: str = "",
        execution_id: str = "",
    ) -> None:
        emitted.append((stream, content))

    collector = _OutputCollector(
        tool_call_id="c-utf8",
        max_output_bytes=20,
        workspace_root=tmp_path,
        emit=emit,
        execution_id="exec-utf8",
        thread_id="t1",
    )
    # Mix 3-byte CJK ('中' = 3 bytes) and 4-byte emoji ('🚀' = 4 bytes) split mid-sequence.
    payload = "前奏-中文输出🚀结尾数据-最终行\n".encode("utf-8")
    assert len(payload) > 20
    # Feed byte-by-byte / awkward chunks so incremental decoding and head clipping are exercised.
    for chunk in (payload[:5], payload[5:13], payload[13:23], payload[23:]):
        collector.append(chunk)
    collector.flush()
    collector.close()

    assert collector.truncated is True
    tail = collector.tail_text()
    assert "\ufffd" not in tail
    assert len(tail.encode("utf-8")) <= 20
    assert tail.endswith("最终行\n")

    # Every emitted delta / tail_snapshot must also be valid UTF-8 without U+FFFD corruption.
    assert any(stream == "tail_snapshot" for stream, _ in emitted)
    for _stream, text in emitted:
        assert "\ufffd" not in text

    # Full uncorrupted log must be written to disk.
    meta = collector.log_metadata()
    assert meta["truncated"] is True
    assert meta["host_log_path"] is not None
    assert Path(meta["host_log_path"]).read_bytes() == payload

    # project_execution_update enforces the exact same UTF-8 boundary across every byte offset.
    full_text = "甲乙丙丁🚀戊己庚辛"
    for cap in range(1, len(full_text.encode("utf-8")) + 2):
        projected = project_execution_update(
            current_output="",
            current_artifact={},
            current_status="running",
            update_artifact={"execution_id": "e1", "running": True, "max_output_bytes": cap},
            output_delta=full_text,
            output_mode="append",
        )
        assert projected is not None
        out, artifact, status, is_error, exit_code = projected
        assert "\ufffd" not in out
        assert len(out.encode("utf-8")) <= cap
        assert status == "running" and is_error is False and exit_code is None
        if len(full_text.encode("utf-8")) > cap:
            assert artifact["truncated"] is True
            assert full_text.endswith(out)


# ---------------------------------------------------------------------------
# 3. Continuous live deltas + tail_snapshot replacement
# ---------------------------------------------------------------------------


def test_live_continuous_deltas_and_tail_snapshot_replacement(tmp_path: Path) -> None:
    state = CliState(session_id="s-live")
    state.apply(RunEvent(type="tool_started", tool_call_id="c-snap", name="execute", arguments={"command": "seq 1 50"}))

    def emit(
        tool_call_id: str,
        content: str,
        stream: str = "merged",
        *,
        result: Any = None,
        name: str = "execute",
        thread_id: str = "",
        execution_id: str = "",
    ) -> None:
        payload = dict(result) if isinstance(result, dict) else {}
        if thread_id:
            payload.setdefault("thread_id", thread_id)
        if execution_id:
            payload.setdefault("execution_id", execution_id)
        state.apply(RunEvent(
            type="tool_output_delta",
            tool_call_id=tool_call_id,
            name=name,
            content=content,
            stream=stream,
            result=payload or None,
        ))

    collector = _OutputCollector(
        tool_call_id="c-snap",
        max_output_bytes=24,
        workspace_root=tmp_path,
        emit=emit,
        execution_id="exec-snap",
        thread_id="s-live",
    )
    for i in range(1, 10):
        collector.append(f"line-{i:02d}\n".encode("utf-8"))
    collector.flush()
    collector.close()

    tool = state.blocks[0]
    assert isinstance(tool, ToolBlock)
    assert tool.output == collector.tail_text()
    assert len(tool.output.encode("utf-8")) <= 24
    assert "line-09\n" in tool.output
    assert "line-01\n" not in tool.output
    assert isinstance(tool.artifact, dict) and tool.artifact.get("truncated") is True

    meta = collector.log_metadata()
    state.apply(RunEvent(
        type="tool_output_delta",
        tool_call_id="c-snap",
        name="execute",
        content="",
        stream="execution_finished",
        result={
            "execution_id": "exec-snap",
            "running": False,
            "exit_code": 0,
            "thread_id": "s-live",
            **meta,
        },
    ))
    assert tool.status == "completed"
    assert tool.output == collector.tail_text()
    assert tool.artifact["agent_log_path"] == meta["agent_log_path"]


# ---------------------------------------------------------------------------
# 4. Multiple write_stdin polls
# ---------------------------------------------------------------------------


def test_multiple_write_stdin_polls_accumulate_and_settle_consistently(tmp_path: Path) -> None:
    live = CliState(session_id="s-poll")
    live.apply(RunEvent(
        type="tool_started",
        tool_call_id="c-ex",
        name="execute",
        arguments={"command": "repl", "interactive": True},
    ))

    def emit(
        tool_call_id: str,
        content: str,
        stream: str = "merged",
        *,
        result: Any = None,
        name: str = "execute",
        thread_id: str = "",
        execution_id: str = "",
    ) -> None:
        payload = dict(result) if isinstance(result, dict) else {}
        if thread_id:
            payload.setdefault("thread_id", thread_id)
        if execution_id:
            payload.setdefault("execution_id", execution_id)
        live.apply(RunEvent(
            type="tool_output_delta",
            tool_call_id=tool_call_id,
            name=name,
            content=content,
            stream=stream,
            result=payload or None,
        ))

    handle = _ScriptedHandle("exec-poll", [("out", b"boot\n")], stdin_mode="pipe")
    collector = _OutputCollector(
        tool_call_id="c-ex",
        max_output_bytes=4096,
        workspace_root=tmp_path,
        emit=emit,
        execution_id="exec-poll",
        thread_id="s-poll",
    )
    registry = ExecutionRegistry()
    session = CommandSession(
        handle=handle,
        pool=_FakePool(),  # type: ignore[arg-type]
        collector=collector,
        thread_id="s-poll",
        timeout=30,
        deadline=None,
        network=False,
        tool_call_id="c-ex",
        emit=emit,
    )
    registry.register(session)
    try:
        r0 = session.wait_until(0.05 + __import__("time").monotonic())
        assert r0.running is True
        a0 = execution_artifact_from_response(r0)
        live.apply(RunEvent(type="tool_completed", tool_call_id="c-ex", name="execute", content=r0.output, artifact=a0))

        # Poll 1
        handle.push("out", b"step-1\n")
        r1 = interact(session, chars="cmd1\n", yield_time=0.05)
        assert r1.running is True
        a1 = execution_artifact_from_response(r1)
        live.apply(RunEvent(type="tool_started", tool_call_id="c-ws1", name="write_stdin", arguments={"execution_id": "exec-poll", "chars": "cmd1\n"}))
        live.apply(RunEvent(type="tool_completed", tool_call_id="c-ws1", name="write_stdin", content=r1.output, artifact=a1))

        # Poll 2
        handle.push("out", b"step-2\n")
        r2 = interact(session, chars="cmd2\n", yield_time=0.05)
        assert r2.running is True
        a2 = execution_artifact_from_response(r2)
        live.apply(RunEvent(type="tool_started", tool_call_id="c-ws2", name="write_stdin", arguments={"execution_id": "exec-poll", "chars": "cmd2\n"}))
        live.apply(RunEvent(type="tool_completed", tool_call_id="c-ws2", name="write_stdin", content=r2.output, artifact=a2))

        # Poll 3 terminates process
        handle.push("out", b"bye\n")
        handle.push("exit", 0)
        r3 = interact(session, chars="quit\n", yield_time=0.5)
        assert r3.running is False
        assert r3.exit_code == 0
        a3 = execution_artifact_from_response(r3)
        live.apply(RunEvent(type="tool_started", tool_call_id="c-ws3", name="write_stdin", arguments={"execution_id": "exec-poll", "chars": "quit\n"}))
        live.apply(RunEvent(type="tool_completed", tool_call_id="c-ws3", name="write_stdin", content=r3.output, artifact=a3))
    finally:
        session.join_pump(timeout=2.0)

    # History rebuild from the exact same persisted ToolMessages must match live rendering.
    messages = [
        AIMessage(content="", tool_calls=[{"id": "c-ex", "name": "execute", "args": {"command": "repl", "interactive": True}}]),
        ToolMessage(content=r0.output, tool_call_id="c-ex", name="execute", artifact=a0),
        AIMessage(content="", tool_calls=[{"id": "c-ws1", "name": "write_stdin", "args": {"execution_id": "exec-poll", "chars": "cmd1\n"}}]),
        ToolMessage(content=r1.output, tool_call_id="c-ws1", name="write_stdin", artifact=a1),
        AIMessage(content="", tool_calls=[{"id": "c-ws2", "name": "write_stdin", "args": {"execution_id": "exec-poll", "chars": "cmd2\n"}}]),
        ToolMessage(content=r2.output, tool_call_id="c-ws2", name="write_stdin", artifact=a2),
        AIMessage(content="", tool_calls=[{"id": "c-ws3", "name": "write_stdin", "args": {"execution_id": "exec-poll", "chars": "quit\n"}}]),
        ToolMessage(content=r3.output, tool_call_id="c-ws3", name="write_stdin", artifact=a3),
    ]
    restored = CliState()
    restored.load_transcript(messages_to_transcript(messages))

    assert len(live.blocks) == 1
    assert len(restored.blocks) == 1
    assert live.blocks[0].output == "boot\nstep-1\nstep-2\nbye\n"  # type: ignore[union-attr]
    assert _render_lines(live) == _render_lines(restored) == [
        "",
        "• Ran repl",
        "  └ output",
        "    … 1 earlier output lines hidden · Ctrl+O to expand",
        "    step-1",
        "    step-2",
        "    bye",
    ]


# ---------------------------------------------------------------------------
# 5 & 6. Terminal before execute return & confirmed terminal ignores late running=True
# ---------------------------------------------------------------------------


def test_terminal_before_execute_tool_return_ignores_late_running_update() -> None:
    state = CliState(session_id="s-race")
    state.apply(RunEvent(
        type="tool_started",
        tool_call_id="c-race",
        name="execute",
        arguments={"command": "fast-job", "interactive": True},
    ))
    state.apply(RunEvent(
        type="tool_output_delta",
        tool_call_id="c-race",
        name="execute",
        content="done-output\n",
        stream="merged",
        result={"execution_id": "exec-race", "thread_id": "s-race"},
    ))
    # Process finishes and emits execution_finished BEFORE execute tool_completed arrives.
    state.apply(RunEvent(
        type="tool_output_delta",
        tool_call_id="c-race",
        name="execute",
        content="",
        stream="execution_finished",
        result={
            "execution_id": "exec-race",
            "running": False,
            "exit_code": 0,
            "termination_reason": None,
            "thread_id": "s-race",
        },
    ))
    # Late execute tool_completed arrives carrying stale running=True from pre-exit yield.
    state.apply(RunEvent(
        type="tool_completed",
        tool_call_id="c-race",
        name="execute",
        content="done-output\n\n[running execution_id=exec-race]",
        artifact={"execution_id": "exec-race", "running": True, "output": "done-output\n"},
    ))
    tool = state.blocks[0]
    assert isinstance(tool, ToolBlock)
    assert tool.status == "completed"
    assert tool.exit_code == 0
    assert tool.artifact["running"] is False
    assert _render_lines(state) == [
        "",
        "• Ran fast-job",
        "  └ output",
        "    done-output",
    ]

    # History path also ignores a late running=True update after a confirmed terminal state.
    history_messages = [
        AIMessage(content="", tool_calls=[{"id": "c-h", "name": "execute", "args": {"command": "fast-job"}}]),
        ToolMessage(
            content="failed-body\n\nExit code: 2",
            tool_call_id="c-h",
            name="execute",
            artifact={"execution_id": "exec-h", "running": False, "exit_code": 2, "output": "failed-body\n"},
        ),
        AIMessage(content="", tool_calls=[{"id": "c-late", "name": "write_stdin", "args": {"execution_id": "exec-h", "chars": ""}}]),
        ToolMessage(
            content="<no new output>",
            tool_call_id="c-late",
            name="write_stdin",
            artifact={"execution_id": "exec-h", "running": True, "output": "stale"},
        ),
    ]
    restored = CliState()
    restored.load_transcript(messages_to_transcript(history_messages))
    h_tool = restored.blocks[0]
    assert isinstance(h_tool, ToolBlock)
    assert h_tool.status == "error"
    assert h_tool.is_error is True
    assert h_tool.exit_code == 2
    assert h_tool.artifact["running"] is False
    assert h_tool.output == "failed-body\n"


# ---------------------------------------------------------------------------
# 7. Live vs history parity for the same persisted execution artifacts
# ---------------------------------------------------------------------------


def test_persisted_execution_output_matches_between_live_and_history_paths() -> None:
    artifact_done = {
        "execution_id": "exec-eq",
        "running": False,
        "exit_code": 1,
        "termination_reason": None,
        "truncated": True,
        "agent_log_path": "/workspace/.deep-agent/logs/exec/eq.log",
        "max_output_bytes": 128,
        "output": "alpha\nbeta\ngamma\ndelta\n",
    }
    formatted_content = (
        "alpha\nbeta\ngamma\ndelta\n\n"
        "[Output truncated: showing the last 128 bytes.\n"
        "Full output saved to: /host/eq.log\n"
        "Agent path: /workspace/.deep-agent/logs/exec/eq.log]\n\n"
        "Exit code: 1"
    )

    live = CliState(session_id="s-eq")
    live.apply(RunEvent(type="tool_started", tool_call_id="c-eq", name="execute", arguments={"command": "pytest"}))
    live.apply(RunEvent(
        type="tool_completed",
        tool_call_id="c-eq",
        name="execute",
        content=formatted_content,
        artifact=artifact_done,
        is_error=True,
    ))

    restored = CliState()
    restored.load_transcript(messages_to_transcript([
        AIMessage(content="", tool_calls=[{"id": "c-eq", "name": "execute", "args": {"command": "pytest"}}]),
        ToolMessage(content=formatted_content, tool_call_id="c-eq", name="execute", artifact=artifact_done),
    ]))

    assert _render_lines(live) == _render_lines(restored) == [
        "",
        "• Failed (exit 1) pytest",
        "  └ output",
        "    … 1 earlier output lines hidden · Ctrl+O to expand",
        "    beta",
        "    gamma",
        "    delta",
        "  Output truncated · full output: /workspace/.deep-agent/logs/exec/eq.log",
    ]


# ---------------------------------------------------------------------------
# 8, 9, 10. Restore running, restore finished, and missing execution -> unconfirmed
# ---------------------------------------------------------------------------


def test_restore_executions_running_finished_and_missing_unconfirmed(tmp_path: Path) -> None:
    # Build two real CommandSessions: one still running, one already finished.
    run_handle = _ScriptedHandle("exec-still", [("out", b"still-alive\n")], stdin_mode="pipe")
    run_session = CommandSession(
        handle=run_handle,
        pool=_FakePool(),  # type: ignore[arg-type]
        collector=_OutputCollector(
            tool_call_id="c-run", max_output_bytes=1024, workspace_root=tmp_path,
            execution_id="exec-still", thread_id="s-restore",
        ),
        thread_id="s-restore", timeout=30, deadline=None, network=False, tool_call_id="c-run",
    )
    done_handle = _ScriptedHandle("exec-done", [("out", b"finished-err\n"), ("exit", 3)], stdin_mode="pipe")
    done_session = CommandSession(
        handle=done_handle,
        pool=_FakePool(),  # type: ignore[arg-type]
        collector=_OutputCollector(
            tool_call_id="c-done", max_output_bytes=1024, workspace_root=tmp_path,
            execution_id="exec-done", thread_id="s-restore",
        ),
        thread_id="s-restore", timeout=30, deadline=None, network=False, tool_call_id="c-done",
    )
    registry = ExecutionRegistry()
    registry.register(run_session)
    registry.register(done_session)
    try:
        # Wait until run_session reads its initial chunk and done_session exits.
        done_session.join_pump(timeout=2.0)
        import time
        deadline = time.monotonic() + 2.0
        while not run_session.collector.tail_text() and time.monotonic() < deadline:
            time.sleep(0.01)

        snapshots = {
            "exec-still": run_session.restore_snapshot(),
            "exec-done": done_session.restore_snapshot(),
            # exec-gone intentionally absent (e.g. CLI restarted).
        }
    finally:
        run_session.cancel()
        run_session.join_pump(timeout=2.0)

    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"id": "c-run", "name": "execute", "args": {"command": "tail -f app.log", "interactive": True}},
                {"id": "c-done", "name": "execute", "args": {"command": "make build"}},
                {"id": "c-gone", "name": "execute", "args": {"command": "sleep 60"}},
            ],
        ),
        ToolMessage(content="", tool_call_id="c-run", name="execute", artifact={"execution_id": "exec-still", "running": True, "output": ""}),
        ToolMessage(content="", tool_call_id="c-done", name="execute", artifact={"execution_id": "exec-done", "running": True, "output": ""}),
        ToolMessage(content="", tool_call_id="c-gone", name="execute", artifact={"execution_id": "exec-gone", "running": True, "output": "before-crash\n"}),
    ]
    restored = CliState()
    restored.load_transcript(messages_to_transcript(messages))
    restored.restore_executions(snapshots, "s-restore")

    b_run, b_done, b_gone = restored.blocks
    assert isinstance(b_run, ToolBlock) and b_run.status == "running" and b_run.output == "still-alive\n"
    assert isinstance(b_done, ToolBlock) and b_done.status == "error" and b_done.exit_code == 3 and b_done.output == "finished-err\n"
    assert isinstance(b_gone, ToolBlock) and b_gone.status == "interrupted" and b_gone.is_error is False and b_gone.artifact["running"] is False

    assert _render_lines(restored, width=120) == [
        "",
        "⠼ Running tail -f app.log · exec-still · interactive",
        "  └ output",
        "    still-alive",
        "",
        "• Failed (exit 3) make build",
        "  └ output",
        "    finished-err",
        "",
        "• Running sleep 60 · exec-gone — interrupted (completion unconfirmed)",
        "  └ output",
        "    before-crash",
    ]


# ---------------------------------------------------------------------------
# 11. Truncated log path & notice rendered exactly once
# ---------------------------------------------------------------------------


def test_truncated_log_path_and_notice_rendered_once_even_with_embedded_footer() -> None:
    log_path = "/workspace/.deep-agent/logs/exec/20261126-abcdef012345.log"
    embedded = (
        "tail line 1\ntail line 2\n\n"
        f"[Output truncated: showing the last 64 bytes.\n"
        f"Full output saved to: /host/20261126-abcdef012345.log\n"
        f"Agent path: {log_path}]"
    )
    # Case A: ToolBlock.output still carries the formatted footer (e.g. fallback path without artifact.output).
    state_fallback = CliState(blocks=[
        ToolBlock(
            "c-f", "execute", {"command": "cat big.txt"},
            output=embedded,
            artifact={"execution_id": "e-f", "running": False, "exit_code": 0, "truncated": True, "agent_log_path": log_path},
            status="completed",
        ),
    ])
    lines_fallback = _render_lines(state_fallback, 140)
    assert sum(log_path in line for line in lines_fallback) == 1
    assert sum("Output truncated" in line for line in lines_fallback) == 1

    # Case B: ToolBlock.output holds clean raw tail from artifact.output.
    state_clean = CliState(blocks=[
        ToolBlock(
            "c-c", "execute", {"command": "cat big.txt"},
            output="tail line 1\ntail line 2\n",
            artifact={"execution_id": "e-c", "running": False, "exit_code": 0, "truncated": True, "agent_log_path": log_path},
            status="completed",
        ),
    ])
    assert _render_lines(state_clean, 140) == lines_fallback == [
        "",
        "• Ran cat big.txt",
        "  └ output",
        "    tail line 1",
        "    tail line 2",
        f"  Output truncated · full output: {log_path}",
    ]


# ---------------------------------------------------------------------------
# Capability contract: WorkspaceCompositeBackend derives real executor capability
# ---------------------------------------------------------------------------


class _SyncOnlyExecutor(SandboxBackendProtocol):
    @property
    def id(self) -> str:
        return "sync-only"

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return ExecuteResponse(output=f"sync:{command}", exit_code=0, truncated=False)

    def download_files(self, paths: list[str]) -> list[Any]:
        return []

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[Any]:
        return []


class _InteractiveExecutor(_SyncOnlyExecutor):
    supports_interactive_execution = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        yield_time: float | None = None,
        stdin_mode: str = "none",
    ) -> ExecuteResponse:
        self.calls.append({"mode": "sync", "command": command, "timeout": timeout, "yield_time": yield_time, "stdin_mode": stdin_mode})
        return ExecuteResponse(output=f"interactive:{command}", exit_code=0, truncated=False)

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        yield_time: float | None = None,
        stdin_mode: str = "none",
    ) -> ExecuteResponse:
        self.calls.append({"mode": "async", "command": command, "timeout": timeout, "yield_time": yield_time, "stdin_mode": stdin_mode})
        return ExecuteResponse(output=f"async-interactive:{command}", exit_code=0, truncated=False)

    def write_stdin(
        self,
        execution_id: str,
        *,
        chars: str = "",
        yield_time: float = 1.0,
        action: str = "write",
        write_timeout: float = 15.0,
        thread_id: str | None = None,
    ) -> ExecuteResponse:
        return ExecuteResponse(output="", exit_code=0, truncated=False)

    def execution_info(self, execution_id: str, *, thread_id: str | None = None) -> dict[str, object] | None:
        return None

    def execution_metadata(self, execution_id: str, *, thread_id: str | None = None) -> dict[str, object] | None:
        return None

    def execution_snapshots(self, thread_id: str) -> dict[str, dict[str, object]]:
        return {}

    def list_active_executions(self, thread_id: str) -> list[dict[str, object]]:
        return []

    def send_execution_input(
        self,
        execution_id: str,
        *,
        chars: str = "",
        action: str = "write",
        write_timeout: float = 15.0,
        thread_id: str | None = None,
    ) -> None:
        pass

    def resize_execution(self, execution_id: str, rows: int, cols: int, *, thread_id: str | None = None) -> None:
        pass

    def cleanup_executions(self, thread_id: str, *, join_timeout: float = 2.0) -> int:
        return 0


class _IncompleteExecutor(_SyncOnlyExecutor):
    supports_interactive_execution = True


def test_workspace_composite_backend_derives_interactive_capability_from_executor(tmp_path: Path) -> None:
    cfg = SandboxConfig(workspace=tmp_path)

    sync_exec = _SyncOnlyExecutor()
    sync_exec.config = cfg  # type: ignore[attr-defined]
    sync_composite = WorkspaceCompositeBackend(sync_exec, skills_dir=None)
    assert sync_composite.supports_interactive_execution is False
    assert interactive_backend(sync_composite) is None
    assert supports_interactive_execution(sync_composite) is False
    assert sync_composite.execute("echo hi").output == "sync:echo hi"

    incomplete_exec = _IncompleteExecutor()
    incomplete_exec.config = cfg  # type: ignore[attr-defined]
    incomplete_composite = WorkspaceCompositeBackend(incomplete_exec, skills_dir=None)
    assert incomplete_composite.supports_interactive_execution is False
    assert interactive_backend(incomplete_composite) is None
    assert supports_interactive_execution(incomplete_composite) is False

    inter_exec = _InteractiveExecutor()
    inter_exec.config = cfg  # type: ignore[attr-defined]
    inter_composite = WorkspaceCompositeBackend(inter_exec, skills_dir=None)
    assert inter_composite.supports_interactive_execution is True
    assert interactive_backend(inter_composite) is inter_composite
    assert supports_interactive_execution(inter_composite) is True
    assert inter_composite.execute("echo hi", yield_time=1.5, stdin_mode="pipe").output == "interactive:echo hi"
    async_res = asyncio.run(inter_composite.aexecute("echo async", yield_time=2.5, stdin_mode="pty"))
    assert async_res.output == "async-interactive:echo async"
    assert inter_exec.calls == [
        {"mode": "sync", "command": "echo hi", "timeout": None, "yield_time": 1.5, "stdin_mode": "pipe"},
        {"mode": "async", "command": "echo async", "timeout": None, "yield_time": 2.5, "stdin_mode": "pty"},
    ]
