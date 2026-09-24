from langchain_core.messages import AIMessage, ToolMessage
from deepagents.backends import StateBackend
from prompt_toolkit.data_structures import Point
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent
from prompt_toolkit.output import DummyOutput

import asyncio
import re
import pytest
from collections.abc import Callable

from agent.cli.gitinfo import GitSummary, parse_status
from agent.cli.interactions import InteractionController
from agent.cli.clipboard import ClipboardImage
from agent.cli.app import (
    FOOTER_LINES,
    CliApplication,
    format_context_usage,
    format_context_window,
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
from agent.cli.state import CliState, MessageBlock, ToolBlock
from agent.config import Settings
from agent.config import ModelProfile
from agent.runner import AgentRunner, RunEvent
from agent.session import SessionStore, TranscriptBlock
from agent.tools.examples import build_example_tools
from agent.factory import DEFAULT_FS_TOOLS, create_agent
from tests.conftest import scripted_model


_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _plain(rendered: str) -> str:
    """Strip SGR sequences so assertions survive Rich splitting a run mid-word."""
    return _ANSI_SGR.sub("", rendered)


def test_user_bubble_has_prefix_and_colored_blank_rows(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    rendered = _capture(_message(MessageBlock(kind="user", content="hello"), False)[0], 30)
    lines = rendered.splitlines()
    assert len(lines) == 3
    assert _plain(lines[0]).strip() == ""
    assert _plain(lines[1]).lstrip().startswith("› hello")
    assert _plain(lines[2]).strip() == ""
    assert all("48;2;48;48;48" in line for line in lines)


def test_editor_has_matching_blank_rows_and_prefix() -> None:
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        editor = app.application.layout.container.content.children[4].content
        top, middle, bottom = editor.children
        prefix, input_window = middle.children
        assert top.height == bottom.height == 1
        assert top.style == bottom.style == prefix.style == input_window.style == "class:editor"
        assert prefix.content.text == "› "
        assert input_window.content is app.editor_control


def test_running_tools_use_a_green_spinner() -> None:
    running = ToolBlock("call", "execute", {"command": "pwd"}, status="running")
    for renderable in (_tool(running, False), _explore_group([
        ToolBlock("list", "ls", {}, status="running"),
    ])):
        title = renderable.renderable.renderables[0]
        assert title.plain[0] in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        assert title.spans[0].style == "green"
        assert not title.plain.startswith("●")


def test_cli_rejects_keybindings_inside_workspace(tmp_path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    settings = Settings.from_mapping({"sandbox": {"workspace": str(workspace)}})
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend(), settings=settings)
    with pytest.raises(ValueError, match="keybindings directory must be outside workspace"):
        CliApplication(runner, config_dir=workspace, output=DummyOutput())


def test_session_command_shows_created_and_updated_timestamps(tmp_path) -> None:
    store = SessionStore(tmp_path / "session-info.sqlite3")
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        session_store=store,
    )
    info = store.get(runner.thread_id)
    assert info is not None
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.show_session()
        output = app.state.blocks[-1].content
    assert f"Created: {info.created_at.astimezone().isoformat(sep=' ', timespec='seconds')}" in output
    assert f"Updated: {info.updated_at.astimezone().isoformat(sep=' ', timespec='seconds')}" in output
    store.close()


def test_resume_menu_lists_sessions_with_content(tmp_path) -> None:
    store = SessionStore(tmp_path / "resume-menu.sqlite3")
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="first"), AIMessage(content="second")]),
        backend=StateBackend(),
        session_store=store,
    )
    runner.invoke("remember this")
    first = runner.thread_id
    runner.new_session()
    runner.invoke("another thread")
    sessions = [info for info in runner.list_sessions() if info.id in {first, runner.thread_id}]

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await app.resume_session()
            assert app.interaction is not None
            assert app.interaction.kind == "resume"
            options = app.interaction.fields[0]["options"]
            assert {option["value"] for option in options} == {item.id for item in sessions}
            for width in (50, 100):
                rendered = re.sub(r"\x1b\[[0-9;]*m", "", render_interaction(app.interaction, width))
                positions = []
                for info in sessions:
                    option = next(option for option in options if option["value"] == info.id)
                    timestamp = info.updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                    assert option["right_label"] == timestamp
                    assert info.title[:40] in option["label"]
                    line = next(line for line in rendered.splitlines() if info.id[:8] in line)
                    assert line.endswith(timestamp)
                    positions.append(line.index(timestamp))
                assert len(set(positions)) == 1

    asyncio.run(scenario())
    store.close()


def test_resume_menu_skips_empty_new_sessions(tmp_path) -> None:
    store = SessionStore(tmp_path / "resume-empty.sqlite3")
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        session_store=store,
    )
    runner.new_session()

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await app.resume_session()
            assert app.interaction is None
            assert any("No sessions with conversation content" in block.content for block in app.state.blocks)

    asyncio.run(scenario())
    store.close()


def test_exit_hint_skips_empty_thread_and_shows_model_after_content(tmp_path) -> None:
    store = SessionStore(tmp_path / "exit-hint.sqlite3")
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="saved")]),
        backend=StateBackend(),
        session_store=store,
        settings=Settings(),
    )
    with create_pipe_input() as pipe:
        empty = CliApplication(runner, input=pipe, output=DummyOutput())
        assert empty.continue_session_message() is None
    runner.invoke("remember")
    with create_pipe_input() as pipe:
        used = CliApplication(runner, input=pipe, output=DummyOutput())
        message = used.continue_session_message()
    assert message is not None
    assert f"deep-agent resume {runner.thread_id}" in message
    assert "Model: qwen3.5-plus" in message
    store.close()


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
        rendered = _plain(render_transcript(app.state, 80))
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


def test_explore_tools_collapse_into_summary() -> None:
    assert {"ls", "read_file", "glob", "grep"}.issubset(DEFAULT_FS_TOOLS)
    state = CliState(blocks=[
        ToolBlock(
            tool_call_id="r1", name="read_file",
            arguments={"file_path": "/workspace/a.py", "offset": 0, "limit": 2000},
            output="src", status="completed",
        ),
        ToolBlock(
            tool_call_id="g1", name="grep",
            arguments={"pattern": "foo", "path": "/workspace", "output_mode": "files_with_matches"},
            output="/workspace/a.py", status="completed",
        ),
        ToolBlock(
            tool_call_id="g2", name="grep",
            arguments={"pattern": "bar", "path": "/workspace", "glob": "*.py", "output_mode": "content"},
            output="hit", status="completed",
        ),
        ToolBlock(
            tool_call_id="gl1", name="glob",
            arguments={"pattern": "*.py", "path": "/workspace"},
            output="/workspace/a.py", status="completed",
        ),
        ToolBlock(
            tool_call_id="gl2", name="glob",
            arguments={"pattern": "*.md", "path": "/workspace"},
            output="/workspace/README.md", status="completed",
        ),
        ToolBlock(
            tool_call_id="gl3", name="glob",
            arguments={"pattern": "*.txt", "path": "/workspace"},
            output="/workspace/note.txt", status="completed",
        ),
        ToolBlock(
            tool_call_id="ls1", name="ls",
            arguments={"path": "/workspace"},
            output="/workspace/a.py", status="completed",
        ),
        ToolBlock(
            tool_call_id="e1", name="execute",
            arguments={"command": "pytest"},
            output="ok", status="completed",
        ),
        ToolBlock(
            tool_call_id="w1", name="write_file",
            arguments={"file_path": "/workspace/out.txt", "content": "x"},
            output="Wrote /workspace/out.txt", status="completed",
        ),
    ])
    collapsed = render_transcript(state, 80)
    assert "Read, grepped, globbed, listed 1 file, 2 greps, 3 globs, 1 listing" in collapsed
    assert "7 items hidden" in collapsed
    assert "/workspace/a.py" not in collapsed
    assert "execute pytest" in collapsed
    assert "write /workspace/out.txt" in collapsed
    state.tools_expanded = True
    expanded = render_transcript(state, 80)
    assert "read /workspace/a.py" in expanded
    assert "items hidden" not in expanded


def test_approval_pane_stays_decision_only() -> None:
    command = "python -m pytest tests/test_cli.py tests/test_runner.py -q --tb=short"
    controller = InteractionController.approval([{
        "toolCallId": "ex-1", "name": "execute",
        "args": {"command": command, "network": True},
    }])
    rendered = re.sub(r"\x1b\[[0-9;]*m", "", render_interaction(controller, 80))
    assert command not in rendered
    assert "NETWORK" in rendered
    assert "Approve tool call?" in rendered
    state = CliState(blocks=[ToolBlock(
        tool_call_id="ex-1", name="execute",
        arguments={"command": command, "network": True},
        status="waiting",
    )])
    assert "execute python -m pytest tests/test_cli.py" in render_transcript(state, 120)


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
    assert "22 output lines hidden" in collapsed
    assert "line 29" in collapsed
    assert "line 0" not in collapsed
    state.tools_expanded = True
    expanded = render_transcript(state, 80)
    assert "line 0" in expanded
    assert "output lines hidden" not in expanded


def test_edit_waiting_shows_short_diff_preview() -> None:
    old = "\n".join(f"keep-{index}" for index in range(20))
    new = "replaced"
    state = CliState(blocks=[ToolBlock(
        tool_call_id="edit-1",
        name="edit_file",
        arguments={"file_path": "/workspace/tests/test_cache.py", "old_string": old, "new_string": new},
        status="waiting",
    )])
    rendered = render_transcript(state, 80)
    assert "edit /workspace/tests/test_cache.py" in rendered
    assert "waiting for input" in rendered
    assert "--- a/workspace/tests/test_cache.py" in rendered
    assert "Ctrl+R to review" in rendered
    assert "keep-15" not in rendered
    review = render_review([("edit_file", {
        "file_path": "/workspace/tests/test_cache.py", "old_string": old, "new_string": new,
    })], 80)
    assert "keep-15" in review


def test_write_and_delete_previews_use_real_schema() -> None:
    write = render_transcript(CliState(blocks=[ToolBlock(
        tool_call_id="w1", name="write_file",
        arguments={"file_path": "/workspace/out.txt", "content": "hello"},
        status="waiting",
    )]), 80)
    assert "write /workspace/out.txt" in write
    assert "/dev/null" in write
    assert "+hello" in write
    delete = render_transcript(CliState(blocks=[ToolBlock(
        tool_call_id="d1", name="delete",
        arguments={"file_path": "/workspace/out.txt"},
        status="waiting",
    )]), 80)
    assert "delete /workspace/out.txt" in delete


def test_truncated_tool_completion_keeps_stream_and_shows_log_path() -> None:
    state = CliState()
    state.apply(RunEvent(type="tool_started", tool_call_id="exec-log", name="execute"))
    state.apply(RunEvent(
        type="tool_output_delta", tool_call_id="exec-log",
        content="first-eight\n\n... Output truncated at 8 bytes.",
    ))
    state.apply(RunEvent(
        type="tool_completed", tool_call_id="exec-log", name="execute",
        content=(
            "last-eight\n\n[Output truncated: showing the last 8 bytes.\n"
            "Full output saved to: /current/workspace/.deep-agent/logs/exec/example.log\n"
            "Agent path: /workspace/.deep-agent/logs/exec/example.log]"
        ),
        result={
            "exit_code": 0, "truncated": True, "max_output_bytes": 8,
            "host_log_path": "/current/workspace/.deep-agent/logs/exec/example.log",
            "agent_log_path": "/workspace/.deep-agent/logs/exec/example.log",
        },
    ))
    output = state.blocks[-1].output
    assert output.startswith("first-eight")
    assert "last-eight" not in output
    assert output.count("Full output saved to:") == 1
    assert "/current/workspace/.deep-agent/logs/exec/example.log" in output
    assert "/workspace/.deep-agent/logs/exec/example.log" in output


def test_execute_completion_uses_artifact_instead_of_output_text() -> None:
    state = CliState()
    state.apply(RunEvent(type="tool_started", tool_call_id="exec-marker", name="execute"))
    state.apply(RunEvent(
        type="tool_output_delta", tool_call_id="exec-marker", content="Exit code: 7\nCancelled by user.\n",
    ))
    state.apply(RunEvent(
        type="tool_completed", tool_call_id="exec-marker", name="execute",
        content="Exit code: 7\nCancelled by user.",
        result={"exit_code": 0, "truncated": False, "termination_reason": None},
    ))
    assert state.blocks[-1].output == "Exit code: 7\nCancelled by user.\n"
    assert not state.blocks[-1].is_error

    state.apply(RunEvent(type="tool_started", tool_call_id="exec-failed", name="execute"))
    state.apply(RunEvent(type="tool_output_delta", tool_call_id="exec-failed", content="failed\n"))
    state.apply(RunEvent(
        type="tool_completed", tool_call_id="exec-failed", name="execute",
        content="failed\n\nExit code: 2", result={"exit_code": 2, "truncated": False}, is_error=True,
    ))
    assert state.blocks[-1].output.endswith("Exit code: 2")
    assert state.blocks[-1].is_error


def test_run_cancelled_keeps_streamed_tool_output() -> None:
    state = CliState()
    state.apply(RunEvent(type="tool_started", tool_call_id="exec-c", name="execute"))
    state.apply(RunEvent(type="tool_output_delta", tool_call_id="exec-c", content="already produced\n"))
    state.apply(RunEvent(type="run_cancelled"))
    tool = state.blocks[0]
    assert isinstance(tool, ToolBlock)
    assert tool.output.startswith("already produced")
    assert "Cancelled by user" in tool.output
    assert tool.status == "error"
    assert tool.is_error
    assert state.status == "Cancelled"


def test_load_transcript_keeps_failed_execute_error() -> None:
    state = CliState()
    state.load_transcript([
        TranscriptBlock(
            kind="tool",
            tool_call_id="exec-failed",
            name="execute",
            content="failed\n\nExit code: 2",
            is_error=True,
            status="error",
        ),
    ])
    tool = state.blocks[0]
    assert isinstance(tool, ToolBlock)
    assert tool.is_error
    assert tool.status == "error"


def test_transcript_header_avoids_duplicate_runtime_context() -> None:
    rendered = render_transcript(CliState(), 120)
    assert "DeepAgent" in rendered
    assert "Ctrl+O tools" in rendered
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
    assert controller.interaction_id == "i-1"
    assert controller.values == {"scope": "global", "note": "later"}


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
            assert any("F2 to decide" in block.content for block in app.state.blocks)

            app._reopen_pending_interaction()
            assert app.interaction is not None
            assert app.interaction.kind == "approval"

            app._toggle_review()
            assert app._reviewing
            app._close_review()
            assert app.interaction is not None
            assert runner.current_interrupt().kind.value == "waiting_confirmation"

    asyncio.run(scenario())
    runner.approve_tool = approve
    runner.reject_tool = reject


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
            pipe.send_text("/quit\r")
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())


def _wheel(event_type: object) -> object:
    return MouseEvent(
        position=Point(x=0, y=0),
        event_type=event_type,
        button=MouseButton.LEFT,
        modifiers=frozenset(),
    )


def _paint(app: CliApplication) -> None:
    """Render one frame. `_redraw` is a no-op until the application is running."""
    app.application.render_counter += 1
    app.application.renderer.render(app.application, app.application.layout)


def _with_painted_app(
    thread_id: str,
    scenario: Callable[[CliApplication], None],
    *,
    lines: int = 80,
) -> None:
    """Run `scenario` against an app that painted a taller-than-viewport transcript.

    Painting needs a running loop: `Application.invalidate` schedules the redraw
    through it, and `_redraw` is a no-op until the application is running.
    """
    async def driver() -> None:
        runner = AgentRunner(
            model=scripted_model([AIMessage(content="unused")]),
            backend=StateBackend(),
            thread_id=thread_id,
        )
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            for index in range(lines):
                app.state.add_system(f"filler line {index}")
            _paint(app)
            scenario(app)

    asyncio.run(driver())


def test_tui_mouse_scroll_updates_transcript_anchor() -> None:
    from prompt_toolkit.mouse_events import MouseEventType

    def scenario(app: CliApplication) -> None:
        assert app.application.mouse_support()
        assert app.transcript_rows() > app.transcript_viewport_rows()

        assert app._transcript_anchor is None
        assert app.transcript_control.mouse_handler(_wheel(MouseEventType.SCROLL_UP)) is None
        assert app._transcript_anchor == app.transcript_max_scroll() - 3

        click = MouseEvent(
            position=Point(x=0, y=0),
            event_type=MouseEventType.MOUSE_DOWN,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
        assert app.interaction_control.mouse_handler(click) is None

    _with_painted_app("mouse-scroll", scenario)


def test_transcript_scroll_down_moves_viewport_back() -> None:
    """Scrolling down used to be a no-op: prompt_toolkit clamped to the old offset."""
    from prompt_toolkit.mouse_events import MouseEventType

    def scenario(app: CliApplication) -> None:
        maximum = app.transcript_max_scroll()
        assert maximum > 9

        for _ in range(3):
            app.transcript_control.mouse_handler(_wheel(MouseEventType.SCROLL_UP))
            _paint(app)
        assert app.transcript_window.vertical_scroll == maximum - 9

        app.transcript_control.mouse_handler(_wheel(MouseEventType.SCROLL_DOWN))
        _paint(app)
        assert app.transcript_window.vertical_scroll == maximum - 6

        for _ in range(2):
            app.transcript_control.mouse_handler(_wheel(MouseEventType.SCROLL_DOWN))
            _paint(app)
        assert app._transcript_anchor is None
        assert app.transcript_window.vertical_scroll == maximum

    _with_painted_app("scroll-down", scenario)


def test_transcript_page_down_follows_page_up() -> None:
    def scenario(app: CliApplication) -> None:
        maximum = app.transcript_max_scroll()
        app.scroll_transcript_to(0)
        _paint(app)
        assert app.transcript_window.vertical_scroll == 0

        app.scroll_transcript(10)
        _paint(app)
        assert app.transcript_window.vertical_scroll == 10

        app.scroll_transcript_to(maximum + 50)
        _paint(app)
        assert app._transcript_anchor is None
        assert app.transcript_window.vertical_scroll == maximum

    _with_painted_app("page-scroll", scenario)


def test_transcript_scrollbar_margin_is_clickable() -> None:
    """prompt_toolkit registers no handler for margins, so dragging did nothing."""
    from prompt_toolkit.mouse_events import MouseEventType

    def scenario(app: CliApplication) -> None:
        handlers = app.application.renderer.mouse_handlers
        column = app.application.output.get_size().columns - 1
        assert handlers.mouse_handlers[0][column] == app.transcript_window._scrollbar_mouse_handler

        top = _wheel(MouseEventType.MOUSE_DOWN)
        assert app.transcript_window._scrollbar_mouse_handler(top) is None
        assert app._transcript_anchor == 0

        bottom = MouseEvent(
            position=Point(x=column, y=app.transcript_viewport_rows() - 1),
            event_type=MouseEventType.MOUSE_DOWN,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
        assert app.transcript_window._scrollbar_mouse_handler(bottom) is None
        assert app._transcript_anchor is None

    _with_painted_app("scrollbar-drag", scenario)


def test_scrollbar_thumb_drag_preserves_grab_position_and_stops_on_release() -> None:
    from prompt_toolkit.mouse_events import MouseEventType

    def scenario(app: CliApplication) -> None:
        window = app.transcript_window
        info = window.render_info
        assert info is not None
        assert info.vertical_scroll == app.transcript_max_scroll()
        thumb_top = int(info.window_height * info.vertical_scroll / info.content_height)
        assert thumb_top > 1

        def event(kind: MouseEventType, row: int) -> MouseEvent:
            return MouseEvent(
                position=Point(x=app.application.output.get_size().columns - 1, y=row),
                event_type=kind, button=MouseButton.LEFT, modifiers=frozenset(),
            )

        window._scrollbar_mouse_handler(event(MouseEventType.MOUSE_DOWN, thumb_top))
        assert app._transcript_anchor is None
        window._scrollbar_mouse_handler(event(MouseEventType.MOUSE_MOVE, thumb_top - 1))
        assert app.transcript_top() < info.vertical_scroll
        moved = app.transcript_top()
        window._scrollbar_mouse_handler(event(MouseEventType.MOUSE_UP, thumb_top - 1))
        assert window._scrollbar_mouse_handler(event(MouseEventType.MOUSE_MOVE, thumb_top - 2)) is NotImplemented
        assert app.transcript_top() == moved

    _with_painted_app("thumb-drag", scenario)


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
        assert len(lines) == FOOTER_LINES - 1
        assert lines[1].rstrip().endswith("qwen3.5-plus")
        assert "default ·" not in lines[1]


def test_footer_reports_workspace_git_and_context_usage() -> None:
    settings = Settings.from_mapping({
        "llm": {
            "default": "metered",
            "models": {"metered": {"model": "qwen3.5-plus", "context_window": "1m"}},
        },
    })
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        thread_id="footer-context",
        settings=settings,
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app._git_summary = GitSummary(branch="main", changed=5, available=True)
        app.state.apply(RunEvent(
            type="usage",
            result={"input_tokens": 39_000, "output_tokens": 12, "total_tokens": 39_012},
        ))
        lines = "".join(fragment[1] for fragment in app._footer_text()).splitlines()
        assert "⎇ main · 5 changed" in lines[2]
        assert "1.0m Context · 3.9% used" in lines[2]


def test_footer_hides_context_meter_without_a_configured_window() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        thread_id="footer-unknown",
        settings=Settings(),
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.state.apply(RunEvent(type="usage", result={"total_tokens": 500}))
        lines = "".join(fragment[1] for fragment in app._footer_text()).splitlines()
        assert len(lines) == FOOTER_LINES - 1
        assert lines[1].rstrip().endswith("qwen3.5-plus")


def test_footer_places_context_beside_model_when_git_unavailable() -> None:
    settings = Settings.from_mapping({
        "llm": {
            "default": "metered",
            "models": {"metered": {"model": "auto", "source": "Token Plan", "context_window": "1m"}},
        },
    })
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        settings=settings,
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        lines = "".join(fragment[1] for fragment in app._footer_text()).splitlines()
        assert len(lines) == FOOTER_LINES - 1
        assert lines[1].rstrip().endswith("auto · Token Plan · 1.0m Context")


def test_context_usage_formatting() -> None:
    assert format_context_window(1_000_000) == "1.0m"
    assert format_context_window(200_000) == "200k"
    assert format_context_window(8_000) == "8k"
    assert format_context_usage({}, 0) == ""
    assert format_context_usage({}, 128_000) == "128k Context"
    assert format_context_usage({"total_tokens": 5_000}, 128_000) == "128k Context · 3.9% used"
    assert format_context_usage({"total_tokens": 9_000_000}, 1_000_000) == "1.0m Context · 100.0% used"


def test_compact_command_reports_threshold_and_current_usage() -> None:
    model = scripted_model([AIMessage(
        content="done",
        usage_metadata={"input_tokens": 1000, "output_tokens": 10, "total_tokens": 1010},
        response_metadata={"model_provider": "openai"},
    )])
    model.profile = {"max_input_tokens": 128_000}
    runner = AgentRunner(
        model=model, backend=StateBackend(),
        settings=Settings.from_mapping({"llm": {"context_window": "128k"}}),
    )
    assert runner.invoke("hello").status == "completed"

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await app._dispatch_command("/compact")
            notice = app.state.blocks[-1].content
            assert "42.5%" in notice
            assert "0.8%" in notice
            assert app.state.running is False

    asyncio.run(scenario())


def test_compact_command_does_not_invent_usage_without_model_report() -> None:
    model = scripted_model([AIMessage(content="unused")])
    model.profile = {"max_input_tokens": 128_000}
    runner = AgentRunner(
        model=model, backend=StateBackend(),
        settings=Settings.from_mapping({"llm": {"context_window": "128k"}}),
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await app._dispatch_command("/compact")
            notice = app.state.blocks[-1].content
            assert "42.5%" in notice
            assert "unknown" in notice
            assert "0.0%" not in notice

    asyncio.run(scenario())


def test_compact_command_runs_upstream_tool_and_reports_success() -> None:
    model = scripted_model([
        AIMessage(
            content="done",
            usage_metadata={"input_tokens": 60_000, "output_tokens": 10, "total_tokens": 60_010},
            response_metadata={"model_provider": "openai"},
        ),
        AIMessage(content="short summary"),
    ])
    model.profile = {"max_input_tokens": 128_000}
    runner = AgentRunner(
        model=model, backend=StateBackend(),
        settings=Settings.from_mapping({"llm": {"context_window": "128k"}}),
    )
    assert runner.invoke("long " * 15_000).status == "completed"

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app.state.usage = {"total_tokens": 60_010}
            task = asyncio.create_task(app._dispatch_command("/compact"))
            # A real Application has a refresh timer; keep the DummyOutput test
            # loop ticking while LangGraph finishes work in the IO executor.
            for _ in range(100):
                if task.done():
                    break
                await asyncio.sleep(0.05)
            assert task.done()
            await task
            assert "Conversation compacted." in app.state.blocks[-1].content
            assert app.state.usage == {}
            assert app.state.running is False

    asyncio.run(scenario())


def test_git_status_parsing() -> None:
    clean = parse_status("## main...origin/main\n")
    assert clean == GitSummary(branch="main", changed=0, available=True)
    assert clean.label() == "⎇ main · clean"

    dirty = parse_status("## feature/x...origin/feature/x [ahead 2]\n M a.py\n?? b.py\n")
    assert dirty.branch == "feature/x"
    assert dirty.changed == 2
    assert dirty.label() == "⎇ feature/x · 2 changed"

    assert parse_status("## No commits yet on main\n?? a.py\n").branch == "main"
    assert parse_status("## HEAD (no branch)\n").branch == "detached"
    assert GitSummary().label() == ""


def test_transcript_renderer_renders_each_unit_once(monkeypatch) -> None:
    """Frozen history must not go back through Rich on every frame."""
    import agent.cli.rendering as rendering

    original = rendering._render_unit
    rendered: list[int] = []

    def counting(renderable: object, width: int) -> object:
        rendered.append(width)
        return original(renderable, width)

    monkeypatch.setattr(rendering, "_render_unit", counting)

    state = CliState()
    state.add_system("first frozen block")
    state.add_system("second frozen block")
    renderer = TranscriptRenderer()

    first, lines = renderer.render(state, 80)
    assert lines > 3
    assert len(rendered) == 3  # header + two system blocks

    again, repeated = renderer.render(state, 80)
    assert repeated == lines
    assert len(rendered) == 3
    assert "".join(part[1] for part in again) == "".join(part[1] for part in first)

    state.apply(RunEvent(type="assistant_delta", content="streaming"))
    streaming, grown = renderer.render(state, 80)
    assert grown > lines
    assert len(rendered) == 4  # only the new active block
    assert "streaming" in "".join(part[1] for part in streaming)

    state.apply(RunEvent(type="assistant_delta", content="streaming further"))
    renderer.render(state, 80)
    assert len(rendered) == 5

    renderer.render(state, 40)
    assert len(rendered) == 5 + 4  # a width change rebuilds every unit


def test_transcript_renderer_caches_collapsed_explore_groups(monkeypatch) -> None:
    """A collapsed read/grep/glob group is one unit and must not re-render when frozen."""
    import agent.cli.rendering as rendering

    original = rendering._render_unit
    rendered: list[int] = []

    def counting(renderable: object, width: int) -> object:
        rendered.append(width)
        return original(renderable, width)

    monkeypatch.setattr(rendering, "_render_unit", counting)

    def explore(index: int, name: str) -> ToolBlock:
        return ToolBlock(
            tool_call_id=f"e{index}", name=name,
            arguments={"file_path": f"f{index}.py"}, output="x", status="completed",
        )

    state = CliState(blocks=[explore(0, "read_file"), explore(1, "grep"), explore(2, "glob")])
    renderer = TranscriptRenderer()

    fragments, _ = renderer.render(state, 80)
    assert len(rendered) == 2  # header + one collapsed group
    assert "Read, grepped, globbed" in _plain("".join(part[1] for part in fragments))

    for _ in range(3):
        renderer.render(state, 80)
    assert len(rendered) == 2

    state.blocks.append(explore(3, "read_file"))
    renderer.render(state, 80)
    assert len(rendered) == 3  # the group gained a member

    state.tools_expanded = True
    renderer.render(state, 80)
    assert len(rendered) == 7  # header replayed, four tool units built


def test_transcript_renderer_matches_uncached_render() -> None:
    state = CliState()
    state.add_user("hello there")
    state.todos = [{"content": "Inspect files", "status": "in_progress"}]
    state.blocks.append(ToolBlock(
        tool_call_id="t1", name="execute", arguments={"command": "pytest -q"},
        output="ok\nfailed", status="completed",
    ))
    state.blocks.append(ToolBlock(
        tool_call_id="t2", name="read_file", arguments={"file_path": "a.py"},
        output="print(1)", status="completed",
    ))
    renderer = TranscriptRenderer()
    fragments, lines = renderer.render(state, 90)
    expected = _plain(render_transcript(state, 90))
    assert "".join(part[1] for part in fragments) == expected
    assert lines == expected.count("\n") + 1


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
            await app.resume_session(original[:8])
            assert runner.thread_id == original
            assert "second draft" in app.buffer.text

            runner.follow_up("third draft")
            target = next(item.id for item in runner.list_sessions() if item.id != original)
            await app.resume_session(target[:8])
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
