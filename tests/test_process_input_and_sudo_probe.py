"""Stage 5 tests: direct user input to running processes (PIPE/PTY) and real bwrap sudo probe."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from langchain_core.messages import AIMessage
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
import pytest

from agent.cli.app import CliApplication
from agent.execution.backend import select_backend
from agent.execution.ownership import reset_execution_thread_id, set_execution_thread_id
from agent.execution.policy import SandboxConfig
from agent.execution.sudo_probe import probe_sudo_feasibility
from agent.runtime.contracts import RunEvent
from agent.runtime.runner import AgentRunner
from tests.conftest import scripted_model


def _make_bwrap_runner(tmp_path: Path, *, mode: str = "restricted", thread_id: str = "proc-t") -> AgentRunner:
    try:
        probe = select_backend(SandboxConfig(workspace=tmp_path, mode=mode))
        probe.backend.executor.pool.shutdown("probe")
    except Exception as exc:
        if os.environ.get("REQUIRE_BWRAP_TEST") == "1":
            raise
        pytest.skip(f"bubblewrap unavailable ({mode}): {exc}")
    return AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        sandbox_config=SandboxConfig(workspace=tmp_path, mode=mode),
        thread_id=thread_id,
    )


def test_direct_process_input_pipe_detach_eof_and_no_fabricated_messages(tmp_path: Path) -> None:
    runner = _make_bwrap_runner(tmp_path, thread_id="direct-pipe")
    tok = set_execution_thread_id("direct-pipe")
    try:
        # Start a real interactive PIPE process inside Bubblewrap
        started = runner.prepared.backend.execute(
            "python3 -u -c 'import sys\nfor line in sys.stdin:\n    print(\"GOT:\" + line.strip(), flush=True)\nprint(\"EOF_DONE\", flush=True)'",
            stdin_mode="pipe",
            yield_time=0.2,
            timeout=15,
        )
        assert started.running and started.execution_id
        exec_id = started.execution_id

        async def scenario() -> None:
            with create_pipe_input() as pipe:
                app = CliApplication(runner, input=pipe, output=DummyOutput())
                blocks_before = list(app.state.blocks)

                # 1) /stdin with one active process attaches directly and renders distinct banner with execution_id
                app.handle_stdin_command("")
                assert app.attached_execution_id == exec_id
                banner = "".join(text for _style, text in app._process_input_banner_text())
                assert exec_id in banner
                assert "pipe" in banner
                assert "Process stdin" in banner

                # 2) Enter submits asynchronously to Worker queue without creating chat messages or history entries
                app.buffer.text = "yes"
                app.run_dispatch._submit_buffer("steer")
                await app.flush_process_input()
                assert app.buffer.text == ""
                assert "yes" not in list(app.buffer.history.get_strings())
                assert app.state.blocks == blocks_before

                # Agent's unread output window was NOT swallowed by direct user input!
                polled = runner.prepared.backend.write_stdin(exec_id, chars="", yield_time=1.0, thread_id="direct-pipe")
                assert polled.running
                assert "GOT:yes" in polled.output

                # 3) Esc detaches WITHOUT terminating the running process!
                app.detach_process_input()
                assert app.attached_execution_id is None
                assert any(item["execution_id"] == exec_id for item in runner.list_active_executions())

                # 4) Re-attach by ID and send EOF -> process finishes cleanly
                app.handle_stdin_command(exec_id)
                assert app.attached_execution_id == exec_id
                assert app.send_attached_input(action="close") is True
                await app.flush_process_input()
                finished = runner.prepared.backend.write_stdin(exec_id, chars="", yield_time=2.0, thread_id="direct-pipe")
                assert not finished.running
                assert "EOF_DONE" in finished.output

                # 5) execution_finished RunEvent auto-detaches process input mode
                app.run_events._apply_event(RunEvent(
                    type="tool_output_delta",
                    tool_call_id="tc-1",
                    stream="execution_finished",
                    result={"execution_id": exec_id, "exit_code": 0},
                ))
                assert app.attached_execution_id is None

        asyncio.run(scenario())
    finally:
        reset_execution_thread_id(tok)
        runner.close()


def test_direct_process_input_multi_picker_session_reset_and_ownership(tmp_path: Path) -> None:
    runner = _make_bwrap_runner(tmp_path, thread_id="multi-proc")
    tok = set_execution_thread_id("multi-proc")
    try:
        p1 = runner.prepared.backend.execute("cat", stdin_mode="pipe", yield_time=0.2, timeout=15)
        p2 = runner.prepared.backend.execute("cat", stdin_mode="pipe", yield_time=0.2, timeout=15)
        assert p1.running and p2.running and p1.execution_id and p2.execution_id

        # Ownership enforcement: another thread cannot send direct input to multi-proc's process
        with pytest.raises(PermissionError):
            runner.prepared.backend.send_execution_input(p1.execution_id, chars="x\n", thread_id="other-session")

        async def scenario() -> None:
            with create_pipe_input() as pipe:
                app = CliApplication(runner, input=pipe, output=DummyOutput())
                # Multiple active processes -> opens unified InteractionController picker
                app.handle_stdin_command("")
                assert app.dialogs.current is not None
                assert app.dialogs.current.kind == "process_attach"
                # Confirm first option -> attaches cleanly
                app.run_dispatch._submit_buffer("steer")
                assert app.dialogs.current is None
                assert app.attached_execution_id in {p1.execution_id, p2.execution_id}

                # Session switch / reset_session_view auto-detaches cleanly
                app.reset_session_view()
                assert app.attached_execution_id is None

        asyncio.run(scenario())

        # Clean up both processes
        runner.send_execution_input(p1.execution_id, action="cancel")
        runner.send_execution_input(p2.execution_id, action="cancel")
    finally:
        reset_execution_thread_id(tok)
        runner.close()


def test_direct_process_input_pty_resize_and_max_input_limit(tmp_path: Path) -> None:
    runner = _make_bwrap_runner(tmp_path, thread_id="pty-proc")
    tok = set_execution_thread_id("pty-proc")
    try:
        # Real PTY command inside Bubblewrap checks kernel TIOCSWINSZ dimensions!
        started = runner.prepared.backend.execute(
            "python3 -u -c 'import os, sys\nsys.stdin.readline()\nsz = os.get_terminal_size(0)\nprint(f\"SIZE:{sz.lines}x{sz.columns}\", flush=True)'",
            stdin_mode="pty",
            yield_time=0.3,
            timeout=15,
        )
        assert started.running and started.execution_id
        exec_id = started.execution_id

        # Sync PTY size via runner.resize_execution -> Worker TIOCSWINSZ
        runner.resize_execution(exec_id, 42, 111)
        runner.send_execution_input(exec_id, chars="go\n", action="write")
        done = runner.prepared.backend.write_stdin(exec_id, chars="", yield_time=3.0, thread_id="pty-proc")
        assert not done.running
        assert "SIZE:42x111" in done.output

        # Direct input goes through the same MAX_INPUT_BYTES (1 MiB) Worker queue check!
        big = runner.prepared.backend.execute("sleep 5", stdin_mode="pipe", yield_time=0.2, timeout=15)
        assert big.running and big.execution_id
        try:
            with pytest.raises(OSError, match="buffer full"):
                runner.send_execution_input(big.execution_id, chars="A" * (1024 * 1024 + 64))
        finally:
            runner.send_execution_input(big.execution_id, action="cancel")
    finally:
        reset_execution_thread_id(tok)
        runner.close()


@pytest.mark.parametrize("mode", ["restricted", "full-access"])
def test_real_bwrap_sudo_feasibility_blocked_by_no_new_privs(tmp_path: Path, mode: str) -> None:
    """Section 21: verify real Bubblewrap sets NoNewPrivs=1 in BOTH restricted and full-access modes."""
    try:
        selection = select_backend(SandboxConfig(workspace=tmp_path, mode=mode))
    except Exception as exc:
        if os.environ.get("REQUIRE_BWRAP_TEST") == "1":
            raise
        pytest.skip(f"bubblewrap unavailable ({mode}): {exc}")

    try:
        feasibility = probe_sudo_feasibility(selection.backend)
        assert feasibility.supported is False
        assert feasibility.sandbox_mode == mode
        assert feasibility.no_new_privs is True
        assert "NoNewPrivs" in feasibility.reason
        assert "disabled" in feasibility.reason.lower()
        if "command not found" not in feasibility.sudo_output.lower():
            assert "no new privileges" in feasibility.sudo_output.lower()
    finally:
        selection.backend.executor.pool.shutdown("probe finished")
