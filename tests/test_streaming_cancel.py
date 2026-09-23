"""Execute hard-cancel and realtime merged output streaming."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from agent.cancel import ToolCancelContext, set_cancel_context, set_output_emitter
from agent.config import SandboxConfig
from agent.control import RunController
from agent.sandbox import UnsandboxedShellBackend, _run_process


def _unsandboxed(tmp_path: Path, **kwargs) -> UnsandboxedShellBackend:
    config = SandboxConfig(workspace=tmp_path, allow_unsandboxed=True, bwrap_path="/missing", **kwargs)
    return UnsandboxedShellBackend(config)


def test_execute_streams_chunks_before_exit(tmp_path: Path) -> None:
    chunks: list[str] = []
    set_output_emitter(lambda _id, text, _stream: chunks.append(text))
    ctx = ToolCancelContext(run_token="r", tool_name="execute", tool_call_id="t1", cancel_event=threading.Event())
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
