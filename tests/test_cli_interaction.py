from langchain_core.messages import AIMessage, ToolMessage
from deepagents.backends import StateBackend
from prompt_toolkit.buffer import CompletionState
from prompt_toolkit.completion import Completion
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.layout.containers import WindowAlign

import asyncio
import logging
import re
import pytest
from rich.cells import cell_len
from collections.abc import Callable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from agent.cli.gitinfo import GitSummary, parse_status
from agent.cli.interactions import InteractionController
from agent.cli.clipboard import ClipboardImage, ClipboardText
from agent.cli.app import (
    FOOTER_LINES,
    CliApplication,
    format_context_usage,
    format_context_window,
    format_workspace_path,
)
from agent.cli.previews import (
    CommandExecutionPreview,
    FileMutationPreview,
    aggregate_file_mutations,
    build_diff_preview,
    build_tool_preview,
    normalize_file_mutation,
    normalize_command_execution,
)
from agent.cli.rendering import (
    TranscriptRenderer,
    _capture,
    _explore_group,
    _message,
    _tool,
    render_interaction,
    render_review,
    render_transcript,
)
from agent.cli.state import CliState, MessageBlock, ToolBlock, TurnSummaryBlock, touch
from agent.cli.pasted_content import PASTED_CONTENT_MIN_CHARS
from agent.config import Settings
from agent.config import ModelProfile
from agent.runner import AgentRunner, RunEvent, RunResult, TurnTiming
from agent.session import SessionStore, TranscriptBlock, messages_to_transcript
from agent.tools.examples import build_example_tools
from agent.factory import DEFAULT_FS_TOOLS, create_agent
from tests.cli_support import (
    await_cli_io as _await_cli_io,
    escape as _escape,
    paint as _paint,
    plain as _plain,
    wheel as _wheel,
    with_painted_app as _with_painted_app,
    without_diff_number as _without_diff_number,
)
from tests.conftest import scripted_model

"""Approval, human input, and keyboard dismissal."""

def test_human_interaction_collects_select_and_text_fields() -> None:
    controller = InteractionController.human({
        "interactionId": "i-1",
        "question": "Choose a scope",
        "fields": [
            {
                "id": "scope", "type": "single_select", "label": "Scope", "required": True,
                "options": [{"value": "local", "label": "Local"}, {"value": "global", "label": "Global"}],
            },
            {"id": "note", "type": "textarea", "label": "Note", "required": False, "options": []},
        ],
    })
    controller.move(1)
    assert controller.accept() is False
    assert controller.accept("later") is True
    assert controller.interaction_id == "i-1"
    assert controller.values == {"scope": "global", "note": "later"}


def test_human_interaction_other_single_and_multi_select() -> None:
    controller = InteractionController.human({"fields": [
        {"id": "scope", "type": "single_select", "required": True,
         "options": [{"value": "local", "label": "Local"}]},
        {"id": "features", "type": "multi_select", "required": True,
         "options": [{"value": "search", "label": "Search"}]},
    ]})
    controller.move(1)
    assert controller.accept() is False
    assert controller.accepts_text
    assert controller.accept("  ") is False
    assert controller.error == "Enter a custom answer."
    assert controller.accept("regional") is False
    assert controller.values["scope"] == "regional"
    assert not controller.accepts_text
    controller.toggle()
    controller.move(1)
    controller.toggle()
    assert controller.accepts_text
    assert controller.accept("analytics") is False
    assert controller.accept() is True
    assert controller.values == {"scope": "regional", "features": ["search", "analytics"]}


def test_human_interaction_other_multi_select_can_be_removed() -> None:
    controller = InteractionController.human({"fields": [{
        "id": "features", "type": "multi_select", "required": True,
        "options": [{"value": "search", "label": "Search"}],
    }]})
    controller.move(1)
    controller.toggle()
    assert controller.accept("analytics") is False
    controller.toggle()
    assert controller.values["features"] == []
    controller.move(-1)
    assert controller.accept() is False
    assert controller.error == "This field is required."


def test_approval_defaults_to_reject_and_escape_is_safe() -> None:
    controller = InteractionController.approval([{
        "toolCallId": "call-write", "name": "write_file",
        "args": {"file_path": "/workspace/note.txt"},
    }])
    assert controller.tool_call_ids == ["call-write"]
    assert controller.accept() is True
    assert controller.values.get("approved") == "reject"


def test_escape_closes_interrupt_ui_without_resuming() -> None:
    runner = AgentRunner(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "call-write", "name": "write_file",
                "args": {"file_path": "/workspace/note.txt", "content": "x"},
            }]),
            AIMessage(content="done"),
        ]),
        backend=StateBackend(),
        thread_id="esc-approval",
    )
    waiting = runner.invoke("write")
    assert waiting.status == "waiting_confirmation"
    approve = runner.approve_tool
    reject = runner.reject_tool

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app.runner.approve_tool = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("approve"))
            app.runner.reject_tool = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("reject"))
            app._handle_result(waiting)
            assert app.interaction is not None
            app._finish_interaction(cancelled=True)
            assert app.interaction is None
            assert runner.current_interrupt().kind.value == "waiting_confirmation"
            assert not any("F2 to decide" in block.content for block in app.state.blocks)
            assert app.state.status == "Approval still pending · F2 to decide"
            assert app._status_visible()
            assert "Approval still pending · F2 to decide" in "".join(value for _, value in app._status_text())

            app._reopen_pending_interaction()
            assert app.interaction is not None
            assert app.interaction.kind == "approval"
            assert app.state.status == "Waiting for input"

            app._toggle_review()
            assert app._reviewing
            app._close_review()
            assert app.interaction is not None
            assert runner.current_interrupt().kind.value == "waiting_confirmation"

    asyncio.run(scenario())
    runner.approve_tool = approve
    runner.reject_tool = reject


@pytest.mark.parametrize("kind,label", [("approval", "Approval"), ("human", "Input"), ("pause", "Pause")])
def test_dismissed_interrupt_hint_uses_working_status_position(kind, label) -> None:
    """关闭面板只更新状态行，重复关闭也不向聊天记录追加提示。"""
    def scenario(app: CliApplication) -> None:
        original = list(app.state.blocks)
        expected = f"{label} still pending · F2 to decide"
        for _ in range(3):
            app.state.running = True
            app._dismiss_pending_interrupt(kind)
            assert app.state.blocks == original
            assert not app.state.running
            fragments = app._status_text()
            assert len(fragments) == 1
            assert expected in fragments[0][1]
        _paint(app)
        screen = app.application.renderer._last_screen
        columns = app.application.output.get_size().columns
        rows = [
            "".join(screen.data_buffer[y][x].char for x in range(columns))
            for y in range(app.application.output.get_size().rows)
        ]
        assert sum(expected in row for row in rows) == 1
        status_row = next(y for y, row in enumerate(rows) if expected in row)
        assert "class:status" in screen.data_buffer[status_row][0].style
        assert any("class:editor" in screen.data_buffer[y][0].style for y in range(status_row + 1, len(rows)))

    _with_painted_app(f"dismiss-status-{kind}", scenario)


@pytest.mark.parametrize("approve", [True, False])
def test_ctrl_c_dismisses_approval_to_status_and_f2_can_finish_decision(approve) -> None:
    """实际按键验证关闭、F2 恢复及批准或拒绝后的正常执行。"""
    async def scenario() -> None:
        runner = AgentRunner(model=scripted_model([
            AIMessage(content="", tool_calls=[{"id": "status-write", "name": "write_file",
                                              "args": {"file_path": "/workspace/status.txt", "content": "x"}}]),
            AIMessage(content="done"),
        ]), backend=StateBackend())
        waiting = runner.invoke("write")
        assert waiting.status == "waiting_confirmation"

        async def wait_until(predicate) -> None:
            for _ in range(300):
                if predicate():
                    return
                await asyncio.sleep(0.01)
            raise TimeoutError("审批状态未更新")

        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app._handle_result(waiting)
            task = asyncio.create_task(app.run_async())
            try:
                await wait_until(lambda: app.application.is_running)
                pipe.send_text("\x03")
                await wait_until(lambda: app.interaction is None)
                assert app.state.status == "Approval still pending · F2 to decide"
                assert not any("still pending" in getattr(block, "content", "") for block in app.state.blocks)
                assert runner.current_interrupt() is not None
                pipe.send_text("\x1bOQ")
                await wait_until(lambda: app.interaction is not None)
                assert app.interaction.kind == "approval"
                assert app.state.status == "Waiting for input"
                if approve:
                    pipe.send_text("\x1b[B")
                    await wait_until(lambda: app.interaction.option_index == 1)
                pipe.send_text("\r")
                await wait_until(lambda: app._run_task is not None and app._run_task.done())
                assert runner.current_interrupt() is None
                assert not app.state.running
                assert "still pending" not in app.state.status
                assert any(getattr(block, "content", "") == "done" for block in app.state.blocks)
            finally:
                app.exit()
                await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())


def test_f2_and_resume_without_interrupt_are_views_only() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        thread_id="no-pending",
    )
    calls = {"approve": 0, "continue": 0}

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app.runner.approve_tool = lambda *a, **k: calls.__setitem__("approve", calls["approve"] + 1)
            app.runner.continue_run = lambda *a, **k: calls.__setitem__("continue", calls["continue"] + 1)
            app._reopen_pending_interaction()
            assert app.interaction is None
            assert app.state.status == "No pending interaction"
            assert calls == {"approve": 0, "continue": 0}

    asyncio.run(scenario())


def test_unknown_interrupt_reopen_reports_without_guessing(monkeypatch) -> None:
    from agent.runner import UnknownInterruptError

    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        thread_id="unknown-int",
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            monkeypatch.setattr(
                app.runner, "current_interrupt",
                lambda: (_ for _ in ()).throw(UnknownInterruptError("Unsupported interrupt type: 'mystery'")),
            )
            app._reopen_pending_interaction()
            assert app.interaction is None
            assert any("Unsupported interrupt type" in block.content for block in app.state.blocks)

    asyncio.run(scenario())


def test_tui_pipe_input_quits_and_restores_application() -> None:
    async def scenario() -> None:
        runner = AgentRunner(
            model=scripted_model([AIMessage(content="unused")]),
            backend=StateBackend(),
            thread_id="cli-pipe",
        )
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            task = asyncio.create_task(app.run_async())
            await asyncio.sleep(0.02)
            pipe.send_text("/exit\r")
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())


def test_restored_human_interaction_accepts_keyboard_selection_after_ctrl_c(tmp_path) -> None:
    store = SessionStore(tmp_path / "human-resume.sqlite3")
    fields = [
        {"id": "annex_gap", "type": "single_select", "required": True,
         "options": [{"value": "current_repo", "label": "Current repo"}, {"value": "other_repo", "label": "Other repo"}]},
        {"id": "next_step", "type": "single_select", "required": True,
         "options": [{"value": "profile_html", "label": "Profile HTML"}, {"value": "validator", "label": "Validator"}]},
    ]
    holder = AgentRunner(
        model=scripted_model([AIMessage(content="", tool_calls=[{
            "id": "ask-annex", "name": "request_human_input",
            "args": {"reason": "Missing validator", "question": "接下来先做哪个？", "fields": fields},
        }])]), backend=StateBackend(), session_store=store,
    )
    waiting = holder.invoke("Inspect AnnexExtensionValidator")
    assert waiting.status == "waiting_human"
    target = holder.thread_id

    async def wait_until(predicate) -> None:
        async def poll() -> None:
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(poll(), timeout=3)

    async def close_original() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(holder, input=pipe, output=DummyOutput())
            app._handle_result(waiting)
            task = asyncio.create_task(app.run_async())
            try:
                await wait_until(lambda: app.application.is_running)
                # First Ctrl+C dismisses the UI; the next two clear and exit.
                pipe.send_text("\x03\x03\x03")
                await asyncio.wait_for(task, timeout=3)
                assert holder.current_interrupt() is not None
            finally:
                if not task.done():
                    app.exit()
                    await task

    asyncio.run(close_original())
    holder.close()
    runner = AgentRunner(model=scripted_model([AIMessage(content="Selected profile HTML")]), backend=StateBackend(), session_store=store)

    async def restore() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app.sessions.startup_session_id = target
            task = asyncio.create_task(app.run_async())
            try:
                await wait_until(lambda: app.application.is_running and app.interaction is not None)
                assert app.interaction.kind == "human"
                pipe.send_text("\x1b[B")
                await wait_until(lambda: app.interaction.option_index == 1)
                pipe.send_text("\r")
                await wait_until(lambda: app.interaction.index == 1)
                assert app.interaction.values == {"annex_gap": "other_repo"}
                pipe.send_text("\r")
                await wait_until(lambda: app._run_task is not None and app._run_task.done())
                assert runner.current_interrupt() is None
                state = runner.prepared.graph.get_state(runner._thread_config())
                answer = next(m for m in state.values["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "ask-annex")
                assert '"annex_gap": "other_repo"' in answer.content
                assert '"next_step": "profile_html"' in answer.content
            finally:
                if app.application.is_running:
                    app.exit()
                await task

    try:
        asyncio.run(restore())
    finally:
        runner.close()
        store.close()


def test_restored_submitted_human_answer_is_history_after_cancel(tmp_path) -> None:
    store = SessionStore(tmp_path / "answered-human.sqlite3")
    holder = AgentRunner(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "ask-annex", "name": "request_human_input",
                "args": {"reason": "Missing validator", "question": "AnnexExtensionValidator 这块对不上，接下来先做哪个？",
                         "fields": [
                             {"id": "annex_gap", "type": "text", "required": True},
                             {"id": "next_step", "type": "text", "required": True},
                         ]},
            }]),
            AIMessage(content="unused"),
        ]), backend=StateBackend(), session_store=store,
    )
    assert holder.invoke("Inspect validator").status == "waiting_human"
    values = {"annex_gap": "other_repo", "next_step": "profile_html"}

    def cancel_after_answer(event) -> None:
        if event.type == "tool_completed" and event.name == "request_human_input":
            holder.request_cancel()

    assert holder.submit_human_input(values, on_event=cancel_after_answer).status == "cancelled"
    target = holder.thread_id
    holder.close()
    runner = AgentRunner(model=scripted_model([AIMessage(content="Continue with profile HTML")]), backend=StateBackend(), session_store=store)
    try:
        snapshot = runner.switch_session(target)
        assert snapshot.interrupt_kind is None
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app.sessions.apply_snapshot(snapshot)
            assert app.interaction is None
            assert not app._reopen_pending_interaction()
            answered = next(b for b in app.state.blocks if isinstance(b, ToolBlock) and b.tool_call_id == "ask-annex")
            assert answered.status == "completed"
            assert '"annex_gap": "other_repo"' in answered.output
            assert '"next_step": "profile_html"' in answered.output
            app._io_executor.shutdown(wait=True)
        assert runner.invoke("按刚才的选择继续").status == "completed"
    finally:
        runner.close()
        store.close()


def test_working_notice_expires_without_removing_transcript(monkeypatch) -> None:
    """权限排队提示只在状态栏短暂显示，聊天记录和原工作状态保留。"""
    from agent.permission import PermissionMode

    async def scenario() -> None:
        runner = AgentRunner(model=scripted_model([AIMessage(content="unused")]), backend=StateBackend())
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            try:
                app.state.running = True
                app.set_status("Working…  Esc to cancel", transient=False)
                monkeypatch.setattr(runner, "request_permission_change", lambda mode: None)
                monkeypatch.setattr(runner, "pending_permission_mode", lambda: PermissionMode.ALLOW)
                app._request_permission_mode(PermissionMode.ALLOW)
                notice = app.state.status
                assert notice.startswith("Permission switch queued: ask → allow")
                assert any(getattr(block, "content", "") == notice for block in app.state.blocks)
                await asyncio.sleep(1.1)
                assert app.state.status == "Working…  Esc to cancel"
                assert any(getattr(block, "content", "") == notice for block in app.state.blocks)
            finally:
                app._io_executor.shutdown(wait=True)
                runner.close()

    asyncio.run(scenario())


def test_status_notices_replace_each_other_and_keep_new_events() -> None:
    """连续提示共享原状态，新审批事件不会被旧提示的超时覆盖。"""
    async def scenario() -> None:
        runner = AgentRunner(model=scripted_model([AIMessage(content="unused")]), backend=StateBackend())
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            try:
                app.set_status("Approval still pending · F2 to decide", transient=False)
                app.set_status("Thinking: expanded")
                await asyncio.sleep(0.6)
                app.set_status("Tool output: expanded")
                await asyncio.sleep(0.6)
                assert app.state.status == "Tool output: expanded"
                await asyncio.sleep(0.5)
                assert app.state.status == "Approval still pending · F2 to decide"
                app._apply_event(RunEvent(type="run_started"))
                app._apply_event(RunEvent(type="steering_queued", content="继续", result={"mode": "steer", "id": "message"}))
                assert app.state.status == "Steering queued"
                await asyncio.sleep(1.1)
                assert app.state.status == "Working…  Esc to cancel"
                app.set_status("Model queued: next")
                app.set_status("Waiting for input", transient=False)
                await asyncio.sleep(1.1)
                assert app.state.status == "Waiting for input"
            finally:
                app._io_executor.shutdown(wait=True)
                runner.close()

    asyncio.run(scenario())


def test_input_cleared_hint_expires_and_keeps_later_status() -> None:
    """清空提示自动隐藏，并且不会在超时后覆盖后来开始的任务。"""
    async def scenario() -> None:
        runner = AgentRunner(model=scripted_model([AIMessage(content="unused")]), backend=StateBackend())
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app.buffer.text = "未发送的文字"
            app._pasted_content.display("甲" * 600)
            app._skill_draft.display("alpha", "/skills/alpha/SKILL.md")
            app.state.attachments.append(object())
            original_blocks = list(app.state.blocks)
            binding = next(item for item in app.bindings.bindings if item.keys == ("c-c",))

            class Event:
                current_buffer = app.buffer

            binding.handler(Event())
            assert app.buffer.text == ""
            assert not app._pasted_content.has_blocks and not app._skill_draft.has_blocks
            assert not app.state.attachments
            assert app.state.blocks == original_blocks
            assert "Input cleared" in "".join(value for _, value in app._status_text())
            await asyncio.sleep(1.1)
            assert app.state.status == "Ready"
            assert not app._status_visible()
            app._show_input_cleared_hint()
            app._apply_event(RunEvent(type="run_started"))
            await asyncio.sleep(1.1)
            assert "Working" in "".join(value for _, value in app._status_text())
            assert "Input cleared" not in app.state.status

    asyncio.run(scenario())


@pytest.mark.parametrize("running,status", [
    (True, "Working…  Esc to cancel"),
    (True, "Cancelling…"),
    (False, "Approval still pending · F2 to decide"),
])
def test_clear_input_keeps_execution_and_pending_status(running, status) -> None:
    """任务执行或审批等待时，Ctrl+C 清空草稿但不替换状态提示。"""
    async def scenario() -> None:
        runner = AgentRunner(model=scripted_model([AIMessage(content="unused")]), backend=StateBackend())
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app.state.running = running
            app.state.status = status
            app.buffer.text = "未发送的文字"
            binding = next(item for item in app.bindings.bindings if item.keys == ("c-c",))

            class Event:
                current_buffer = app.buffer

            binding.handler(Event())
            assert app.buffer.text == ""
            assert app.state.status == status
            assert app.state.running == running
            assert "Input cleared" not in "".join(value for _, value in app._status_text())

    asyncio.run(scenario())


def test_double_ctrl_c_disables_mouse_before_leaving_full_screen() -> None:
    class RecordingOutput(DummyOutput):
        def __init__(self) -> None:
            self.events: list[str] = []
            super().__init__()

        def enable_mouse_support(self) -> None:
            self.events.append("enable_mouse")

        def disable_mouse_support(self) -> None:
            self.events.append("disable_mouse")

        def quit_alternate_screen(self) -> None:
            self.events.append("quit_alternate_screen")

    async def scenario() -> None:
        runner = AgentRunner(
            model=scripted_model([AIMessage(content="unused")]),
            backend=StateBackend(), thread_id="ctrl-c-mouse",
        )
        output = RecordingOutput()
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=output)
            task = asyncio.create_task(app.run_async())
            await asyncio.sleep(0.02)
            pipe.send_text("\x03\x03")
            await asyncio.wait_for(task, timeout=2)
        assert "enable_mouse" in output.events
        first_disable = output.events.index("disable_mouse")
        assert first_disable < output.events.index("quit_alternate_screen")
        assert "enable_mouse" not in output.events[first_disable + 1:]

    asyncio.run(scenario())

