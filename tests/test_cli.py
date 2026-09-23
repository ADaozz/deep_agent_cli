from langchain_core.messages import AIMessage, ToolMessage
from deepagents.backends import StateBackend
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

import asyncio

from agent.cli.interactions import InteractionController
from agent.cli.clipboard import ClipboardImage
from agent.cli.app import CliApplication
from agent.cli.rendering import render_transcript
from agent.cli.state import CliState, MessageBlock, ToolBlock
from agent.config import Settings
from agent.config import ModelProfile
from agent.runner import AgentRunner, RunEvent
from agent.session import SessionStore
from agent.tools.examples import build_example_tools
from agent.factory import create_agent
from tests.conftest import scripted_model


def test_runner_emits_tool_lifecycle_without_changing_result() -> None:
    events: list[RunEvent] = []
    prepared = create_agent(model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "call-docs", "name": "lookup_docs", "args": {"query": "middleware"},
            }]),
            AIMessage(content="done"),
        ]), backend=StateBackend(), extra_tools=build_example_tools())
    runner = AgentRunner(
        prepared=prepared,
        thread_id="cli-events",
    )
    result = runner.invoke("look it up", on_event=events.append)
    assert result.status == "completed"
    assert result.output == "done"
    event_types = [event.type for event in events]
    assert event_types[0] == "run_started"
    assert event_types[-1] == "run_completed"
    assert "assistant_started" in event_types
    assert "assistant_completed" in event_types
    tool_start = next(event for event in events if event.type == "tool_started")
    assert tool_start.tool_call_id == "call-docs"


def test_write_todos_renders_current_plan_and_restores_from_checkpoint(tmp_path) -> None:
    store = SessionStore(tmp_path / "plan.sqlite3")
    first_plan = [
        {"content": "Inspect files", "status": "in_progress"},
        {"content": "Run tests", "status": "pending"},
    ]
    updated_plan = [
        {"content": "Inspect files", "status": "completed"},
        {"content": "Run tests", "status": "in_progress"},
    ]
    runner = AgentRunner(model=scripted_model([
        AIMessage(content="", tool_calls=[{"id": "todo-1", "name": "write_todos", "args": {"todos": first_plan}}]),
        AIMessage(content="", tool_calls=[{"id": "todo-2", "name": "write_todos", "args": {"todos": updated_plan}}]),
        AIMessage(content="working"),
    ]), backend=StateBackend(), session_store=store)
    events: list[RunEvent] = []
    assert runner.invoke("plan the work", on_event=events.append).status == "completed"
    assert [event.result for event in events if event.type == "todos_updated"] == [first_plan, updated_plan]
    assert all(event.name != "write_todos" for event in events if event.type.startswith("tool_"))
    live_state = CliState()
    for event in events:
        live_state.apply(event)
    assert live_state.todos == updated_plan
    snapshot = runner.load_session(runner.thread_id)
    assert snapshot is not None
    assert snapshot.todos == updated_plan
    assert all(block.name != "write_todos" for block in snapshot.transcript if block.kind == "tool")
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app._apply_session_snapshot(snapshot)
        rendered = render_transcript(app.state, 80)
        assert rendered.count("Plan") == 1
        assert "✓ Inspect files" in rendered
        assert "● Run tests" in rendered
        assert "○ Run tests" not in rendered
    thread = runner.thread_id
    store.close()
    reopened = SessionStore(tmp_path / "plan.sqlite3")
    resumed = AgentRunner(model=scripted_model([AIMessage(content="unused")]),
                          backend=StateBackend(), session_store=reopened)
    assert resumed.switch_session(thread).todos == updated_plan
    reopened.close()


def test_plan_ignores_tool_calls_and_update_envelopes() -> None:
    runner = AgentRunner(model=scripted_model([AIMessage(content="unused")]), backend=StateBackend())
    events: list[RunEvent] = []
    plan = [{"content": "Inspect files", "status": "in_progress"}]
    runner._emit_update_events({
        "model": {"messages": [AIMessage(content="", tool_calls=[{
            "id": "todo-1", "name": "write_todos", "args": {"todos": plan},
        }])]},
    }, events.append)
    runner._emit_update_events({
        "tools": {"messages": [ToolMessage(content="ok", tool_call_id="todo-1", name="write_todos")]},
    }, events.append)
    runner._emit_update_events({"tools": {"todos": plan}}, events.append)
    assert not events


def test_cli_state_updates_streaming_block_in_place() -> None:
    state = CliState()
    state.apply(RunEvent(type="run_started"))
    state.apply(RunEvent(type="thinking_delta", content="first"))
    state.apply(RunEvent(type="assistant_delta", content="hello"))
    state.apply(RunEvent(type="assistant_delta", content="hello world"))
    assistants = [item for item in state.blocks if isinstance(item, MessageBlock)]
    assert len(assistants) == 1
    assert assistants[0].thinking == "first"
    assert assistants[0].content == "hello world"


def test_tool_render_is_compact_then_expandable() -> None:
    state = CliState(blocks=[ToolBlock(
        tool_call_id="exec-1",
        name="execute",
        arguments={"command": "pytest tests/"},
        output="\n".join(f"line {index}" for index in range(30)),
        status="completed",
    )])
    collapsed = render_transcript(state, 80)
    assert "execute pytest tests/" in collapsed
    assert "earlier lines" in collapsed
    assert "line 29" in collapsed
    state.tools_expanded = True
    expanded = render_transcript(state, 80)
    assert '"command"' in expanded
    assert "line 0" in expanded


def test_transcript_header_avoids_duplicate_runtime_context() -> None:
    rendered = render_transcript(CliState(), 120)
    assert "DeepAgent" in rendered
    assert "drag to copy" in rendered
    assert "/workspace/project · SANDBOXED · perm:ask" not in rendered


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
    assert controller.decision() == {
        "type": "human_input",
        "interactionId": "i-1",
        "values": {"scope": "global", "note": "later"},
    }


def test_approval_defaults_to_reject_and_escape_is_safe() -> None:
    controller = InteractionController.approval([{"name": "write_file", "args": {"file_path": "/workspace/note.txt"}}])
    assert controller.accept() is True
    assert controller.decision()["type"] == "reject"
    assert controller.decision(cancelled=True)["type"] == "reject"


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
            pipe.send_text("/quit\r")
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())


def test_footer_keeps_current_model_at_bottom_right() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        thread_id="footer-model",
        settings=Settings(),
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        footer = "".join(fragment[1] for fragment in app._footer_text())
        lines = footer.splitlines()
        assert len(lines) == 2
        assert lines[1].rstrip().endswith("qwen3.5-plus")
        assert "default ·" not in lines[1]


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
        assert any(getattr(block, "pending", False) for block in app.state.blocks)
        app.buffer.text = "later"
        app._submit_buffer("followUp")
        assert runner.control.pending_follow_up_count() == 1


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
            await app.resume_session(first[:8])
            assert app.runner.thread_id == first
            assert any(
                getattr(block, "content", "") == "saved"
                for block in app.state.blocks
            )

    asyncio.run(scenario())


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
