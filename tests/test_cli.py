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

"""Run control, session switching, paste, and compact commands."""

def test_cli_steer_and_follow_up_use_run_controller() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        thread_id="cli-steer",
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.state.running = True
        app.buffer.text = "steer now"
        app._submit_buffer("steer")
        assert runner.control.pending_steering_count() == 1
        assert not app.state.blocks
        app.buffer.text = "later"
        app._submit_buffer("followUp")
        assert runner.control.pending_follow_up_count() == 1
        assert not app.state.blocks
        assert "queued 2: steer now | later" in "".join(value for _, value in app._status_text())
        assert runner.control.pop_steering() == "steer now"
        assert [block.content for block in app.state.blocks] == ["steer now"]
        assert "queued 1: later" in "".join(value for _, value in app._status_text())
        assert runner.control.pop_follow_up() == "later"
        assert [block.content for block in app.state.blocks] == ["steer now", "later"]
        assert runner.control.pending_message_texts() == []
        assert " · queued " not in "".join(value for _, value in app._status_text())


@pytest.mark.parametrize("propose_tools", [False, True])
def test_cli_enter_during_stream_replans_before_tools(monkeypatch, propose_tools) -> None:
    from threading import Event
    from langchain_core.messages import AIMessageChunk, HumanMessage
    from langchain_core.outputs import ChatGenerationChunk

    streaming = Event()
    submitted = Event()
    calls = []
    model = scripted_model([])
    object.__setattr__(model, "streaming", True)
    model.model_fields_set.add("streaming")

    def stream(self, messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            yield ChatGenerationChunk(message=AIMessageChunk(content="Working"))
            streaming.set()
            assert submitted.wait(5), "UI did not submit while the model was streaming"
            if propose_tools:
                yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[{
                    "name": "write_file", "args": '{"file_path":"/stale.txt","content":"stale"}',
                    "id": "stale-call", "index": 0,
                }]))
        else:
            yield ChatGenerationChunk(message=AIMessageChunk(content="Corrected"))

    monkeypatch.setattr(type(model), "_stream", stream)
    runner = AgentRunner(model=model, backend=StateBackend())

    async def scenario():
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app._loop = asyncio.get_running_loop()
            try:
                app._start_run("start")
                assert await _await_cli_io(asyncio.to_thread(streaming.wait, 5), timeout=6)
                app.buffer.text = "change direction"
                app._submit_buffer("steer")
                assert runner.control.pending_steering_count() == 1
                submitted.set()
                await _await_cli_io(app._run_task, timeout=10)
                assert len(calls) == 2, runner.prepared.graph.get_state(runner._thread_config()).values["messages"]
                assert any(isinstance(m, HumanMessage) and m.content == "change direction" for m in calls[1])
                assert runner.control.pending_steering_count() == 0
                assert not any(isinstance(block, ToolBlock) for block in app.state.blocks)
                assert [block.content for block in app.state.blocks if isinstance(block, MessageBlock) and block.kind == "user"] == ["start", "change direction"]
            finally:
                submitted.set()
                app._io_executor.shutdown(wait=True)
                runner.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["steer", "followUp"])
def test_cli_drains_input_arriving_after_worker_completion(mode) -> None:
    runner = AgentRunner(model=scripted_model([
        AIMessage(content="first done"), AIMessage(content="queued done"),
    ]), backend=StateBackend())

    async def scenario():
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app._loop = asyncio.get_running_loop()
            try:
                result = runner.invoke("first")
                # The worker returned, but the UI has not handled its result.
                app.state.running = True
                app.buffer.text = "late input"
                app._submit_buffer(mode)
                assert runner.control.pending_message_texts() == ["late input"]
                app._handle_result(result)
                assert app._run_task is not None
                await _await_cli_io(app._run_task)
                assert runner.control.pending_message_texts() == []
                messages = runner.prepared.graph.get_state(runner._thread_config()).values["messages"]
                assert messages[-1].content == "queued done"
                assert [block.content for block in app.state.blocks if isinstance(block, MessageBlock) and block.kind == "user"] == ["late input"]
            finally:
                app._io_executor.shutdown(wait=True)
                runner.close()

    asyncio.run(scenario())


def test_cli_new_input_appends_to_existing_queue_when_idle(monkeypatch) -> None:
    runner = AgentRunner(model=scripted_model([AIMessage(content="unused")]), backend=StateBackend())
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        monkeypatch.setattr(app, "_start_queued_run", lambda: None)
        runner.follow_up("older")
        app.buffer.text = "newer"
        app._submit_buffer("steer")
        assert runner.control.pending_message_texts() == ["older", "newer"]
        assert not app.state.blocks
        assert app.buffer.text == ""


def test_cli_esc_requests_cancel() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        thread_id="cli-esc",
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.state.running = True
        runner.control.begin_run()
        runner.steer("please stop")
        runner.follow_up("after cancel")
        # Invoke the escape binding through the key bindings table.
        for binding in app.bindings.bindings:
            keys = getattr(binding, "keys", ())
            if keys == ("escape",) or (isinstance(keys, tuple) and keys == ("escape",)):
                class _E:
                    current_buffer = app.buffer
                binding.handler(_E())  # type: ignore[misc]
                break
        else:
            app.runner.request_cancel()
        assert runner.control.cancel_requested
        assert "please stop" in app.buffer.text
        assert "after cancel" in app.buffer.text
        assert runner.control.pending_steering_count() == 0
        assert runner.control.pending_follow_up_count() == 0
        assert "restored" in app.state.status.lower() or "Cancelling" in app.state.status


def test_cli_resume_prefix_switches_session(tmp_path) -> None:
    from agent.session import SessionStore

    store = SessionStore(tmp_path / "cli-resume.sqlite3")
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="saved")]),
        backend=StateBackend(),
        session_store=store,
    )
    first = runner.thread_id
    runner.invoke("remember")
    runner.new_session()
    assert runner.thread_id != first

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await _await_cli_io(app.resume_session(first[:8]))
            assert app.runner.thread_id == first
            assert any(
                getattr(block, "content", "") == "saved"
                for block in app.state.blocks
            )

    asyncio.run(scenario())


def test_cli_returns_queued_input_on_new_and_resume(tmp_path) -> None:
    store = SessionStore(tmp_path / "queued.sqlite3")
    runner = AgentRunner(model=scripted_model([AIMessage(content="saved")]),
                         backend=StateBackend(), session_store=store)
    original = runner.thread_id

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            runner.follow_up("first draft")
            app.new_session()
            assert runner.thread_id != original
            assert "first draft" in app.buffer.text
            assert runner.control.pending_follow_up_count() == 0

            runner.steer("second draft")
            await _await_cli_io(app.resume_session(original[:8]))
            assert runner.thread_id == original
            assert "second draft" in app.buffer.text

            runner.follow_up("third draft")
            target = next(item.id for item in runner.list_sessions() if item.id != original)
            await _await_cli_io(app.resume_session(target[:8]))
            assert runner.thread_id == target
            assert "third draft" in app.buffer.text
            assert runner.control.pending_follow_up_count() == 0

    asyncio.run(scenario())
    store.close()


def test_exact_single_image_path_paste_becomes_attachment(tmp_path) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    settings = Settings(
        llm_profiles=(ModelProfile("vision", "fake", input=("text", "image")),),
        llm_default="vision",
    )
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), thread_id="paste-image", settings=settings,
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app._handle_pasted_text(str(image_path))
            for _ in range(50):
                if app.state.attachments:
                    break
                await asyncio.sleep(0.02)
            assert len(app.state.attachments) == 1
            assert app.buffer.text == ""
            app._handle_pasted_text(f"Please inspect {image_path}")
            assert app.buffer.text == f"Please inspect {image_path}"
            app._io_executor.shutdown(wait=True)

    asyncio.run(scenario())


def test_image_path_paste_stays_text_for_text_model(tmp_path) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), thread_id="paste-text", settings=Settings(),
    )
    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app._handle_pasted_text(str(image_path))
            assert app.buffer.text == str(image_path)
            assert "pasted as text" in app.state.status

    asyncio.run(scenario())


def test_long_paste_displays_block_but_submits_full_text() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), thread_id="long-paste",
    )
    pasted = "内容" * PASTED_CONTENT_MIN_CHARS
    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app.buffer.complete_while_typing = lambda: False
            submitted: list[str] = []
            app._start_run = lambda text, **_kwargs: submitted.append(text)  # type: ignore[method-assign]
            app.buffer.insert_text("请检查 ")
            app._handle_pasted_text(pasted)
            app.buffer.insert_text(" 谢谢")
            assert app.buffer.text == f"请检查 [Pasted Content {len(pasted)} chars] 谢谢"
            with set_app(app.application):
                line = app.editor_control.create_content(80, 10).get_line(0)
            assert any("class:pasted-content" in fragment[0] for fragment in line)
            app._submit_buffer("steer")
            assert submitted == [f"请检查 {pasted} 谢谢"]
            assert app.buffer.text == ""
            assert not app._pasted_content.has_blocks
            assert f"请检查 {pasted} 谢谢" in app.buffer.history.get_strings()

    asyncio.run(scenario())


def test_edited_paste_marker_blocks_submit_until_restored() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), thread_id="edited-paste",
    )
    pasted = "内容" * PASTED_CONTENT_MIN_CHARS
    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app.buffer.complete_while_typing = lambda: False
            submitted: list[str] = []
            app._start_run = lambda text, **_kwargs: submitted.append(text)  # type: ignore[method-assign]
            app._handle_pasted_text(pasted)
            app.buffer.text = f"[Pasted Content {len(pasted) + 1} chars]"
            app._submit_buffer("steer")
            assert submitted == []
            assert app.buffer.text == f"[Pasted Content {len(pasted) + 1} chars]"
            assert any(
                "marker was edited or truncated" in getattr(block, "content", "")
                for block in app.state.blocks
            )
            app.buffer.text = f"[Pasted Content {len(pasted)} chars]"
            app._submit_buffer("steer")
            assert submitted == [pasted]

    asyncio.run(scenario())


def test_attach_gate_considers_pending_vision_model(tmp_path) -> None:
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    settings = Settings(
        llm_profiles=(
            ModelProfile("text", "model-a"),
            ModelProfile("vision", "model-b", input=("text", "image")),
        ),
        llm_default="text",
    )
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), thread_id="attach-gate", settings=settings,
    )
    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            assert not await app._attach_path(str(image_path))
            assert "Current or pending model does not support image input" in app.state.status
            runner.switch_model("vision")
            assert await app._attach_path(str(image_path))
            assert [ref.filename for ref in app.state.attachments] == ["shot.png"]
            app._io_executor.shutdown(wait=True)

    asyncio.run(scenario())


def test_long_clipboard_text_uses_same_compact_paste_path() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), thread_id="clipboard-long-paste",
    )
    pasted = "长" * PASTED_CONTENT_MIN_CHARS

    class FakeClipboard:
        def inspect(self):  # type: ignore[no-untyped-def]
            return ClipboardText(pasted)

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app.buffer.complete_while_typing = lambda: False
            app.clipboard = FakeClipboard()  # type: ignore[assignment]
            await app._paste_clipboard()
            assert app.buffer.text == f"[Pasted Content {len(pasted)} chars]"
            assert app._pasted_content.expand(app.buffer.text) == pasted
            app._io_executor.shutdown(wait=True)

    asyncio.run(scenario())


def test_clipboard_image_is_not_exported_for_text_model() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), thread_id="clipboard-gate", settings=Settings(),
    )

    class FakeClipboard:
        exported = False

        def inspect(self):
            return ClipboardImage()

        def export_image(self):
            self.exported = True
            raise AssertionError("export must happen after the capability gate")

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            fake = FakeClipboard()
            app.clipboard = fake  # type: ignore[assignment]
            await app._paste_clipboard()
            assert fake.exported is False
            assert "does not support image" in app.state.status
            app._io_executor.shutdown(wait=True)

    asyncio.run(scenario())


def test_cli_waits_for_a_busy_session_and_takes_over(tmp_path) -> None:
    store = SessionStore(tmp_path / "cli-wait.sqlite3")
    holder = AgentRunner(
        model=scripted_model([AIMessage(content="held")]),
        backend=StateBackend(),
        session_store=store,
    )
    holder.invoke("content for the picker")
    target = holder.thread_id
    waiter = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        session_store=store,
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(waiter, input=pipe, output=DummyOutput())
            await _await_cli_io(app.resume_session(target[:8]))
            assert app.sessions.wait_target == target
            assert "Waiting for session" in app.state.status
            assert app.interaction is None

            # Input cannot reach the runner while the wait is active.
            app.buffer.text = "blocked during wait"
            app._submit_buffer("steer")
            assert waiter.control.pending_steering_count() == 0
            assert app.state.running is False

            holder.close()
            await _await_cli_io(app.sessions.wait_task)
            assert app.sessions.wait_target is None
            assert waiter.thread_id == target
            assert any(getattr(block, "content", "") == "held" for block in app.state.blocks)
            waiter.close()

    asyncio.run(scenario())
    store.close()


def test_cli_esc_cancels_wait_and_returns_to_picker(tmp_path) -> None:
    store = SessionStore(tmp_path / "cli-esc-wait.sqlite3")
    holder = AgentRunner(
        model=scripted_model([AIMessage(content="held")]),
        backend=StateBackend(),
        session_store=store,
    )
    holder.invoke("content for the picker")
    target = holder.thread_id
    waiter = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        session_store=store,
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(waiter, input=pipe, output=DummyOutput())
            await _await_cli_io(app.resume_session(target[:8]))
            assert app.sessions.wait_target == target

            for binding in app.bindings.bindings:
                keys = getattr(binding, "keys", ())
                if keys == ("escape",) or (isinstance(keys, tuple) and keys == ("escape",)):
                    class _E:
                        current_buffer = app.buffer
                    binding.handler(_E())  # type: ignore[misc]
                    break

            assert app.sessions._wait_cancelled is True
            await _await_cli_io(app.sessions.wait_task)
            assert app.sessions.wait_target is None
            # Esc keeps the runner detached and reopens the picker.
            assert waiter._runtime.lease is None
            assert app.interaction is not None
            assert app.interaction.kind == "resume"
            waiter.close()

    asyncio.run(scenario())
    holder.close()
    store.close()


def test_cli_startup_resume_waits_for_a_busy_session(tmp_path) -> None:
    store = SessionStore(tmp_path / "cli-startup-wait.sqlite3")
    holder = AgentRunner(
        model=scripted_model([AIMessage(content="held")]),
        backend=StateBackend(),
        session_store=store,
    )
    holder.invoke("content for the picker")
    target = holder.thread_id
    waiter = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        session_store=store,
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(waiter, input=pipe, output=DummyOutput())
            app.sessions.startup_session_id = target
            await _await_cli_io(app.sessions.start())
            # Startup shares the controller workflow: busy means waiting UI.
            assert app.sessions.wait_target == target
            assert "Waiting for session" in app.state.status

            holder.close()
            await _await_cli_io(app.sessions.wait_task)
            assert app.sessions.wait_target is None
            assert waiter.thread_id == target
            assert any(getattr(block, "content", "") == "held" for block in app.state.blocks)
            waiter.close()

    asyncio.run(scenario())
    store.close()



def test_status_omits_permission_session_and_sandbox_details(tmp_path) -> None:
    from dataclasses import replace as dc_replace
    from agent.config import SandboxConfig
    from agent.sandbox import select_backend

    config = dc_replace(SandboxConfig(workspace=tmp_path), bwrap_path="/bin/true")
    backend = select_backend(config, check=False).backend
    runner = AgentRunner(
        prepared=create_agent(model=scripted_model([AIMessage(content="done")]), backend=backend, skills=[]),
        thread_id="sandbox-status",
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.show_status()
        status = app.state.blocks[-1].content
        for label in ("Permission:", "Thread:", "Sandbox:", "Filesystem:", "Offline sandbox:", "Networked sandbox:"):
            assert label not in status
        assert "Model:" in status and "Tools:" in status and "Average TPS:" in status
    runner.close()


@pytest.mark.parametrize("selected", [False, True])
def test_escape_completion_preserves_current_document(selected) -> None:
    from prompt_toolkit.document import Document

    def scenario(app: CliApplication) -> None:
        app.buffer.document = Document("/mod", 4)
        app.buffer.complete_state = CompletionState(app.buffer.document, [Completion("model", start_position=-3)])
        if selected:
            app.buffer.go_to_completion(0)
        before = app.buffer.document
        _escape(app)
        assert app.buffer.complete_state is None
        assert app.buffer.document == before
        assert not app.runner.control.cancel_requested
        app.buffer.insert_text("x")
        assert app.buffer.text == before.text + "x"
        app.buffer.document = Document("/mod", 4)
        app.buffer.complete_state = CompletionState(app.buffer.document, [Completion("model", start_position=-3)])
        tab = next(b for b in app.bindings.bindings if b.keys == ("c-i",))
        class Event:
            current_buffer = app.buffer
        tab.handler(Event())
        assert app.buffer.text.startswith("/model")
        assert app.buffer.complete_state is None

    _with_painted_app("escape-completion", scenario)


@pytest.mark.parametrize("gap,exits", [(0.3, True), (0.7, True), (0.999, True), (1.0, False), (1.2, False)])
def test_ctrl_c_exit_window_boundaries(monkeypatch, gap, exits) -> None:
    from types import SimpleNamespace
    import agent.cli.app as cli_module

    def scenario(app: CliApplication) -> None:
        clock = [0.0]
        monkeypatch.setattr(cli_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        exited = []
        monkeypatch.setattr(app, "exit", lambda: exited.append(True))
        binding = next(b for b in app.bindings.bindings if b.keys == ("c-c",))
        class Event:
            current_buffer = app.buffer
        app.buffer.text = "draft"
        binding.handler(Event())
        assert not exited and app.buffer.text == ""
        clock[0] = gap
        app.buffer.text = "another draft"
        binding.handler(Event())
        assert bool(exited) is exits
        if not exits:
            assert app.buffer.text == ""
            assert app._last_ctrl_c == gap
            clock[0] += 0.7
            binding.handler(Event())
            assert exited

    _with_painted_app("ctrl-c-boundary", scenario)


def test_context_uses_only_api_input_and_preserves_zero():
    assert format_context_usage({"input_tokens": 0, "total_tokens": 90000}, 128000) == "128k Context · 0.0% used"
    assert format_context_usage({"total_tokens": 90000}, 128000) == "128k Context · Unknown"
    assert format_context_usage({"input_tokens": 1280, "total_tokens": 90000}, 128000) == "128k Context · 1.0% used"


@pytest.mark.parametrize("event_type", ["run_started", "assistant_started", "compaction_finished", "run_cancelled", "run_failed"])
def test_context_lifecycle_preserves_previous_usage(event_type):
    from agent.cli.state import CliState
    state = CliState(usage={"input_tokens": 500})
    state.apply(RunEvent(type=event_type))
    assert state.usage == {"input_tokens": 500}
    state.apply(RunEvent(type="usage", result={"input_tokens": 0}))
    assert state.usage == {"input_tokens": 0}
    state.apply(RunEvent(type="usage", result={}))
    assert state.usage == {"input_tokens": 0}


@pytest.mark.parametrize("usage", [{}, {"output_tokens": 12}, {"total_tokens": 512},
                                   {"input_tokens": None}, {"input_tokens": -1},
                                   {"input_tokens": True}])
def test_context_ignores_usage_without_valid_input_tokens(usage):
    from agent.cli.state import CliState
    state = CliState(usage={"input_tokens": 500, "output_tokens": 10, "total_tokens": 510})
    state.apply(RunEvent(type="usage", result=usage))
    assert state.usage == {"input_tokens": 500, "output_tokens": 10, "total_tokens": 510}
    state.apply(RunEvent(type="usage", result={"input_tokens": 750}))
    assert state.usage == {"input_tokens": 750}


def test_escape_before_worker_starts_cancels_the_dispatched_run() -> None:
    """C16 in the TUI: Esc lands after running=True but before begin_run."""
    import threading

    calls = []

    class CountingModel(type(scripted_model([]))):
        def _generate(self, *args, **kwargs):
            calls.append(1)
            return super()._generate(*args, **kwargs)

    runner = AgentRunner(model=CountingModel(messages=iter([AIMessage(content="never")])),
                         backend=StateBackend(), thread_id="esc-before-start")

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app._loop = asyncio.get_running_loop()
            gate = threading.Event()
            blockers = [app._io_executor.submit(gate.wait, 5) for _ in range(2)]
            app._start_run("dispatched")
            assert app.state.running
            _escape(app)
            _escape(app)
            gate.set()
            for blocker in blockers:
                blocker.result(5)
            await asyncio.wait_for(app._run_task, 5)
            assert not app.state.running
            assert app.state.status == "Cancelled"
            assert calls == []
            assert any("Operation cancelled." in getattr(block, "content", "") for block in app.state.blocks)
            app._io_executor.shutdown(wait=True)

    try:
        asyncio.run(scenario())
    finally:
        runner.close()


def test_repeated_escape_sends_one_cancel_per_run() -> None:
    runner = AgentRunner(model=scripted_model([AIMessage(content="ok")]), backend=StateBackend(),
                         thread_id="esc-once")
    sent = []
    runner.request_cancel = lambda: sent.append(1)  # type: ignore[method-assign]
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.state.running = True
        for _ in range(4):
            _escape(app)
        assert sent == [1]
        assert "Cancelling" in app.state.status
        app._cancel_sent = False
        app.state.running = False
    runner.close()


def test_stale_run_events_do_not_reach_the_current_run() -> None:
    runner = AgentRunner(model=scripted_model([AIMessage(content="ok")]), backend=StateBackend(),
                         thread_id="stale-events")
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app._run_epoch = 2
        app.state.running = True
        app._apply_run_event(RunEvent(type="run_cancelled"), 1)
        app._apply_run_event(RunEvent(type="assistant_delta", content="old run text"), 1)
        assert app.state.running
        assert not any("old run text" in getattr(block, "content", "") for block in app.state.blocks)
        app._apply_run_event(RunEvent(type="assistant_delta", content="current text"), 2)
        assert any(getattr(block, "content", "") == "current text" for block in app.state.blocks)
        # Control events from the UI thread carry no epoch and always apply.
        app._apply_run_event(RunEvent(type="run_cancelling"), None)
        assert app.state.status == "Cancelling…"
    runner.close()


def test_unconfirmed_cancel_cleanup_is_reported() -> None:
    runner = AgentRunner(model=scripted_model([AIMessage(content="ok")]), backend=StateBackend(),
                         thread_id="cancel-unclosed")
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.state.running = True
        app._handle_result(RunResult(status="cancelled", error="Cancelled, but the model request did not confirm it stopped"))
        assert not app.state.running
        assert app.state.status == "Cancel cleanup not confirmed"
        assert any("did not confirm" in getattr(block, "content", "") for block in app.state.blocks)
    runner.close()


def test_removed_tool_argument_aliases_do_not_create_diff_previews() -> None:
    from agent.cli.previews import build_diff_preview

    assert build_diff_preview("edit_file", {
        "file_path": "/workspace/a.py", "old_text": "old", "new_text": "new",
    }) is None


def test_web_search_preview_does_not_parse_output_for_missing_artifact() -> None:
    from agent.cli.previews import build_web_search_preview

    preview = build_web_search_preview(ToolBlock(
        "search", "web_search", {"query": "example"}, status="completed",
        output="Web search results for: example\n5 results",
    ))
    assert preview.detail == ""


@pytest.mark.parametrize("decision", ["dismiss", "approve", "reject"])
def test_arriving_approval_keeps_typing_and_restores_draft_after_f2(decision) -> None:
    async def scenario() -> None:
        runner = AgentRunner(model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "draft-write", "name": "write_file",
                "args": {"file_path": "/draft.txt", "content": "hello"},
            }]),
            AIMessage(content="done"),
        ]), backend=StateBackend())
        waiting = runner.invoke("write")
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            task = asyncio.create_task(app.run_async())

            async def wait_until(predicate) -> None:
                for _ in range(300):
                    if predicate():
                        return
                    await asyncio.sleep(0.01)
                raise TimeoutError("approval input did not update")

            try:
                await wait_until(lambda: app.application.is_running)
                pipe.send_text("draft")
                await wait_until(lambda: app.buffer.text == "draft")
                app._handle_result(waiting)
                assert app.interaction is None
                assert "F2" in app.state.status
                pipe.send_text(" 中文\x1b[D")
                await wait_until(lambda: app.buffer.text == "draft 中文" and app.buffer.cursor_position == 7)
                document = app.buffer.document
                pipe.send_text("\x1bOQ")
                await wait_until(lambda: app.interaction is not None)
                assert app.interaction.kind == "approval"
                assert app.buffer.text == ""
                if decision == "dismiss":
                    pipe.send_text("\x03")
                    await wait_until(lambda: app.interaction is None)
                    assert runner.current_interrupt() is not None
                else:
                    if decision == "approve":
                        pipe.send_text("\x1b[B")
                        await wait_until(lambda: app.interaction.option_index == 1)
                    pipe.send_text("\r")
                    await wait_until(lambda: app._run_task is not None and app._run_task.done())
                    assert runner.current_interrupt() is None
                assert app.buffer.document == document
                assert app._approval_draft is None
            finally:
                app.exit()
                await asyncio.wait_for(task, timeout=3)
                runner.close()

    asyncio.run(scenario())


def test_approval_preserves_pasted_content_and_queues_new_input() -> None:
    async def scenario() -> None:
        runner = AgentRunner(model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "paste-write", "name": "write_file",
                "args": {"file_path": "/paste.txt", "content": "hello"},
            }]), AIMessage(content="done"),
        ]), backend=StateBackend())
        waiting = runner.invoke("write")
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            try:
                payload = "long pasted content\n" * 100
                app._handle_pasted_text(payload)
                displayed = app.buffer.text
                assert app._pasted_content.has_blocks
                app._handle_result(waiting)
                assert app.interaction is None
                app._reopen_pending_interaction()
                app._finish_interaction(cancelled=True)
                assert app.buffer.text == displayed
                assert app._pasted_content.expand(displayed) == payload
                app._submit_buffer("steer")
                assert runner.control.pending_message_texts() == [payload]
                assert app.buffer.text == ""
                assert app._run_task is None
                assert runner.current_interrupt() is not None
                assert "F2" in app.state.status
            finally:
                app._io_executor.shutdown(wait=True)
                runner.close()

    asyncio.run(scenario())


def test_status_metrics_are_compact_indented_and_grouped_by_model_effort():
    runner = AgentRunner(model=scripted_model([AIMessage(content="unused")]), backend=StateBackend(), settings=Settings())
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.state.apply(RunEvent(type="run_started"))
        for metrics in [
            {"model": "deepseek-flash", "reasoning_effort": "low", "tps": 75.0, "reasoning_tps": 66.7, "answer_tps": 100.0},
            {"model": "deepseek-flash", "reasoning_effort": "high", "tps": 20.0},
            {"model": "qwen", "tps": 200.0},
        ]:
            app.state.apply(RunEvent(type="model_metrics", result=metrics))
        assert "TPS" not in "".join(value for _, value in app._status_text())
        text = app._model_metrics_lines()
        assert text.startswith("deepseek-flash-low\n  5% low TPS: 75.0")
        assert "\ndeepseek-flash-high\n  5% low TPS: 20.0" in text
        assert "\nqwen\n  5% low TPS: 200.0" in text
        for section in text.split("\n\n"):
            lines = section.splitlines()
            assert len(lines) == 4 and all(line.startswith("  ") for line in lines[1:])
        assert "Prompt Tokens" not in text and "client-observed" not in text and "Duration" not in text
        for label in ("Total Output TPS:", "Reasoning TPS:", "Answer TPS:"):
            assert label not in text
        app.state.apply(RunEvent(type="run_completed"))
        assert not app._status_visible()
        app.show_status()
        assert text in app.state.blocks[-1].content


def test_status_metrics_restore_on_restart_switch_and_clear(tmp_path):
    from agent.session import SessionStore
    from agent.cli.commands import _clear

    path = tmp_path / "metrics.sqlite3"
    store = SessionStore(path)
    runner = AgentRunner(model=scripted_model([]), backend=StateBackend(), session_store=store, settings=Settings())
    first_id = runner.thread_id
    reports = [{"model": "deepseek-flash", "reasoning_effort": "low", "tps": value} for value in [10.0, 20.0]]
    for report in reports:
        store.append_model_metrics(first_id, report)
    second_id = runner.new_session().id
    store.append_model_metrics(second_id, {"model": "qwen", "tps": 200.0})
    runner.close()
    store.close()
    store = SessionStore(path)
    runner = AgentRunner(model=scripted_model([]), backend=StateBackend(), session_store=store,
                         thread_id=first_id, settings=Settings())
    try:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            assert app.state.model_metrics_history == reports
            assert "  Average TPS: 15.0" in app._model_metrics_lines()
            assert "  5% low TPS: 10.0" in app._model_metrics_lines()
            asyncio.run(_clear(app, ""))
            assert app.state.model_metrics_history == reports
            app.sessions.apply_snapshot(runner.switch_session(second_id))
            assert app._model_metrics_lines().startswith("qwen\n")
            assert "  Average TPS: 200.0" in app._model_metrics_lines()
            assert "deepseek" not in app._model_metrics_lines()
            app._apply_session_snapshot(runner.switch_session(first_id))
            assert "  Average TPS: 15.0" in app._model_metrics_lines()
            app.sessions.new_session()
            assert app.state.model_metrics_history == []
    finally:
        runner.close()
        store.close()


def test_mcp_logs_use_background_file_and_restore_handlers(capsys, tmp_path) -> None:
    from fastmcp.client.logging import LogMessage, default_log_handler

    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.buffer.text = "unfinished input"
        app._mcp_log_path = tmp_path / "logs" / "mcp.log"
        before = len(app.state.blocks)
        logger = logging.getLogger("fastmcp")
        previous = (logger.handlers[:], logger.propagate)
        with app._capture_runtime_logs():
            handlers = logger.handlers[:]
            with app._capture_runtime_logs():
                asyncio.run(default_log_handler(LogMessage(level="critical", data="server alert")))
            assert logger.handlers == handlers  # UI exit keeps the outer shutdown log scope.
            logging.getLogger("fastmcp.client.auth.oauth").warning("OAuth callback waiting")
            logging.getLogger("mcp").warning("Shutdown cleanup completed")
            assert len(app.state.blocks) == before
            logs = app._mcp_log_path.read_text()
            assert "Received CRITICAL from server: server alert" in logs
            assert "OAuth callback waiting" in logs
            assert "Shutdown cleanup completed" in logs
            assert app.buffer.text == "unfinished input"
        assert (logger.handlers, logger.propagate) == previous
        assert capsys.readouterr().err == ""
    runner.close()


def test_long_chinese_choices_have_one_selected_row_and_no_second_wrap() -> None:
    from prompt_toolkit.formatted_text import to_formatted_text
    from prompt_toolkit import ANSI
    from agent.cli.rendering import render_interaction

    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.interaction = InteractionController.human({"question": "这个标识是什么用途？", "fields": [{
            "id": "purpose", "type": "single_select", "options": [
                {"value": "auth", "label": "是 OAuth 授权码或标识，需要进一步操作", "description": "授权流程里的 code / client_id / session_id，想检查或验证"},
                {"value": "typo", "label": "贴错了，不用理", "description": "忽略这次粘贴，继续之前的任务"},
                {"value": "task", "label": "是另一个任务的输入", "description": "以这个标识执行下一步任务"},
            ],
        }]})
        window = next(w for w in app.application.layout.find_all_windows() if w.content is app.interaction_control)
        assert not window.wrap_lines()
        for index in range(4):
            app.interaction.option_index = index
            fragments = to_formatted_text(ANSI(render_interaction(app.interaction, 40)))
            text = "".join(item[1] for item in fragments)
            assert text.count("→") == 1
            assert "Other:" in text
            assert "Enter confirm" in text
    runner.close()


def test_python_warnings_do_not_overwrite_tui_input(capsys) -> None:
    import warnings

    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.buffer.text = "keep this input"
        previous = warnings.showwarning
        with app._capture_runtime_logs():
            warnings.warn("OAuth cache warning regression", UserWarning)
            assert app.state.blocks[-1].content == "UserWarning: OAuth cache warning regression"
            assert app.buffer.text == "keep this input"
        assert warnings.showwarning is previous
        assert capsys.readouterr().err == ""
    runner.close()
