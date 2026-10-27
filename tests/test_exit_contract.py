"""Current keyboard, exit, and session-lease decisions.

These tests record what the CLI does today. The map is docs/refactor-baseline.md.
They should fail when a later change moves a decision accidentally.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent.cli.app import INPUT_CLEARED_HINT, CliApplication
from agent.cli.interactions import InteractionController
from agent.cli.lifecycle import TaskStop
from agent.runner import AgentRunner, RunResult
from agent.session import SessionStore
from tests.conftest import scripted_model


_APPROVAL = [{"id": "call-1", "name": "write_file", "args": {"file_path": "/workspace/note.txt"}}]


class Harness:
    def __init__(self, app: CliApplication, runner: AgentRunner, cancels: list[int], left: list[bool]) -> None:
        self.app = app
        self.runner = runner
        self.cancels = cancels
        self.left = left
        self.decisions: list[str] = []

    def press(self, keys: tuple[str, ...]) -> None:
        binding = next(item for item in self.app.bindings.bindings if item.keys == keys)
        binding.handler(SimpleNamespace(current_buffer=self.app.buffer))

    def close(self) -> None:
        self.app._io_executor.shutdown(wait=False, cancel_futures=True)
        self.runner.close()


def _open(store: SessionStore | None = None) -> tuple[Harness, object]:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        session_store=store,
        thread_id=None if store is not None else "exit-contract",
    )
    pipe = create_pipe_input()
    pipe.__enter__()
    app = CliApplication(runner, input=pipe, output=DummyOutput())
    cancels: list[int] = []
    left: list[bool] = []
    runner.request_cancel = lambda: cancels.append(1)  # type: ignore[method-assign]
    # prompt_toolkit refuses Application.exit() until run_async() is active.
    # The decisions above that call are what this contract locks.
    app.application.exit = lambda *args, **kwargs: left.append(True)  # type: ignore[method-assign]
    harness = Harness(app, runner, cancels, left)
    runner.approve_tool = lambda *args, **kwargs: harness.decisions.append("approve")  # type: ignore[method-assign]
    runner.reject_tool = lambda *args, **kwargs: harness.decisions.append("reject")  # type: ignore[method-assign]
    return harness, pipe


def _finish(harness: Harness, pipe: object) -> None:
    try:
        harness.close()
    finally:
        pipe.__exit__(None, None, None)


@pytest.fixture
def cli():
    harness, pipe = _open()
    try:
        yield harness
    finally:
        _finish(harness, pipe)


def _clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    import agent.cli.app as cli_module

    clock = [0.0]
    monkeypatch.setattr(cli_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    return clock


def test_ctrl_c_exit_window_is_one_clock(cli: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _clock(monkeypatch)
    cli.app.buffer.text = "draft"
    cli.press(("c-c",))
    assert cli.app.buffer.text == ""
    assert not cli.app._exiting
    assert cli.app._last_ctrl_c == 0.0
    assert cli.cancels == []

    clock[0] = 0.999
    cli.press(("c-c",))
    assert cli.app._exiting
    assert cli.left == [True]
    assert cli.cancels == []


def test_running_ctrl_c_then_exit_cancels_once_per_exit(cli: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _clock(monkeypatch)
    cli.app.state.running = True
    cli.app.state.status = "Working…  Esc to cancel"
    cli.app.buffer.text = "draft"
    cli.press(("c-c",))
    assert cli.app.buffer.text == ""
    assert cli.app.state.running
    assert cli.app.state.status == "Working…  Esc to cancel"
    assert INPUT_CLEARED_HINT not in cli.app.state.status
    assert cli.cancels == []

    clock[0] = 0.2
    cli.press(("c-c",))
    assert cli.app._exiting
    assert cli.cancels == [1]
    cli.press(("c-c",))
    assert cli.cancels == [1]


def test_ctrl_c_dismisses_interaction_without_arming_exit(cli: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _clock(monkeypatch)
    cli.app.interaction = InteractionController.approval(_APPROVAL)
    cli.press(("c-c",))
    assert cli.app.interaction is None
    assert cli.app._last_ctrl_c is None
    assert not cli.app._exiting
    assert cli.decisions == []
    assert cli.app.state.status == "Approval still pending · F2 to decide"

    clock[0] = 0.1
    cli.press(("c-c",))
    assert not cli.app._exiting
    assert cli.app._last_ctrl_c == 0.1


def test_ctrl_c_at_window_boundary_clears_again(cli: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _clock(monkeypatch)
    cli.press(("c-c",))
    clock[0] = 1.0
    cli.app.buffer.text = "later"
    cli.press(("c-c",))
    assert cli.app.buffer.text == ""
    assert not cli.app._exiting
    assert cli.left == []
    assert cli.app._last_ctrl_c == 1.0


def test_ctrl_d_edits_nonempty_input_and_exits_when_empty(cli: Harness) -> None:
    cli.app.buffer.text = "ab"
    cli.app.buffer.cursor_position = 0
    cli.press(("c-d",))
    assert cli.app.buffer.text == "b"
    assert not cli.app._exiting

    cli.app.buffer.text = "ab"
    cli.app.buffer.cursor_position = 2
    cli.press(("c-d",))
    assert cli.app.buffer.text == "ab"
    assert not cli.app._exiting

    cli.app.interaction = InteractionController.approval(_APPROVAL)
    cli.app.buffer.text = ""
    cli.press(("c-d",))
    assert cli.app._exiting
    assert cli.left == [True]
    assert cli.app.interaction is not None
    assert cli.decisions == []
    assert cli.cancels == []


def test_slash_exit_uses_exit_entry(cli: Harness) -> None:
    cli.app.state.running = True

    async def invoke(times: int) -> None:
        handler = cli.app.command_by_name["exit"].handler
        for _ in range(times):
            await handler(cli.app, "ignored")

    asyncio.run(invoke(2))
    assert cli.app._exiting
    assert cli.left == [True]
    assert cli.cancels == [1]


def test_exit_does_not_start_another_ui_task(cli: Harness) -> None:
    cli.app.state.running = True
    cli.app.exit()

    async def scenario() -> None:
        ran: list[int] = []

        async def helper() -> None:
            ran.append(1)

        task = cli.app.lifecycle.spawn(helper(), stop=TaskStop.CANCEL, name="clipboard-paste")
        await task
        assert ran == []

    asyncio.run(scenario())
    assert cli.cancels == [1]
    cli.app.exit()
    assert cli.cancels == [1]


def test_repeated_exit_is_ignored(cli: Harness) -> None:
    cli.app.state.running = True
    cli.app.exit()
    cli.app.exit()
    assert cli.cancels == [1]
    assert cli.left == [True]
    assert cli.app._exiting


def test_escape_after_exit_requests_cancel_again_while_running(cli: Harness) -> None:
    """exit() does not mark the run cancel as already sent."""
    cli.app.state.running = True
    cli.app.exit()
    cli.press(("escape",))
    assert cli.cancels == [1, 1]
    assert cli.app._cancel_sent


def test_escape_prefers_slash_line_over_scroll(cli: Harness) -> None:
    cli.app._transcript_line_count = 500
    cli.app._transcript_anchor = 0
    cli.app.buffer.text = "/status"
    cli.press(("escape",))
    assert cli.app.buffer.text == ""
    assert cli.app._transcript_anchor == 0
    assert cli.cancels == []

    cli.app.buffer.text = "hello"
    cli.press(("escape",))
    assert cli.app.buffer.text == "hello"
    assert cli.app._transcript_anchor is None


def test_escape_closes_review_before_approval(cli: Harness) -> None:
    cli.app.interaction = InteractionController.approval(_APPROVAL)
    cli.app._reviewing = True
    cli.press(("escape",))
    assert not cli.app._reviewing
    assert cli.app.interaction is not None
    assert cli.decisions == []

    cli.press(("escape",))
    assert cli.app.interaction is None
    assert cli.app.state.status == "Approval still pending · F2 to decide"
    assert cli.decisions == []


def test_escape_backs_out_of_a_picker_without_applying_it(cli: Harness) -> None:
    cli.app.interaction = InteractionController(
        kind="resume",
        title="Resume session",
        question="Select a session",
        fields=[],
    )
    cli.press(("escape",))
    assert cli.app.interaction is None
    assert cli.app.state.status == "Resume cancelled"
    assert not cli.app._exiting


def test_session_wait_ctrl_c_uses_the_shared_exit_window(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "wait.sqlite3")
    harness, pipe = _open(store)
    clock = _clock(monkeypatch)
    try:
        target = "abcdef1234567890"
        harness.app.sessions._wait_target = target
        harness.app.state.status = harness.app.sessions.wait_status()
        harness.app.buffer.text = "draft"
        harness.press(("escape",))
        assert harness.app.sessions._wait_cancelled
        assert not harness.app._exiting
        assert harness.cancels == []

        harness.app.sessions._wait_cancelled = False
        harness.app.buffer.text = "draft"
        harness.press(("c-c",))
        assert harness.app.buffer.text == ""
        assert not harness.app.sessions._wait_cancelled
        assert harness.app.state.status == INPUT_CLEARED_HINT
        clock[0] = 0.4
        harness.press(("c-c",))
        assert harness.app._exiting
        assert harness.app.sessions._wait_cancelled
        assert harness.cancels == []
    finally:
        _finish(harness, pipe)
        store.close()


def test_escape_during_mcp_background_does_not_cancel_a_run(cli: Harness) -> None:
    cli.app.mcp_controller.busy = True
    cli.press(("escape",))
    assert cli.cancels == []
    assert "background" in cli.app.state.status

    cli.app.state.running = True
    cli.press(("escape",))
    assert cli.cancels == [1]
    assert cli.app.state.status.startswith("Cancelling")


def test_escape_during_compaction_does_not_cancel(cli: Harness) -> None:
    cli.app._compacting = True
    cli.press(("escape",))
    assert cli.cancels == []
    assert cli.app.state.status == "Wait for compaction to finish"


def test_ui_exit_keeps_the_session_lease(tmp_path) -> None:
    store = SessionStore(tmp_path / "lease.sqlite3")
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        session_store=store,
    )
    thread = runner.thread_id
    pipe = create_pipe_input()
    pipe.__enter__()
    try:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.application.exit = lambda *args, **kwargs: None  # type: ignore[method-assign]
        app.exit()
        assert app._exiting
        assert runner._runtime.lease is not None
        assert store.try_acquire_session(thread) is None
        runner.close()
        assert runner._runtime.lease is None
        taken = store.try_acquire_session(thread)
        assert taken is not None
        taken.release()
    finally:
        pipe.__exit__(None, None, None)
        store.close()


def test_completed_run_does_not_start_follow_up_after_exit() -> None:
    harness, pipe = _open()
    app, runner = harness.app, harness.runner
    reservations: list[int] = []
    real_reserve = runner.reserve_run

    def reserve() -> int:
        reservations.append(1)
        return real_reserve()

    runner.reserve_run = reserve  # type: ignore[method-assign]
    try:
        runner.follow_up("still queued")
        app.state.running = True
        epoch = app._run_epoch
        app.exit()
        app._handle_result(RunResult(status="completed", output="done"))
        assert reservations == []
        assert app._run_epoch == epoch
        assert runner.control.pending_follow_up_count() == 1
        assert runner.control.pending_message_texts() == ["still queued"]
    finally:
        _finish(harness, pipe)


def test_exit_does_not_mark_a_queued_follow_up_applied() -> None:
    harness, pipe = _open()
    app, runner = harness.app, harness.runner
    try:
        runner.control.follow_up("keep me")
        app.state.running = True
        app.exit()
        app._start_queued_run()
        assert runner.control.pending_follow_up_count() == 1
        assert runner.control.pending_message_texts() == ["keep me"]
        assert all(getattr(block, "content", None) != "keep me" for block in app.state.blocks)
    finally:
        _finish(harness, pipe)


def test_exit_does_not_reserve_another_run() -> None:
    harness, pipe = _open()
    app, runner = harness.app, harness.runner
    reservations: list[int] = []
    runner.reserve_run = lambda: reservations.append(1)  # type: ignore[method-assign]
    try:
        runner.steer("first")
        runner.follow_up("second")
        app.state.running = True
        app.exit()
        app._start_run("direct")
        app._start_queued_run()
        app._handle_result(RunResult(status="completed", output="done"))
        assert reservations == []
        assert runner.control.pending_message_texts() == ["first", "second"]
    finally:
        _finish(harness, pipe)


def test_completed_run_still_starts_steering_before_follow_up() -> None:
    harness, pipe = _open()
    app, runner = harness.app, harness.runner
    started: list[str] = []
    real_start = app._start_run

    def start(text: str, **kwargs: object) -> None:
        resume = kwargs.get("resume_call")
        args = getattr(resume, "args", ())
        started.append(args[0] if args else text)
        if kwargs.get("resume_call") is None:
            real_start(text, **kwargs)  # type: ignore[arg-type]

    app._start_run = start  # type: ignore[method-assign]
    try:
        runner.steer("steer-first")
        runner.follow_up("follow-second")
        app._handle_result(RunResult(status="completed", output="done"))
        assert started == ["steer-first"]
        assert runner.control.pending_steering_count() == 0
        assert runner.control.pending_message_texts() == ["follow-second"]
        app._handle_result(RunResult(status="completed", output="again"))
        assert started == ["steer-first", "follow-second"]
        assert runner.control.pending_message_texts() == []
    finally:
        _finish(harness, pipe)
