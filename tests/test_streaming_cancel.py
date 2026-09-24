"""Execute hard-cancel and realtime merged output streaming."""
from __future__ import annotations

import threading
import time
from pathlib import Path
import re

import pytest

from agent.cancel import ToolCancelContext, set_cancel_context, set_output_emitter
from agent.config import SandboxConfig
from agent.control import RunController
from agent.sandbox import UnsandboxedShellBackend, WorkspaceCompositeBackend, _run_process
from agent.tools.execute import build_execute_tool


def _unsandboxed(tmp_path: Path, **kwargs) -> UnsandboxedShellBackend:
    config = SandboxConfig(workspace=tmp_path, allow_unsandboxed=True, bwrap_path="/missing", **kwargs)
    return UnsandboxedShellBackend(config)


def test_execute_streams_chunks_before_exit(tmp_path: Path) -> None:
    chunks: list[str] = []
    set_output_emitter(lambda _id, text, _stream: chunks.append(text))
    ctx = ToolCancelContext(tool_name="execute", tool_call_id="t1", cancel_event=threading.Event())
    set_cancel_context(ctx)
    try:
        backend = _unsandboxed(tmp_path, max_output_bytes=100000)
        result = backend.execute("printf 'one\\n'; sleep 0.05; printf 'two\\n'")
    finally:
        set_cancel_context(None)
        set_output_emitter(None)
    assert result.exit_code == 0
    assert chunks, "expected realtime deltas before completion"
    assert "one" in "".join(chunks)
    assert "two" in result.output


def test_execute_cancel_kills_process_group(tmp_path: Path) -> None:
    controller = RunController()
    controller.begin_run()
    ctx = controller.open_tool_context(tool_name="execute", tool_call_id="kill-me")
    backend = _unsandboxed(tmp_path)

    def cancel_soon() -> None:
        time.sleep(0.1)
        controller.cancel()

    thread = threading.Thread(target=cancel_soon, daemon=True)
    thread.start()
    started = time.monotonic()
    result = backend.execute("sleep 30")
    elapsed = time.monotonic() - started
    thread.join(timeout=2)
    controller.close_tool_context(ctx)
    assert result.exit_code == 130
    assert "Cancelled" in result.output
    assert elapsed < 5


def test_execute_timeout_returns_124(tmp_path: Path) -> None:
    backend = _unsandboxed(tmp_path)
    result = backend.execute("sleep 5", timeout=1)
    assert result.exit_code == 124


def test_timeout_when_child_keeps_output_pipe_open(tmp_path: Path) -> None:
    backend = _unsandboxed(tmp_path)
    started = time.monotonic()
    result = backend.execute("sleep 4 &", timeout=1)
    elapsed = time.monotonic() - started
    assert result.exit_code == 0
    assert elapsed < 0.8


def test_post_exit_output_resets_idle_grace(tmp_path: Path) -> None:
    backend = _unsandboxed(tmp_path)
    result = backend.execute("(sleep 0.04; printf a; sleep 0.04; printf b; sleep 0.04; printf c) &")
    assert result.exit_code == 0
    assert result.output == "abc"


def test_unicode_split_across_chunks(tmp_path: Path) -> None:
    chunks: list[str] = []
    set_output_emitter(lambda _id, text, _stream: chunks.append(text))
    # Write a multi-byte UTF-8 character in two writes via python.
    script = (
        "import sys; "
        "sys.stdout.buffer.write(b'\\xe4'); sys.stdout.buffer.flush(); "
        "import time; time.sleep(0.05); "
        "sys.stdout.buffer.write(b'\\xb8\\xad'); sys.stdout.buffer.flush()"
    )
    try:
        result = _run_process(
            ["python3", "-c", script],
            timeout=5,
            max_output_bytes=1000,
            cwd=tmp_path,
        )
    finally:
        set_output_emitter(None)
    assert result.exit_code == 0
    assert "中" in result.output or "中" in "".join(chunks)


def test_truncation_keeps_draining(tmp_path: Path) -> None:
    backend = _unsandboxed(tmp_path, max_output_bytes=8)
    result = backend.execute("printf 'abcdefghijklmnop'")
    assert result.truncated is True
    assert "truncated" in result.output.lower()


def test_truncation_also_caps_streamed_output(tmp_path: Path) -> None:
    chunks: list[str] = []
    set_output_emitter(lambda _id, text, _stream: chunks.append(text))
    try:
        result = _unsandboxed(tmp_path, max_output_bytes=16).execute("printf '%020000d' 0")
    finally:
        set_output_emitter(None)
    streamed = "".join(chunks)
    assert result.exit_code == 0
    assert result.truncated
    assert streamed.startswith("0000000000000000")
    assert streamed.count("Output truncated") == 1
    assert len(streamed) < 120


def test_short_and_exact_limit_output_do_not_create_log(tmp_path: Path) -> None:
    backend = _unsandboxed(tmp_path, max_output_bytes=8)
    for command, expected in (("printf short", "short"), ("printf 12345678", "12345678")):
        result = backend.execute(command)
        assert result.output == expected
        assert not result.truncated
    assert not (tmp_path / ".deep-agent" / "logs").exists()


def test_truncated_result_has_tail_and_complete_workspace_log(tmp_path: Path) -> None:
    chunks: list[str] = []
    set_output_emitter(lambda _id, text, _stream: chunks.append(text))
    try:
        backend = _unsandboxed(tmp_path, max_output_bytes=8)
        result = backend.execute("printf ABCD; sleep 0.05; printf efgh >&2; sleep 0.05; printf 1234")
    finally:
        set_output_emitter(None)
    assert result.truncated
    assert result.output.startswith("efgh1234")
    assert result.output.count("[Output truncated: showing") == 1
    match = re.search(r"Agent path: (/workspace/\.deep-agent/logs/exec/[^\]\n]+)", result.output)
    assert match is not None
    assert f"Full output saved to: {tmp_path}/.deep-agent/logs/exec/" in result.output
    log_path = tmp_path / match.group(1).removeprefix("/workspace/")
    assert log_path.read_bytes() == b"ABCDefgh1234"
    routed = WorkspaceCompositeBackend(backend, skills_dir=None).read(match.group(1))
    assert routed.file_data and routed.file_data["content"] == "ABCDefgh1234"
    assert log_path.stat().st_mode & 0o777 == 0o600
    assert log_path.parent.stat().st_mode & 0o777 == 0o700
    streamed = "".join(chunks)
    assert streamed.startswith("ABCDefgh")
    assert "1234" not in streamed
    assert streamed.count("Output truncated") == 1
    tool_message = build_execute_tool(backend).invoke({
        "type": "tool_call", "id": "exec-test", "name": "execute",
        "args": {"command": "printf ABCDefgh1234"},
    })
    tool_result, artifact = tool_message.content, tool_message.artifact
    assert tool_result.count("[Output truncated: showing") == 1
    assert "[output truncated]" not in tool_result
    assert artifact["truncated"] is True
    assert artifact["host_log_path"] and str(tmp_path) in artifact["host_log_path"]
    assert artifact["agent_log_path"].startswith("/workspace/.deep-agent/logs/exec/")


def test_truncated_timeout_keeps_tail_and_captured_log(tmp_path: Path) -> None:
    result = _unsandboxed(tmp_path, max_output_bytes=8).execute(
        "printf ABCDefgh1234; sleep 5", timeout=1,
    )
    assert result.exit_code == 124
    assert "efgh1234" in result.output
    assert "timed out" in result.output
    assert f"Full output saved to: {tmp_path}/.deep-agent/logs/exec/" in result.output
    assert "Agent path: /workspace/.deep-agent/logs/exec/" in result.output


def test_cancel_drains_already_produced_pipe_output(tmp_path: Path) -> None:
    chunks: list[str] = []
    controller = RunController()
    controller.begin_run()
    ctx = controller.open_tool_context(tool_name="execute", tool_call_id="drain-cancel")
    block = "B" * 65536
    script = tmp_path / "writer.py"
    script.write_text(
        "import sys, time\n"
        "sys.stdout.write('PREFIX\\n')\n"
        f"sys.stdout.write({block!r})\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    def emit(_id: str, text: str, _stream: str) -> None:
        chunks.append(text)
        if "PREFIX" in "".join(chunks):
            controller.cancel()

    set_cancel_context(ctx)
    set_output_emitter(emit)
    try:
        result = _unsandboxed(tmp_path, max_output_bytes=200_000).execute(f"python3 {script}")
    finally:
        controller.close_tool_context(ctx)
        set_cancel_context(None)
        set_output_emitter(None)
    streamed = "".join(chunks)
    assert result.exit_code == 130
    assert block in result.output
    assert block in streamed
    assert "Cancelled by user" in result.output


def test_truncated_cancel_keeps_tail_and_captured_log(tmp_path: Path) -> None:
    controller = RunController()
    controller.begin_run()
    ctx = controller.open_tool_context(tool_name="execute", tool_call_id="cancel-log")
    set_cancel_context(ctx)
    thread = threading.Thread(target=lambda: (time.sleep(0.1), controller.cancel()), daemon=True)
    thread.start()
    try:
        result = _unsandboxed(tmp_path, max_output_bytes=8).execute("printf ABCDefgh1234; sleep 5")
    finally:
        thread.join(2)
        controller.close_tool_context(ctx)
        set_cancel_context(None)
    assert result.exit_code == 130
    assert "efgh1234" in result.output
    assert "Cancelled by user" in result.output
    assert f"Full output saved to: {tmp_path}/.deep-agent/logs/exec/" in result.output
    assert "Agent path: /workspace/.deep-agent/logs/exec/" in result.output


def test_log_symlink_is_rejected_without_losing_tail(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / ".deep-agent").symlink_to(outside, target_is_directory=True)
    result = _unsandboxed(tmp_path, max_output_bytes=8).execute("printf ABCDefgh1234")
    assert result.truncated
    assert result.output.startswith("efgh1234")
    assert "Full output could not be saved" in result.output
    assert "Full output saved to:" not in result.output
    assert list(outside.iterdir()) == []


def test_log_creation_failure_reports_missing_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import sandbox

    def cannot_create(_workspace: Path) -> tuple[int, int, str]:
        raise PermissionError("log directory is not writable")

    monkeypatch.setattr(sandbox, "_create_exec_log", cannot_create)
    result = _unsandboxed(tmp_path, max_output_bytes=8).execute("printf ABCDefgh1234")
    assert result.truncated
    assert result.output.startswith("efgh1234")
    assert "Full output could not be saved" in result.output
    assert "Full output saved to:" not in result.output


def test_cancellable_tool_cancel_callback_invoked_once() -> None:
    calls = {"n": 0}
    controller = RunController()
    controller.begin_run()
    ctx = controller.open_tool_context(tool_name="custom", tool_call_id="c1")

    def on_cancel() -> None:
        calls["n"] += 1

    ctx.register_callback(on_cancel)
    controller.cancel()
    controller.cancel()
    assert calls["n"] == 1
    controller.close_tool_context(ctx)


def test_tool_cancel_middleware_calls_cancel_once() -> None:
    from types import SimpleNamespace
    from agent.middleware.cancel_tools import ToolCancelMiddleware

    class Tool:
        count = 0

        def cancel(self) -> None:
            self.count += 1

    tool = Tool()
    controller = RunController()
    controller.begin_run()
    request = SimpleNamespace(tool_call={"name": "example", "id": "example-1"}, tool=tool)
    ToolCancelMiddleware(controller).wrap_tool_call(request, lambda _: controller.cancel())
    assert tool.count == 1
