"""Transcript, tool card, and diff rendering."""

from langchain_core.messages import AIMessage, ToolMessage
from deepagents.backends import StateBackend
from prompt_toolkit.buffer import CompletionState
from prompt_toolkit.completion import Completion
from prompt_toolkit.application.current import set_app
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.layout.containers import WindowAlign

import asyncio
import logging
import re
import pytest
from rich.cells import cell_len
from rich.text import Text
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from agent.cli.interactions import InteractionController
from agent.cli.app import (
    CliApplication,
)
from agent.cli.previews import (
    CommandExecutionPreview,
    FileMutationPreview,
    aggregate_file_mutations,
    build_diff_preview,
    build_tool_preview,
    file_tool_preview,
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
    render_transcript,
)
from agent.cli.state import CliState, MessageBlock, ToolBlock, TurnSummaryBlock
from agent.bootstrap.config import Settings
from agent.cli.display import UiDisplayLimits
from agent.runtime.runner import AgentRunner
from agent.runtime.contracts import RunEvent, RunResult, TurnTiming
from agent.sessions.store import SessionStore
from agent.sessions.transcript import messages_to_transcript
from agent.sessions.contracts import TranscriptBlock
from agent.tools.examples import build_example_tools
from agent.bootstrap.factory import DEFAULT_FS_TOOLS, create_agent
from tests.cli_support import (
    paint as _paint,
    plain as _plain,
    with_painted_app as _with_painted_app,
    without_diff_number as _without_diff_number,
)
from tests.conftest import scripted_model


def test_interaction_headings_and_navigation_are_concise() -> None:
    field = {"id": "effort", "label": "Reasoning effort", "type": "single_select",
             "options": [{"value": "default", "label": "default"}]}
    controller = InteractionController(kind="model_reasoning", title="Reasoning effort", question="", fields=[field])
    rendered = _plain(render_interaction(controller, 80))
    assert "1/1" not in rendered
    assert rendered.count("Reasoning effort") == 1
    assert "Esc back" in rendered
    controller.fields.append({"id": "other", "label": "Other", "type": "text"})
    assert "1/2  Reasoning effort" in _plain(render_interaction(controller, 80))


def test_interaction_keeps_user_context() -> None:
    controller = InteractionController.human({"title": "Input required", "question": "Which environment?",
                                              "reason": "Deployment requires a target"})
    rendered = _plain(render_interaction(controller, 80))
    assert "Which environment?" in rendered
    assert "Deployment requires a target" in rendered


def test_user_bubble_has_prefix_and_colored_blank_rows(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    rendered = _capture(_message(MessageBlock(kind="user", content="hello"), False)[0], 30)
    lines = rendered.splitlines()
    assert len(lines) == 5
    assert _plain(lines[0]).strip() == ""
    assert _plain(lines[-1]).strip() == ""
    assert "48;2;48;48;48" not in lines[0] + lines[-1]
    assert _plain(lines[1]).strip() == ""
    assert _plain(lines[2]).lstrip().startswith("› hello")
    assert _plain(lines[3]).strip() == ""
    assert all("48;2;48;48;48" in line for line in lines[1:-1])


def test_user_bubbles_have_one_external_blank_row_between_messages() -> None:
    state = CliState()
    state.blocks.append(MessageBlock(kind="assistant", content="before"))
    state.add_user("first")
    state.add_user("second")
    state.blocks.append(MessageBlock(kind="assistant", content="after"))
    lines = render_transcript(state, 40).splitlines()
    colored = [index for index, line in enumerate(lines) if "48;2;48;48;48" in line]
    before = next(index for index, line in enumerate(lines) if _plain(line).strip() == "before")
    after = next(index for index, line in enumerate(lines) if _plain(line).strip() == "after")
    assert len(colored) == 6
    assert colored[0] == before + 2
    assert colored[3] == colored[2] + 2
    assert after == colored[-1] + 2


@pytest.mark.parametrize("content", ["长消息" * 180, "```\n" + "\n".join(f"line {i}" for i in range(12)) + "\n```"], ids=["chinese", "code"])
@pytest.mark.parametrize("width", [20, 50])
def test_long_user_messages_collapse_after_wrapping_and_expand_without_losing_content(content, width) -> None:
    state = CliState()
    state.add_user(content)
    renderer = TranscriptRenderer()
    collapsed, _ = renderer.render(state, width)
    collapsed_text = "".join(part[1] for part in collapsed)
    assert "Ctrl+B" in collapsed_text
    if width == 50:
        assert "Ctrl+B to expand" in collapsed_text
        assert "more lines" in collapsed_text
    state.messages_expanded = True
    expanded, _ = renderer.render(state, width)
    expanded_text = "".join(part[1] for part in expanded)
    assert "Ctrl+B" in expanded_text
    assert expanded_text.count("\n") > collapsed_text.count("\n")
    assert state.blocks[0].content == content
    state.messages_expanded = False
    assert renderer.render(state, width)[0] == collapsed


def test_ctrl_b_expands_queue_below_status_and_scrolls_without_editing_input() -> None:
    def scenario(app: CliApplication) -> None:
        text = "\n".join(f"queue line {index:03d}" for index in range(80))
        app.runner.control.follow_up(text)
        app.state.add_user("sent content " * 80)
        app.buffer.text = "draft stays here"
        status = "".join(part[1] for part in app._status_text())
        assert "queue line" not in status
        assert "queue line 079" not in status
        binding = next(item for item in app.bindings.bindings if item.keys == ("c-b",))
        binding.handler(None)
        _paint(app)
        assert app._queue_expanded()
        assert app.buffer.text == "draft stays here"
        assert "queue line 079" in "".join(part[1] for part in app._queue_text())
        positions = app.application.renderer._last_screen.visible_windows_to_write_positions
        assert app.queue_window in positions
        assert positions[app.queue_window].height <= app.application.output.get_size().rows // 3
        app._scroll_queue(1000)
        _paint(app)
        assert app.queue_window.vertical_scroll > 0
        screen = app.application.renderer._last_screen
        assert any(
            "queue line 079" in "".join(screen.data_buffer[y][x].char for x in range(app._width()))
            for y in range(app.application.output.get_size().rows)
        )
        app._scroll_queue(-1000)
        _paint(app)
        assert app.queue_window.vertical_scroll == 0
        binding.handler(None)
        _paint(app)
        assert app.queue_window in app.application.renderer._last_screen.visible_windows_to_write_positions
        assert not app.state.messages_expanded
        assert app.runner.control.pending_message_texts() == [text]
        assert app.state.blocks[-1].content == "sent content " * 80

    _with_painted_app("queue-expand", scenario)


def test_editor_has_matching_blank_rows_and_prefix() -> None:
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        body = app.application.layout.container.children[-1].children[0].content
        assert not any(getattr(child, "style", None) == "class:editor-border" for child in body.children)
        editor = next(child.content for child in body.children if hasattr(getattr(child, "content", None), "children"))
        top, middle, bottom = editor.children
        prefix, input_window = middle.children
        assert top.height == bottom.height == 1
        assert top.style == bottom.style == prefix.style == input_window.style == "class:editor"
        assert prefix.content.text == [("", "› ")]
        assert input_window.content is app.editor_control


def test_back_to_bottom_hint_is_centered_above_editor() -> None:
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        body = app.application.layout.container.children[-1].children[0].content
        editor = next(child.content for child in body.children if hasattr(getattr(child, "content", None), "children"))
        hint = next(child.content for child in body.children if getattr(getattr(child, "content", None), "content", None) is app.back_to_bottom_control)
        divider = app.interaction_divider_window
        assert hint.content is app.back_to_bottom_control
        assert hint.align is WindowAlign.CENTER
        assert divider.char == "─"
        assert divider.style == "class:interaction-divider"
        assert editor.children[1].children[1].content is app.editor_control


def test_interaction_divider_only_appears_for_interactive_screens() -> None:
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        body = app.application.layout.container.children[-1].children[0].content
        divider = next(child for child in body.children if getattr(child, "content", None) is app.interaction_divider_window)
        with set_app(app.application):
            assert not divider.filter()
            app.buffer.text = "/"
            app.buffer.complete_state = CompletionState(app.buffer.document, [Completion("/help")])
            assert not divider.filter()
            app.dialogs.current = InteractionController(
                kind="model", title="Select model", question="Choose a model",
                fields=[{"id": "model", "type": "single_select", "label": "Model", "options": []}],
            )
            assert divider.filter()
            app.dialogs.current = InteractionController.approval([{"toolCallId": "x", "name": "execute", "args": {}}])
            assert divider.filter()
            app.dialogs.current = None
            app.buffer.reset()
            assert not divider.filter()


def test_filesystem_warning_appears_in_transcript_not_terminal(capsys) -> None:
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        logger = logging.getLogger("deepagents.backends.filesystem")
        original_handlers = logger.handlers[:]
        original_propagate = logger.propagate
        with app._capture_runtime_logs():
            logger.warning("Glob of '/' timed out after 5s with 0 match(es); returning partial results")
            assert "Glob of '/' timed out" in app.state.blocks[-1].content
            assert app.buffer.text == ""
        assert logger.handlers == original_handlers
        assert logger.propagate is original_propagate
        assert capsys.readouterr().err == ""


def test_running_tools_use_green_spinner_and_completed_tools_green_dot() -> None:
    running = ToolBlock("call", "execute", {"command": "pwd"}, status="running")
    title = _tool(running, False).renderable.renderables[0]
    assert title.plain.lstrip()[0] in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    assert title.spans[0].style == "green"

    for status in ("running", "completed"):
        explore = ToolBlock("list", "ls", {}, status=status)
        collapsed_title = _explore_group([explore]).renderables[1]
        expanded_title = _tool(explore, True).renderable.renderables[0]
        for item in (collapsed_title, expanded_title):
            assert item.plain.lstrip()[0] in ("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if status == "running" else "•")
            assert item.spans[0].style == "green"


def test_cli_rejects_keybindings_inside_workspace(tmp_path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    settings = Settings.from_mapping({"sandbox": {"workspace": str(workspace)}})
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend(), settings=settings)
    with pytest.raises(ValueError, match="keybindings directory must be outside workspace"):
        CliApplication(runner, config_dir=workspace, output=DummyOutput())


def test_command_panel_keeps_title_color_spacing_and_wrapped_body_indentation() -> None:
    state = CliState()
    body = "connected: " + "x" * 45 + "\n\nnext paragraph"
    state.add_command_panel("/mcp", body, styled_spans=((0, len("connected"), "green"),))

    for width in (24, 80):
        document = TranscriptRenderer().render_document(state, width)
        rows = [document.get_line(i) for i in range(document.line_count)]
        heading = next(i for i, row in enumerate(rows) if "".join(value for _, value in row).rstrip() == "/mcp")
        title = rows[heading]
        assert "".join(value for _, value in title).rstrip() == "/mcp"
        assert any("ansimagenta" in style and "/" in value for style, value in title)
        assert not "".join(value for _, value in rows[heading + 1]).strip()
        body_rows = rows[heading + 2:]
        assert all(text.startswith("  ") for row in body_rows if (text := "".join(
            value for _, value in row
        )).strip())
        assert "".join(value for row in body_rows for style, value in row if "ansigreen" in style) == "connected"


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
            assert app.dialogs.current is not None
            assert app.dialogs.current.kind == "resume"
            options = app.dialogs.current.fields[0]["options"]
            assert {option["value"] for option in options} == {item.id for item in sessions}
            for width in (50, 100):
                rendered = re.sub(r"\x1b\[[0-9;]*m", "", render_interaction(app.dialogs.current, width))
                positions = []
                for info in sessions:
                    option = next(option for option in options if option["value"] == info.id)
                    timestamp = info.updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                    assert option["right_label"] == timestamp
                    assert option["label"] == (info.summary or info.id)
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
            assert app.dialogs.current is None
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
    assert "Model: qwen3.5-plus · default" in message
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
    assert event_types[-2:] == ["run_completed", "turn_completed"]
    timing = events[-1].result
    assert isinstance(timing, TurnTiming)
    assert timing.elapsed_seconds >= 0
    assert timing.finished_at.tzinfo is timezone.utc
    assert "assistant_started" in event_types
    assert "assistant_completed" in event_types
    tool_start = next(event for event in events if event.type == "tool_started")
    assert tool_start.tool_call_id == "call-docs"


def test_turn_summary_is_after_answer_and_uses_configured_timezone() -> None:
    state = CliState()
    state.apply(RunEvent(type="assistant_delta", content="done"))
    state.apply(RunEvent(type="run_completed", content="done"))
    state.apply(RunEvent(
        type="turn_completed",
        content="done",
        result=TurnTiming(476, datetime(2026, 9, 28, 2, 24, tzinfo=timezone.utc)),
    ))
    assert isinstance(state.blocks[-1], TurnSummaryBlock)
    assert len([block for block in state.blocks if isinstance(block, MessageBlock) and block.kind == "assistant"]) == 1
    shanghai = _plain(render_transcript(state, 80, ZoneInfo("Asia/Shanghai")))
    tokyo = _plain(render_transcript(state, 80, ZoneInfo("Asia/Tokyo")))
    assert "done" in shanghai
    assert "Worked for 7m 56s · 10:24" in shanghai
    assert "Worked for 7m 56s · 11:24" in tokyo
    assert shanghai.index("done") < shanghai.index("Worked for")


def test_session_restores_without_plan_tool(tmp_path) -> None:
    store = SessionStore(tmp_path / "session.sqlite3")
    runner = AgentRunner(model=scripted_model([AIMessage(content="working")]),
                         backend=StateBackend(), session_store=store)
    events: list[RunEvent] = []
    assert runner.invoke("work", on_event=events.append).status == "completed"
    assert not [event for event in events if event.type == "todos_updated"]
    assert "Plan" not in _plain(render_transcript(CliState(), 80))
    thread = runner.thread_id
    runner.close()
    store.close()
    reopened = SessionStore(tmp_path / "session.sqlite3")
    resumed = AgentRunner(model=scripted_model([AIMessage(content="continued")]),
                          backend=StateBackend(), session_store=reopened)
    snapshot = resumed.switch_session(thread)
    assert any(block.content == "working" for block in snapshot.transcript)
    assert resumed.invoke("continue").output == "continued"
    resumed.close()
    reopened.close()


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


def test_thinking_collapses_to_latest_three_lines_by_default() -> None:
    lines = [f"think {index}" for index in range(8)]
    state = CliState(blocks=[MessageBlock(kind="assistant", thinking="\n".join(lines), content="done")])
    collapsed = _plain(render_transcript(state, 100))
    assert "… 5 earlier thinking lines hidden · Ctrl+T to expand" in collapsed
    assert "think 0" not in collapsed and "think 2" not in collapsed
    assert "think 4" not in collapsed
    assert all(f"think {index}" in collapsed for index in range(5, 8))

    state.thinking_collapsed = False
    expanded = _plain(render_transcript(state, 100))
    assert "earlier thinking lines hidden" not in expanded
    assert all(f"think {index}" in expanded for index in range(8))

    short = CliState(blocks=[MessageBlock(
        kind="assistant", thinking="\n".join(f"short {index}" for index in range(3)), content="",
    )])
    short_view = _plain(render_transcript(short, 100))
    assert "earlier thinking lines hidden" not in short_view
    assert all(f"short {index}" in short_view for index in range(3))


def test_explore_tools_collapse_into_summary() -> None:
    assert {"ls", "read"}.issubset(DEFAULT_FS_TOOLS)
    assert "grep" not in DEFAULT_FS_TOOLS
    assert "glob" not in DEFAULT_FS_TOOLS
    state = CliState(blocks=[
        ToolBlock(
            tool_call_id="r1", name="read",
            arguments={"file_path": "/workspace/a.py"},
            output="src", status="completed",
        ),
        ToolBlock(
            tool_call_id="ls1", name="ls",
            arguments={"path": "/workspace"},
            output="/workspace/a.py", status="completed",
        ),
        ToolBlock(
            tool_call_id="g1", name="grep",
            arguments={"pattern": "foo", "path": "/workspace"},
            output="/workspace/a.py", status="completed",
        ),
        ToolBlock(
            tool_call_id="gl1", name="glob",
            arguments={"pattern": "*.py", "path": "/workspace"},
            output="/workspace/a.py", status="completed",
        ),
        ToolBlock(
            tool_call_id="e1", name="execute",
            arguments={"command": "pytest"},
            output="ok", status="completed",
        ),
        ToolBlock(
            tool_call_id="w1", name="write",
            arguments={"file_path": "/workspace/out.txt", "content": "x"},
            output="Wrote /workspace/out.txt", status="completed",
        ),
    ])
    collapsed = _plain(render_transcript(state, 80))
    assert "Explored 2 items" in collapsed
    assert "Read /workspace/a.py" in collapsed
    assert "Listed /workspace" in collapsed
    assert "grep" in collapsed and "glob" in collapsed
    assert "Searched" not in collapsed and "Finding" not in collapsed
    assert "Explored 4 items" not in collapsed
    assert "Ctrl+O to expand" in collapsed
    assert "Ran pytest" in collapsed
    assert "write workspace/out.txt" in collapsed
    state.tools_expanded = True
    expanded = _plain(render_transcript(state, 120))
    assert "Read /workspace/a.py" in expanded
    assert "Listed /workspace" in expanded
    assert "items hidden" not in expanded


@pytest.mark.parametrize("count", [1, 5, 6, 74])
def test_explore_preview_shows_only_last_five_in_order(count: int) -> None:
    blocks = [
        ToolBlock(str(index), "read", {"file_path": f"file-{index}.py"}, status="completed")
        for index in range(count)
    ]
    group = _explore_group(blocks)
    rendered = _plain(_capture(group, 100))
    lines = rendered.splitlines()
    assert f"Explored {count} {'item' if count == 1 else 'items'}" in rendered
    shown = min(count, 5)
    assert [f"Read file-{index}.py" for index in range(count - shown, count)] == [
        line.strip().split(" ", 1)[1] for line in lines if "Read file-" in line
    ]
    assert rendered.count("Read file-") == shown
    if count > 5:
        assert f"├ … {count - 5} more" in rendered
    else:
        assert " more" not in rendered
    assert f"└ Read file-{count - 1}.py" in rendered
    assert lines[-1].strip() == "Ctrl+O to expand"
    assert group.renderables[-1].style == "dim"


def test_running_explore_group_does_not_fill_terminal_width() -> None:
    """Live summaries must leave room before the terminal's auto-wrap edge."""
    blocks = [
        ToolBlock(str(index), ("ls", "read")[index % 2], {}, status="running")
        for index in range(10)
    ]
    for count in (4, 6, 8, 10):
        rendered = _plain(_capture(_explore_group(blocks[:count], width=245), 245))
        assert f"Exploring {count} items" in rendered
        if count > 5:
            assert f"… {count - 5} more" in rendered
        assert all(len(line) < 245 for line in rendered.splitlines())


def test_explore_preview_truncates_long_paths_before_terminal_edge() -> None:
    block = ToolBlock(
        "long", "read",
        {"file_path": "some/really/long/path/to/a/file/that/exceeds/the/terminal/width.py"},
        status="running",
    )
    rendered = _plain(_capture(_explore_group([block], width=20), 20))
    assert all(len(line) < 20 for line in rendered.splitlines())
    assert "Ctrl+O to expand" in rendered


@pytest.mark.parametrize(("status", "is_error", "expected"), [
    ("running", False, "Exploring 1 item"),
    ("waiting", False, "Explore 1 item — waiting for input"),
    ("completed", False, "Explored 1 item"),
    ("interrupted", False, "Explore 1 item · 1 interrupted"),
    ("error", True, "Explore 1 item · 1 failed"),
])
def test_explore_title_reflects_execution_state(status, is_error, expected) -> None:
    block = ToolBlock("read", "read", {"file_path": "app.py"}, status=status, is_error=is_error)
    rendered = _plain(_capture(_explore_group([block]), 100))
    assert expected in rendered
    if status != "completed":
        assert "Explored" not in rendered


@pytest.mark.parametrize(("name", "arguments", "artifact", "running", "completed"), [
    ("edit", {"file_path": "notes", "old_string": "old", "new_string": "new"}, {}, "Editing", "Edited"),
    ("write", {"file_path": "notes", "content": "new"}, {"operation": "create"}, "Writing", "Created"),
    ("write", {"file_path": "notes", "content": "new"}, {"operation": "overwrite"}, "Writing", "Wrote"),
    ("delete", {"file_path": "notes"}, {}, "Deleting", "Deleted"),
])
def test_file_tool_title_changes_only_after_success(name, arguments, artifact, running, completed) -> None:
    state = CliState()
    state.apply(RunEvent(type="tool_started", tool_call_id="file", name=name, arguments=arguments))
    for expanded in (False, True):
        state.tools_expanded = expanded
        rendered = _plain(render_transcript(state, 100))
        assert f"{running} notes" in rendered
        assert f"{completed} notes" not in rendered
    state.apply(RunEvent(type="tool_completed", tool_call_id="file", name=name, artifact=artifact))
    for expanded in (False, True):
        state.tools_expanded = expanded
        rendered = _plain(render_transcript(state, 100))
        assert f"{completed} notes" in rendered
        assert f"{running} notes" not in rendered


def test_running_tool_card_does_not_fill_terminal_width() -> None:
    for name, arguments in (
        ("execute", {"command": "pwd"}),
        ("write", {"file_path": "note.txt", "content": "hello"}),
        ("request_human_input", {"question": "Continue?"}),
    ):
        block = ToolBlock("call", name, arguments, status="running")
        rendered = _plain(_capture(_tool(block, False), 120))
        assert all(len(line) < 120 for line in rendered.splitlines())


def test_failed_execute_shows_real_exit_code_command_and_compact_output() -> None:
    state = CliState()
    state.apply(RunEvent(type="tool_started", tool_call_id="push", name="execute", arguments={"command": "git push origin main"}))
    state.apply(RunEvent(
        type="tool_completed", tool_call_id="push", name="execute", is_error=True,
        content="\n".join(f"detail {index}" for index in range(10)) + "\nExit code: 7",
        artifact={"exit_code": 7},
    ))
    block = state.blocks[0]
    assert isinstance(block, ToolBlock) and block.exit_code == 7
    collapsed = _plain(render_transcript(state, 100))
    assert "• Failed (exit 7) git push origin main" in collapsed
    assert "└ output" in collapsed
    assert "detail 9" in collapsed
    assert "detail 0" not in collapsed
    assert "Ctrl+O to expand" in collapsed
    state.tools_expanded = True
    assert "detail 0" in _plain(render_transcript(state, 100))


def test_failed_human_input_summarizes_fields_without_schema_until_expanded() -> None:
    output = (
        "Argument validation failed. `request_human_input` did not run and produced no side effects.\n"
        "Correct the arguments below and call the tool again.\n\nErrors:\n"
        "- fields: received str -> must be a JSON array\n"
        "  received: [serialized fields]\n"
        "- recommendation: received str -> must be a JSON object\n"
        "  received: {serialized recommendation}\n\n"
        "`request_human_input` parameter schema:\n{large schema}"
    )
    state = CliState(blocks=[ToolBlock(
        "ask", "request_human_input", {"question": "下一步怎么处理？"},
        output=output, status="error", is_error=True,
    )])
    collapsed = _plain(render_transcript(state, 100))
    assert "Failed Asking 下一步怎么处理？" in collapsed
    assert "Failed Failed" not in collapsed
    assert "fields: received str" in collapsed
    assert "recommendation: received str" in collapsed
    assert "schema" not in collapsed
    assert "serialized" not in collapsed
    state.tools_expanded = True
    expanded = _plain(render_transcript(state, 100))
    assert "{large schema}" in expanded
    assert "serialized fields" in expanded


def test_failed_mutation_shows_error_instead_of_unapplied_diff() -> None:
    state = CliState(blocks=[ToolBlock(
        "write", "write", {"file_path": "/workspace/out.txt", "content": "hello"},
        output="Error: permission denied", status="error", is_error=True,
    )])
    for expanded in (False, True):
        state.tools_expanded = expanded
        rendered = _plain(render_transcript(state, 100))
        assert "Failed Writing workspace/out.txt" in rendered
        assert "Failed Failed" not in rendered
        assert "permission denied" in rendered
        assert "+hello" not in rendered


def test_failed_tool_wraps_long_command_and_output_before_terminal_edge() -> None:
    block = ToolBlock(
        "long", "execute", {"command": "git push " + "a" * 180},
        output="Error: " + "b" * 180, status="error", is_error=True,
    )
    for expanded in (False, True):
        rendered = _plain(_capture(_tool(block, expanded, 80), 80))
        assert all(len(line) < 80 for line in rendered.splitlines())
        assert rendered.count("a") >= 180
        assert rendered.count("b") >= 180


def _assert_card_indentation(rendered: str, width: int, output: str) -> None:
    lines = [line.rstrip() for line in _plain(rendered).splitlines() if line.strip()]
    divider = lines.index("  └ output")
    assert divider > 1
    assert all(line.startswith("  ") and not line.startswith("   ") for line in lines[1:divider])
    assert len(lines[divider + 1:]) > 1
    assert all(line.startswith("    ") for line in lines[divider + 1:])
    output_rows = lines[divider + 1:]
    joined = "".join(line[4:] for line in output_rows).replace(" ", "")
    if "earlieroutputlineshidden" in joined:
        notice, content = joined.split("Ctrl+Otoexpand", 1)
        assert notice.startswith("…")
        assert len(output_rows[-3:]) == 3
        assert content == "".join(line[4:] for line in output_rows[-3:]).replace(" ", "")
        assert output.replace(" ", "").endswith(content)
    else:
        assert joined == output.replace(" ", "")
    assert all(cell_len(line) < width for line in lines)


@pytest.mark.parametrize("width", [20, 40, 80])
@pytest.mark.parametrize("expanded", [False, True])
@pytest.mark.parametrize(("status", "reason", "exit_code"), [
    ("running", None, None),
    ("completed", None, 0),
    ("error", None, 3),
    ("error", "timeout", 1),
    ("error", "cancelled", 1),
    ("error", "spawn_error", 1),
])
def test_command_card_preserves_wrapped_indentation(width, expanded, status, reason, exit_code) -> None:
    command = "python3 -m pytest " + "a" * 180
    output = "测试输出" * 50
    block = ToolBlock(
        "wrap", "execute", {"command": command}, output=output,
        status=status, is_error=status == "error", exit_code=exit_code,
        artifact={"exit_code": exit_code, "termination_reason": reason},
    )
    rendered = _capture(_tool(block, expanded, width), width)
    _assert_card_indentation(rendered, width, output)
    title = _plain(rendered).split("  └ output")[0]
    if expanded or width == 80:
        assert "a" * 180 in "".join(line.strip() for line in title.splitlines())
    else:
        assert "more command lines hidden" in " ".join(line.strip() for line in title.splitlines())


@pytest.mark.parametrize("width", [20, 40, 80])
@pytest.mark.parametrize("expanded", [False, True])
def test_running_stream_preserves_wrapped_indentation(width, expanded) -> None:
    state = CliState()
    state.tools_expanded = expanded
    state.apply(RunEvent(
        type="tool_started", tool_call_id="wrap", name="execute",
        arguments={"command": "python3 -m pytest " + "a" * 180},
    ))
    output = ""
    for delta in ("b" * 90, "c" * 90):
        state.apply(RunEvent(type="tool_output_delta", tool_call_id="wrap", content=delta))
        output += delta
        lines = _plain(render_transcript(state, width)).splitlines()
        start = next(index for index, line in enumerate(lines) if "Running" in line)
        _assert_card_indentation("\n".join(lines[start:]), width, output)


@pytest.mark.parametrize("width", [20, 40, 80])
@pytest.mark.parametrize("expanded", [False, True])
def test_non_command_failure_preserves_wrapped_indentation(width, expanded) -> None:
    output = "Error: " + "b" * 180
    block = ToolBlock(
        "wrap", "read", {"file_path": "a" * 180},
        output=output, status="error", is_error=True,
    )
    _assert_card_indentation(_capture(_tool(block, expanded, width), width), width, output)


@pytest.mark.parametrize("width", [20, 40, 80])
@pytest.mark.parametrize("name", ["start_task", "web_search"])
def test_generic_tool_card_title_preserves_wrapped_indentation(width, name) -> None:
    """SubAgent and other generic cards share the same title wrap/indent path."""
    long = "x" * 180
    if name == "start_task":
        args = {"description": f"Do carefully: {long}"}
        marker = "SubAgent"
    else:
        args = {"query": long}
        marker = "Searching"
    block = ToolBlock("wrap", name, args, status="running")
    lines = [line.rstrip() for line in _plain(_capture(_tool(block, False, width), width)).splitlines() if line.strip()]
    assert any(marker in line for line in lines)
    wrapped = [line for line in lines[1:] if line.startswith("  ") and not line.startswith("   ")]
    assert wrapped, lines
    assert all(cell_len(line) < width for line in lines)
    assert "xxxx" in "".join(line.strip() for line in lines)


def test_explore_group_keeps_last_five_and_surfaces_older_failures() -> None:
    blocks = [ToolBlock(str(index), "read", {"file_path": f"file-{index}.py"}, status="completed") for index in range(8)]
    blocks[0].status = "error"
    blocks[0].is_error = True
    blocks[0].output = "Error: permission denied"
    group = _explore_group(blocks, 100)
    rendered = _plain(_capture(group, 100))
    assert "Explore 8 items · 1 failed" in rendered
    assert "… 3 more" in rendered
    assert "└ Read file-7.py" in rendered
    assert "Failed Reading file-0.py: Error: permission denied" in rendered
    assert "Failed Failed" not in rendered
    assert group.renderables[1].spans[0].style == "#888888"


def test_explore_group_caps_failure_preview_and_terminal_width() -> None:
    blocks = [ToolBlock(
        str(index), "read", {"file_path": f"file-{index}.py"},
        output="Error: search failed", status="error", is_error=True,
    ) for index in range(10)]
    rendered = _plain(_capture(_explore_group(blocks, 80), 80))
    assert "Explore 10 items · 10 failed" in rendered
    assert "… 7 more failed" in rendered
    assert rendered.count("Error: search failed") == 3
    assert "Failed Failed" not in rendered
    assert "Failed (exit" not in rendered
    assert all(len(line) < 80 for line in rendered.splitlines())


def test_approval_pane_shows_command_and_network() -> None:
    command = "python -m pytest tests/test_cli.py tests/test_runner.py -q --tb=short"
    controller = InteractionController.approval([{
        "toolCallId": "ex-1", "name": "execute",
        "args": {"command": command, "network": True},
    }])
    rendered = re.sub(r"\x1b\[[0-9;]*m", "", render_interaction(controller, 80))
    assert command in " ".join(rendered.split())
    assert "Network: ON" in rendered
    assert "Approve tool call?" in rendered
    state = CliState(blocks=[ToolBlock(
        tool_call_id="ex-1", name="execute",
        arguments={"command": command, "network": True},
        status="waiting",
    )])
    assert "Running python -m pytest tests/test_cli.py" in render_transcript(state, 120)


def test_tool_render_is_compact_then_expandable() -> None:
    state = CliState(blocks=[ToolBlock(
        tool_call_id="exec-1",
        name="execute",
        arguments={"command": "pytest tests/"},
        output="\n".join(f"line {index}" for index in range(30)),
        status="completed",
    )])
    collapsed = render_transcript(state, 80)
    assert "Ran pytest tests/" in collapsed
    assert "└ output" in collapsed
    assert "27 earlier output lines hidden" in collapsed
    assert "line 25" not in collapsed
    assert collapsed.index("27 earlier output lines hidden") < collapsed.index("line 27")
    assert "line 29" in collapsed
    assert "line 0" not in collapsed
    state.tools_expanded = True
    expanded = render_transcript(state, 80)
    assert "line 0" in expanded
    assert "output lines hidden" not in expanded


@pytest.mark.parametrize("term", ["dumb", "unknown", "xterm-256color"])
def test_capture_uses_requested_width_on_any_term(monkeypatch, term) -> None:
    monkeypatch.setenv("TERM", term)
    rendered = [line.rstrip() for line in _plain(_capture(Text("x" * 150), 120)).splitlines()]
    assert rendered == ["x" * 120, "x" * 30]


@pytest.mark.parametrize("width", [60, 100])
@pytest.mark.parametrize("status", ["running", "completed", "error"])
@pytest.mark.parametrize("payload", [".process-chevron{transition:transform .15s}" * 20, "中文输出" * 100], ids=["css", "cjk"])
def test_execute_output_tail_counts_wrapped_terminal_rows(width, status, payload) -> None:
    output = f"first output\n{payload}\nlast output"
    block = ToolBlock(
        "wrapped-output", "execute", {"command": "cat style.css"},
        output=output, status=status, is_error=status == "error",
        exit_code=1 if status == "error" else None,
    )
    collapsed = _plain(_capture(_tool(block, False, width), width))
    tail = [line.rstrip() for line in collapsed.splitlines()]
    tail = tail[tail.index("  └ output") + 1:]
    assert "earlier output lines hidden" in tail[0]
    assert len(tail[1:]) == 3
    assert tail[-1].strip() == "last output"
    assert all(cell_len(line) <= width - 2 for line in tail)
    expanded = _plain(_capture(_tool(block, True, width), width))
    assert "first output" in expanded
    assert "lines hidden" not in expanded
    assert payload.replace(" ", "") in "".join(line.strip() for line in expanded.splitlines()).replace(" ", "")
    assert block.output == output


def test_generic_tool_output_tail_counts_wrapped_terminal_rows() -> None:
    block = ToolBlock("custom", "custom_tool", {}, output="x" * 600, status="completed")
    collapsed = _plain(_capture(_tool(block, False, 80), 80))
    rows = [line.rstrip() for line in collapsed.splitlines()]
    hint_index = next(index for index, line in enumerate(rows) if "output lines hidden" in line)
    assert "…" in rows[hint_index]
    assert len(rows[hint_index + 1:]) == 3
    assert "lines hidden" not in _plain(_capture(_tool(block, True, 80), 80))


def test_execute_running_tail_and_expanded_output_update_with_stream() -> None:
    state = CliState()
    state.apply(RunEvent(
        type="tool_started", tool_call_id="stream", name="execute",
        arguments={"command": "pytest -q"},
    ))
    started = _plain(render_transcript(state, 100))
    assert "Running pytest -q" in started
    assert "Ran pytest -q" not in started
    for index in range(6):
        state.apply(RunEvent(type="tool_output_delta", tool_call_id="stream", content=f"line {index}\n"))
    collapsed = _plain(render_transcript(state, 100))
    assert "Running pytest -q" in collapsed
    assert "Ran pytest -q" not in collapsed
    assert "└ output" in collapsed
    assert "3 earlier output lines hidden · Ctrl+O to expand" in collapsed
    assert "line 3" in collapsed and "line 5" in collapsed
    assert "line 1" not in collapsed

    state.apply(RunEvent(type="tool_output_delta", tool_call_id="stream", content="line 6\n"))
    collapsed = _plain(render_transcript(state, 100))
    assert "4 earlier output lines hidden" in collapsed
    assert "line 2" not in collapsed and "line 6" in collapsed

    state.tools_expanded = True
    assert "line 0" in _plain(render_transcript(state, 100))
    state.apply(RunEvent(type="tool_output_delta", tool_call_id="stream", content="line 7\n"))
    expanded = _plain(render_transcript(state, 100))
    assert "Running pytest -q" in expanded
    assert "Ran pytest -q" not in expanded
    assert "line 0" in expanded and "line 7" in expanded
    assert "earlier output lines hidden" not in expanded

    state.tools_expanded = False
    collapsed = _plain(render_transcript(state, 100))
    assert "5 earlier output lines hidden" in collapsed
    assert "line 5" in collapsed and "line 7" in collapsed

    state.apply(RunEvent(
        type="tool_completed", tool_call_id="stream", name="execute",
        artifact={"exit_code": 0},
    ))
    for expanded in (False, True):
        state.tools_expanded = expanded
        completed = _plain(render_transcript(state, 100))
        assert "Ran pytest -q" in completed
        assert "Running pytest -q" not in completed


def test_execute_stream_invalidates_collapsed_and_expanded_document_cache() -> None:
    state = CliState()
    renderer = TranscriptRenderer()
    state.apply(RunEvent(
        type="tool_started", tool_call_id="cache", name="execute",
        arguments={"command": "watch"},
    ))

    def document_text() -> str:
        document = renderer.render_document(state, 100)
        return _plain("\n".join(
            "".join(fragment[1] for fragment in document.get_line(index))
            for index in range(document.line_count)
        ))

    for index in range(6):
        state.apply(RunEvent(type="tool_output_delta", tool_call_id="cache", content=f"line {index}\n"))
    assert "line 5" in document_text()
    state.apply(RunEvent(type="tool_output_delta", tool_call_id="cache", content="line 6\n"))
    compact = document_text()
    assert "line 6" in compact and "line 2" not in compact

    state.tools_expanded = True
    assert "line 0" in document_text()
    state.apply(RunEvent(type="tool_output_delta", tool_call_id="cache", content="line 7\n"))
    expanded = document_text()
    assert "line 0" in expanded and "line 7" in expanded


@pytest.mark.parametrize(("reason", "exit_code", "expected"), [
    (None, 0, "Ran"),
    (None, 3, "Failed (exit 3)"),
    ("timeout", 1, "Timed out"),
    ("cancelled", 1, "Cancelled"),
    ("spawn_error", 1, "Failed to start"),
])
def test_execute_status_semantics_precede_exit_code(reason, exit_code, expected) -> None:
    block = ToolBlock(
        "command", "execute", {"command": "pytest -q"},
        output="result", status="error" if exit_code else "completed", is_error=bool(exit_code),
        artifact={"exit_code": exit_code, "termination_reason": reason}, exit_code=exit_code,
    )
    preview = build_tool_preview(block)
    assert isinstance(preview.command_execution, CommandExecutionPreview)
    rendered = _plain(_capture(_tool(block, False), 100))
    assert f"• {expected} pytest -q" in rendered
    assert "└ output" in rendered and "result" in rendered


@pytest.mark.parametrize(("block_status", "expected"), [
    ("waiting", "waiting for input"),
    ("interrupted", "interrupted (completion unconfirmed)"),
])
def test_execute_unconfirmed_states_override_stale_exit_code(block_status, expected) -> None:
    block = ToolBlock(
        "command", "execute", {"command": "pytest -q"},
        status=block_status, exit_code=1,
        artifact={"exit_code": 1, "termination_reason": "cancelled"},
    )
    assert normalize_command_execution(block).status == block_status
    rendered = _plain(_capture(_tool(block, False), 100))
    assert f"Running pytest -q — {expected}" in rendered
    assert "Cancelled" not in rendered


def test_execute_legacy_metadata_falls_back_without_parsing_output() -> None:
    block = ToolBlock(
        "legacy", "execute", {"command": "pytest -q"},
        output="Exit code: 7\nCancelled by user.", status="completed",
    )
    assert normalize_command_execution(block).status == "succeeded"
    assert "Ran pytest -q" in _plain(_capture(_tool(block, False), 100))
    block.is_error = True
    assert normalize_command_execution(block).status == "failed"


def test_run_completed_cannot_confirm_unfinished_execute() -> None:
    state = CliState()
    state.apply(RunEvent(
        type="tool_started", tool_call_id="unfinished", name="execute",
        arguments={"command": "pytest -q"},
    ))
    state.apply(RunEvent(type="tool_output_delta", tool_call_id="unfinished", content="partial\n"))
    state.apply(RunEvent(type="run_completed"))
    block = state.blocks[0]
    assert isinstance(block, ToolBlock) and block.status == "interrupted"
    rendered = _plain(render_transcript(state, 100))
    assert "Running pytest -q — interrupted (completion unconfirmed)" in rendered
    assert "Ran pytest -q" not in rendered


@pytest.mark.parametrize(("status", "artifact", "output", "synthetic"), [
    ("failed", {"exit_code": 2}, "real output\n\nExit code: 2", "Exit code: 2"),
    ("timeout", {"exit_code": 124, "termination_reason": "timeout"},
     "real output\n\nError: Command timed out after 2 seconds.", "Command timed out"),
    ("cancelled", {"exit_code": 130, "termination_reason": "cancelled"},
     "real output\n\nCancelled by user.", "Cancelled by user."),
])
def test_execute_card_hides_known_completion_footer(status, artifact, output, synthetic) -> None:
    block = ToolBlock(
        "footer", "execute", {"command": "build"}, output=output,
        artifact=artifact, status="error", is_error=True,
    )
    rendered = _plain(_capture(_tool(block, False), 100))
    assert "real output" in rendered
    assert synthetic not in rendered
    assert status != "failed" or "Failed (exit 2)" in rendered


@pytest.mark.parametrize(("status", "reason", "color"), [
    ("completed", None, "green"),
    ("waiting", None, "yellow"),
    ("interrupted", None, "#888888"),
    ("error", None, "#888888"),
    ("error", "timeout", "#888888"),
    ("error", "cancelled", "#888888"),
    ("error", "spawn_error", "#888888"),
])
def test_execute_status_marker_colors(status, reason, color) -> None:
    block = ToolBlock(
        "marker", "execute", {"command": "build"}, status=status,
        is_error=status == "error",
        artifact={"termination_reason": reason, "exit_code": 0 if status == "completed" else 1},
    )
    title = _tool(block, False).renderable.renderables[0]
    assert title.spans[0].style == color
    assert title.plain.startswith("• ")


def test_edit_waiting_shows_short_diff_preview() -> None:
    old = "\n".join(f"keep-{index}" for index in range(20))
    new = "replaced"
    state = CliState(blocks=[ToolBlock(
        tool_call_id="edit-1",
        name="edit",
        arguments={"file_path": "/workspace/tests/test_cache.py", "old_string": old, "new_string": new},
        status="waiting",
    )])
    rendered = _plain(render_transcript(state, 80))
    assert "Editing /workspace/tests/test_cache.py" in rendered
    assert "waiting for input" in rendered
    assert "--- a/workspace/tests/test_cache.py" not in rendered
    assert "+++ b/workspace/tests/test_cache.py" not in rendered
    assert "@@" not in rendered
    assert "- keep-0" in rendered
    assert "keep-15" not in rendered
    assert "Ctrl+R to review" in rendered
    for flag in ("diffs_expanded", "tools_expanded"):
        expanded = _plain(render_transcript(CliState(blocks=state.blocks, **{flag: True}), 80))
        assert "DeepAgentCLI" in expanded
        assert "waiting for input" in expanded
        assert "keep-15" in expanded
        assert "hidden" not in expanded
        assert "--- a/workspace/tests/test_cache.py" not in expanded


def test_edit_preview_excludes_context_and_metadata_lines() -> None:
    state = CliState(blocks=[ToolBlock(
        tool_call_id="edit-ctx",
        name="edit",
        arguments={
            "file_path": "/workspace/notes.md",
            "old_string": "first\nsecond\nthird",
            "new_string": "first\nchanged\nthird",
        },
        status="completed",
    )])
    rendered = _plain(render_transcript(state, 100))
    assert "Edited /workspace/notes.md (+1 -1)" in rendered
    assert "- second" in rendered
    assert "+ changed" in rendered
    assert "first" not in rendered
    assert "third" not in rendered
    for header in ("--- a/", "+++ b/", "@@"):
        assert header not in rendered


def test_edit_counts_are_colored_and_changed_lines_are_numbered(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    block = ToolBlock(
        tool_call_id="edit-numbered", name="edit",
        arguments={
            "file_path": "/workspace/notes.md",
            "old_string": "first\nsecond\nthird",
            "new_string": "first\nchanged\nthird",
        },
        status="completed",
    )
    rendered = render_transcript(CliState(blocks=[block]), 100)
    plain = _plain(rendered)
    assert "2- second" in plain
    assert "2+ changed" in plain
    assert re.search(r"\x1b\[38;2;19;161;14m\+1\x1b\[0m", rendered)
    assert re.search(r"\x1b\[38;2;197;15;31m-1\x1b\[0m", rendered)
    expanded = _plain(render_transcript(CliState(blocks=[block], diffs_expanded=True), 100))
    assert "2- second" in expanded
    assert "2+ changed" in expanded


def test_consecutive_edits_to_same_file_share_one_card_and_review() -> None:
    path = "/workspace/report.md"
    blocks = [ToolBlock(
        tool_call_id=f"edit-{index}", name="edit",
        arguments={"file_path": path, "old_string": f"old {index}", "new_string": f"entry {index}"},
        status="completed",
    ) for index in range(4)]
    state = CliState(blocks=blocks)
    rendered = _plain(render_transcript(state, 100))
    assert rendered.count(f"Edited {path}") == 1
    assert f"Edited {path} (+4 -4)" in rendered
    for index in range(2):
        assert f"+ entry {index}" in rendered
    assert "+ entry 2" not in rendered
    assert "4 changed lines hidden" in rendered
    expanded = _plain(render_transcript(CliState(blocks=blocks, diffs_expanded=True), 100))
    assert "DeepAgentCLI" in expanded
    assert expanded.count(f"Edited {path}") == 1
    for index in range(4):
        assert f"+ entry {index}" in expanded
    assert "hidden" not in expanded


def test_grouped_edits_identify_independent_snippet_line_numbers() -> None:
    blocks = [ToolBlock(
        f"edit-{index}", "edit",
        {"file_path": "/workspace/App.vue", "old_string": old, "new_string": new},
        status="completed",
    ) for index, (old, new) in enumerate([
        ("context\nold first", "context\nnew first"),
        ("old second", "new second"),
    ])]
    rendered = _plain(render_transcript(CliState(blocks=blocks), 100))
    assert rendered.count("Edited /workspace/App.vue") == 1
    assert rendered.index("Edit 1 · snippet line numbers") < rendered.index("2+ new first")
    assert rendered.index("2+ new first") < rendered.index("2- old first")
    assert rendered.index("2- old first") < rendered.index("Edit 2 · snippet line numbers")
    assert rendered.index("Edit 2 · snippet line numbers") < rendered.index("1+ new second")
    assert rendered.index("1+ new second") < rendered.index("1- old second")
    assert "(+2 -2)" in rendered
    expanded = _plain(render_transcript(CliState(blocks=blocks, tools_expanded=True), 100))
    assert expanded.count("Edited /workspace/App.vue") == 1
    assert expanded.count("snippet line numbers") == 2


def test_edit_group_merges_across_files_until_message() -> None:
    def edit(identifier: str, path: str, new: str = "y") -> ToolBlock:
        return ToolBlock(
            tool_call_id=identifier, name="edit",
            arguments={"file_path": path, "old_string": "x", "new_string": new},
            status="completed",
        )

    path = "/workspace/report.md"
    state = CliState(blocks=[
        edit("one", path, "first"), edit("other", "/workspace/other.md"), edit("two", path, "second"),
        MessageBlock(kind="assistant", content="done"), edit("three", path),
    ])
    rendered = _plain(render_transcript(state, 100))
    assert "Edited 2 files" in rendered
    assert f"├ {path}" in rendered
    assert "└ /workspace/other.md" in rendered
    # Multi-file cards match create: latest edit per path, no Edit N headers.
    assert "Edit 1 · snippet line numbers" not in rendered
    assert "+ second" in rendered
    assert "+ first" not in rendered
    assert rendered.count(f"Edited {path}") == 1


def test_multi_file_edit_matches_create_latest_per_path() -> None:
    """Same path twice inside a multi-file group: show latest only, like Created N files."""
    edits = [
        ToolBlock("e1", "edit", {
            "file_path": "/workspace/a.md", "old_string": "x", "new_string": "first",
        }, status="completed"),
        ToolBlock("e2", "edit", {
            "file_path": "/workspace/b.md", "old_string": "x", "new_string": "b",
        }, status="completed"),
        ToolBlock("e3", "edit", {
            "file_path": "/workspace/a.md", "old_string": "y", "new_string": "second",
        }, status="completed"),
    ]
    rendered = _plain(render_transcript(CliState(blocks=edits), 100))
    assert "Edited 2 files (+2 -2)" in rendered
    assert "├ /workspace/a.md (+1 -1)" in rendered
    assert "└ /workspace/b.md (+1 -1)" in rendered
    assert "+ second" in rendered
    assert "+ first" not in rendered
    assert "Edit 1 · snippet line numbers" not in rendered
    assert "⋮" not in rendered


def test_multi_file_tree_matches_subagent_frame_and_marks_only_omitted_files() -> None:
    """├ / │ / └ stay gray; ⋮ only under a path that actually hid lines."""
    short = "only"
    long = "\n".join(f"line {index}" for index in range(6))
    blocks = [
        ToolBlock("c0", "write", {"file_path": "/workspace/short.md", "content": short},
                  artifact={"operation": "create"}, status="completed"),
        ToolBlock("c1", "write", {"file_path": "/workspace/long.md", "content": long},
                  artifact={"operation": "create"}, status="completed"),
        ToolBlock("c2", "write", {"file_path": "/workspace/last.md", "content": "tail"},
                  artifact={"operation": "create"}, status="completed"),
    ]
    rendered = _plain(render_transcript(CliState(blocks=blocks), 100))
    lines = [line.rstrip() for line in rendered.splitlines() if line.strip()]
    short_at = next(i for i, line in enumerate(lines) if "├ /workspace/short.md" in line)
    long_at = next(i for i, line in enumerate(lines) if "├ /workspace/long.md" in line)
    last_at = next(i for i, line in enumerate(lines) if "└ /workspace/last.md" in line)
    short_body = lines[short_at + 1:long_at]
    long_body = lines[long_at + 1:last_at]
    last_body = lines[last_at + 1:]
    assert all("│" in line for line in short_body)
    assert "⋮" not in "\n".join(short_body)
    assert any("│" in line and "⋮" in line for line in long_body)
    assert all("│" not in line for line in last_body)
    assert "⋮" not in "\n".join(last_body)
    assert "changed lines hidden" in rendered
    # Single-file create does not grow a tree or a ⋮.
    alone = _plain(render_transcript(CliState(blocks=[blocks[0]]), 100))
    assert "├ " not in alone and "└ " not in alone and "⋮" not in alone


def _diff_background_rows(rendered: str, color: str) -> list[str]:
    pattern = rf"\x1b\[[0-9;]*48;2;{color}m([^\x1b]*)\x1b\[0m"
    return ["".join(parts) for line in rendered.splitlines() if (parts := re.findall(pattern, line))]


def test_diff_background_covers_line_number_through_content_and_leaves_trail(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    block = ToolBlock(
        tool_call_id="edit-background", name="edit",
        arguments={
            "file_path": "/workspace/notes.md",
            "old_string": "old",
            "new_string": "x" * 80,
        },
        status="completed",
    )
    write = ToolBlock(
        tool_call_id="write-background", name="write",
        arguments={"file_path": "/workspace/new.md", "content": "new"}, status="completed",
    )
    for rendered, available in (
        (render_transcript(CliState(blocks=[block]), 40), 39),
        (render_transcript(CliState(blocks=[block], diffs_expanded=True), 40), 39),
        (render_transcript(CliState(blocks=[write]), 40), 39),
        (render_transcript(CliState(blocks=[write], tools_expanded=True), 40), 39),
    ):
        green = _diff_background_rows(rendered, "0;95;0")
        assert green
        # Line number shares the add/delete background; short rows stay under full width.
        assert any(row[:1].isdigit() for row in green)
        short = [row for row in green if len(row) < available]
        assert short
        trail = _diff_background_rows(rendered, "12;12;12")
        assert trail


def test_large_write_preview_has_numbered_content_width_green_rows(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    content = "\n".join(["#!/usr/bin/env node", "/**", *[f"line {index}" for index in range(116)]])
    block = ToolBlock(
        "script", "write",
        {"file_path": "workspace/login-coolcollege.mjs", "content": content},
        status="completed",
    )
    rendered = render_transcript(CliState(blocks=[block]), 60)
    plain = _plain(rendered)
    assert re.search(r"(?m)^\s+1\+ #!/usr/bin/env node", plain)
    assert re.search(r"(?m)^\s+2\+ /\*\*", plain)
    assert "… 114 lines hidden · Ctrl+R to review" in plain
    green_rows = _diff_background_rows(rendered, "0;95;0")
    assert green_rows and any(row.startswith("1+") for row in green_rows)
    assert all(len(row) < 59 for row in green_rows)
    assert _diff_background_rows(rendered, "12;12;12")

    expanded = _plain(render_transcript(CliState(blocks=[block], diffs_expanded=True), 60))
    assert "DeepAgentCLI" in expanded
    assert re.search(r"(?m)^\s+118\+ line 115", expanded)
    assert "hidden" not in expanded


def test_edit_changed_rows_use_content_width_without_context(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    args = {
        "file_path": "/workspace/example.py",
        "old_string": "keep\nold\nend",
        "new_string": "keep\nnew\nend",
    }
    block = ToolBlock("edit-width", "edit", args, status="completed")
    rendered = render_transcript(CliState(blocks=[block], diffs_expanded=True), 50)
    plain = _plain(rendered)
    assert re.search(r"(?m)^\s+2- old", plain)
    assert re.search(r"(?m)^\s+2\+ new", plain)
    assert "keep" not in plain
    assert "end" not in plain
    red = _diff_background_rows(rendered, "95;0;0")
    green = _diff_background_rows(rendered, "0;95;0")
    assert green == ["2+ new"]
    assert red == ["2- old"]
    assert plain.index("2+ new") < plain.index("2- old")
    assert _diff_background_rows(rendered, "12;12;12")


def test_python_diff_keeps_syntax_foreground_on_add_delete_background(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    args = {
        "file_path": "/workspace/demo.py",
        "old_string": "def foo():\n    return 1\n",
        "new_string": "def foo():\n    return 2\n",
    }
    block = ToolBlock("edit-syntax", "edit", args, status="completed")
    for rendered in (
        render_transcript(CliState(blocks=[block]), 80),
        render_transcript(CliState(blocks=[block], tools_expanded=True), 80),
    ):
        assert "48;2;95;0;0" in rendered
        assert "48;2;0;95;0" in rendered
        # Dark+ keyword #569CD6 (38;2;86;156;214) survives on the diff bg.
        assert re.search(
            r"\x1b\[[0-9;]*38;2;86;156;214[0-9;]*48;2;(?:95;0;0|0;95;0)m",
            rendered,
        )


@pytest.mark.parametrize("view", ["preview", "expanded"])
@pytest.mark.parametrize("depth,green,red", [
    ("DEPTH_4_BIT", "42", "41"),
    ("DEPTH_8_BIT", "48;5;22", "48;5;52"),
    ("DEPTH_24_BIT", "48;2;0;95;0", "48;2;95;0;0"),
])
def test_edit_highlights_keep_red_and_green_in_terminal_output(view, depth, green, red) -> None:
    from io import StringIO
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.output.color_depth import ColorDepth
    from prompt_toolkit.output.vt100 import Vt100_Output
    from prompt_toolkit.styles import Style

    block = ToolBlock("edit-colors", "edit", {
        "file_path": "/workspace/example.py", "old_string": "old", "new_string": "new",
    }, status="completed")
    state = CliState(blocks=[block], diffs_expanded=view == "expanded")
    fragments, _ = TranscriptRenderer().render(state, 60)
    for marker, color in (("+", "00af00"), ("-", "af0000")):
        marked = "".join(text for style, text in fragments if color in style)
        assert marked == marker
    stream = StringIO()
    terminal = Vt100_Output(stream, lambda: Size(rows=24, columns=60), term="xterm-256color")
    style = Style.from_dict({})
    for fragment_style, text in fragments:
        terminal.set_attributes(style.get_attrs_for_style_str(fragment_style), ColorDepth(depth))
        terminal.write(text)
    terminal.flush()
    sequences = re.findall(r"\x1b\[([0-9;]+)m", stream.getvalue())
    for expected in (green, red):
        assert any(f";{expected};" in f";{sequence};" for sequence in sequences)


def test_expanded_edit_keeps_changed_lines_on_the_original_card() -> None:
    args = {
        "file_path": "/workspace/notes.md",
        "old_string": "first\nsecond\nthird",
        "new_string": "first\nchanged\nthird",
    }
    state = CliState(blocks=[
        MessageBlock(kind="user", content="nearby"),
        ToolBlock("edit-expand", "edit", args, status="completed"),
    ], diffs_expanded=True)
    rendered = _plain(render_transcript(state, 100))
    assert "DeepAgentCLI" in rendered
    assert "nearby" in rendered
    assert "Edited /workspace/notes.md" in rendered
    assert "- second" in rendered
    assert "+ changed" in rendered
    assert "first" not in rendered
    assert "third" not in rendered
    for header in ("--- a/", "+++ b/", "@@"):
        assert header not in rendered


def test_edit_preview_pure_deletion_over_budget() -> None:
    old = "\n".join(f"line-{index}" for index in range(10))
    block = ToolBlock(
        tool_call_id="edit-del",
        name="edit",
        arguments={"file_path": "/workspace/f.md", "old_string": old, "new_string": ""},
        status="completed",
    )
    state = CliState(blocks=[block])
    rendered = _plain(render_transcript(state, 100))
    assert "Edited /workspace/f.md (+0 -10)" in rendered
    assert "- line-0" in rendered
    assert "- line-3" in rendered
    assert "- line-4" not in rendered
    assert "- line-9" not in rendered
    assert "… 6 changed lines hidden · Ctrl+R to review" in rendered
    changed = [line for line in build_tool_preview(block).lines if _without_diff_number(line.text).startswith(("-", "+"))]
    assert len(changed) == 4
    assert all(_without_diff_number(line.text).startswith("-") for line in changed)


def test_edit_with_empty_old_string_stays_edited_and_keeps_review_diff() -> None:
    new = "\n".join(f"line-{index}" for index in range(10))
    block = ToolBlock(
        tool_call_id="edit-add",
        name="edit",
        arguments={"file_path": "/workspace/f.md", "old_string": "", "new_string": new},
        status="completed",
    )
    rendered = _plain(render_transcript(CliState(blocks=[block]), 100))
    assert "Edited /workspace/f.md (+10 -0)" in rendered
    assert "+ line-0" in rendered
    expanded = _plain(render_transcript(CliState(blocks=[block], tools_expanded=True), 100))
    assert "Edited /workspace/f.md" in expanded
    assert "+ line-0" in expanded
    assert "+ line-9" in expanded
    assert "hidden" not in expanded


def test_consecutive_creates_list_each_file_with_counts_and_preview() -> None:
    blocks = [ToolBlock(
        tool_call_id=f"create-{index}", name="write",
        arguments={
            "file_path": f"/workspace/file-{index}.md",
            "content": f"content {index}",
        },
        artifact={"operation": "create"},
        status="completed",
    ) for index in range(4)]
    state = CliState(blocks=blocks)
    colored = render_transcript(state, 100)
    rendered = _plain(colored)
    assert rendered.count("Created 4 files") == 1
    assert "Created 4 files (+4 -0)" in rendered
    for index in range(3):
        assert f"├ /workspace/file-{index}.md (+1 -0)" in rendered
        assert f"+ content {index}" in rendered
    assert "└ /workspace/file-3.md (+1 -0)" in rendered
    assert "+ content 3" in rendered
    assert "⋮" not in rendered
    assert re.search(r"\x1b\[38;2;19;161;14m\+1\x1b\[0m", colored)
    assert re.search(r"\x1b\[38;2;197;15;31m-0\x1b\[0m", colored)
    expanded = _plain(render_transcript(CliState(blocks=blocks, diffs_expanded=True), 100))
    assert "DeepAgentCLI" in expanded
    assert "Created 4 files" in expanded
    assert "+ content 0" in expanded
    assert "+ content 3" in expanded


def test_file_expand_flags_share_in_place_full_body() -> None:
    content = "\n".join(f"line-{index}" for index in range(6))
    blocks = [
        MessageBlock(kind="user", content="keep this message"),
        *[ToolBlock(
            f"create-{index}", "write",
            {"file_path": f"/workspace/f{index}.py", "content": content},
            artifact={"operation": "create"}, status="completed",
        ) for index in range(2)],
    ]
    collapsed = _plain(render_transcript(CliState(blocks=blocks), 100))
    assert "keep this message" in collapsed
    assert "Created 2 files" in collapsed
    assert "⋮" in collapsed
    assert "line-5" not in collapsed
    for flag in ("diffs_expanded", "tools_expanded"):
        rendered = _plain(render_transcript(CliState(blocks=blocks, **{flag: True}), 100))
        assert "DeepAgentCLI" in rendered
        assert "keep this message" in rendered
        assert "Created 2 files" in rendered
        assert "├ /workspace/f0.py" in rendered
        assert "└ /workspace/f1.py" in rendered
        assert "line-5" in rendered
        assert "⋮" not in rendered
        assert "hidden" not in rendered
        assert "@@" not in rendered


def test_write_operation_artifact_controls_create_and_overwrite_titles() -> None:
    path = "/workspace/report.md"
    args = {"file_path": path, "content": "hello"}
    created = ToolBlock("write-new", "write", args, artifact={"operation": "create"}, status="completed")
    overwritten = ToolBlock("write-old", "write", args, artifact={"operation": "overwrite"}, status="completed")
    unknown = ToolBlock("write-legacy", "write", args, status="completed")
    edited = ToolBlock(
        "edit-old-empty", "edit",
        {"file_path": path, "old_string": "", "new_string": "hello"},
        artifact={"operation": "create"}, status="completed",
    )
    assert "• Created /workspace/report.md (+1 -0)" in _plain(render_transcript(CliState(blocks=[created]), 100))
    assert "• Wrote /workspace/report.md (+1 -0)" in _plain(render_transcript(CliState(blocks=[overwritten]), 100))
    assert "• write workspace/report.md (+1 -0)" in _plain(render_transcript(CliState(blocks=[unknown]), 100))
    assert "• Edited /workspace/report.md (+1 -0)" in _plain(render_transcript(CliState(blocks=[edited]), 100))


def test_file_mutation_normalizer_produces_semantics_and_compact_rows() -> None:
    path = "/workspace/report.md"
    created = ToolBlock("create", "write", {"file_path": path, "content": "x"},
                        artifact={"operation": "create"}, status="completed")
    overwritten = ToolBlock("overwrite", "write", {
        "file_path": path, "content": "\n".join(f"line-{index}" for index in range(10)),
    }, artifact={"operation": "overwrite"}, status="completed")
    edited = ToolBlock("modify", "edit", {
        "file_path": path, "old_string": "old", "new_string": "new",
    }, status="completed")
    deleted = ToolBlock("delete", "delete", {"file_path": path}, status="completed")
    assert normalize_file_mutation(created).operation == "create"
    assert normalize_file_mutation(edited).operation == "modify"
    assert normalize_file_mutation(overwritten).operation == "overwrite"
    assert normalize_file_mutation(deleted).operation == "delete"
    compact = normalize_file_mutation(overwritten).compact_lines
    assert len([line for line in compact if line.style == "add"]) == 4
    assert compact[-1].text == "… 6 lines hidden · Ctrl+R to review"
    assert normalize_file_mutation(ToolBlock("legacy", "write", overwritten.arguments,
                                            status="completed")) is None


def test_create_aggregation_consumes_semantics_without_tool_identity() -> None:
    mutations = [FileMutationPreview(f"/workspace/{index}.py", "create") for index in range(7)]
    grouped = aggregate_file_mutations(mutations)
    assert grouped.paths == tuple(mutation.path for mutation in mutations)
    assert grouped.sections[0].path == "/workspace/0.py"
    assert grouped.sections[-1].path == "/workspace/6.py"
    assert len(grouped.sections) == 7
    assert grouped.compact_lines == ()


def test_edit_preview_many_deletions_keep_one_addition() -> None:
    old = "\n".join(f"keep-{index}" for index in range(20))
    block = ToolBlock(
        tool_call_id="edit-floor-plus",
        name="edit",
        arguments={"file_path": "/workspace/file.md", "old_string": old, "new_string": "replaced"},
        status="completed",
    )
    state = CliState(blocks=[block])
    rendered = _plain(render_transcript(state, 100, limits=UiDisplayLimits(head_lines=8)))
    assert "Edited /workspace/file.md (+1 -20)" in rendered
    assert "+ replaced" in rendered
    assert "- keep-6" in rendered
    assert "- keep-7" not in rendered
    assert "… 13 changed lines hidden · Ctrl+R to review" in rendered
    texts = [_without_diff_number(line.text) for line in build_tool_preview(block, limits=UiDisplayLimits(head_lines=8)).lines]
    assert texts == [
        "+ replaced",
        *[f"- keep-{index}" for index in range(7)],
        "… 13 changed lines hidden · Ctrl+R to review",
    ]
    assert "  ⋮" not in texts
    expanded = _plain(render_transcript(CliState(blocks=[block], diffs_expanded=True), 100))
    assert "- keep-15" in expanded
    assert "+ replaced" in expanded
    assert "hidden" not in expanded
    assert "⋮" not in expanded


def test_edit_preview_many_additions_keep_one_deletion() -> None:
    new = "\n".join(f"add-{index}" for index in range(20))
    block = ToolBlock(
        tool_call_id="edit-floor-minus",
        name="edit",
        arguments={"file_path": "/workspace/file.md", "old_string": "keep", "new_string": new},
        status="completed",
    )
    rendered = _plain(render_transcript(CliState(blocks=[block]), 100, limits=UiDisplayLimits(head_lines=8)))
    assert "Edited /workspace/file.md (+20 -1)" in rendered
    assert "- keep" in rendered
    assert "+ add-6" in rendered
    assert "+ add-7" not in rendered
    assert "… 13 changed lines hidden · Ctrl+R to review" in rendered
    texts = [_without_diff_number(line.text) for line in build_tool_preview(block, limits=UiDisplayLimits(head_lines=8)).lines]
    assert texts == [
        "+ add-0",
        "- keep",
        *[f"+ add-{index}" for index in range(1, 7)],
        "… 13 changed lines hidden · Ctrl+R to review",
    ]
    assert "  ⋮" not in texts


def test_edit_preview_under_budget_shows_all_without_hidden_hint() -> None:
    block = ToolBlock(
        tool_call_id="edit-small",
        name="edit",
        arguments={"file_path": "/workspace/notes.md", "old_string": "l1\nl2\nl3", "new_string": "n1\nn2"},
        status="completed",
    )
    rendered = _plain(render_transcript(CliState(blocks=[block]), 100, limits=UiDisplayLimits(head_lines=8)))
    assert "Edited /workspace/notes.md (+2 -3)" in rendered
    texts = [_without_diff_number(line.text) for line in build_tool_preview(block, limits=UiDisplayLimits(head_lines=8)).lines]
    assert texts == ["+ n1", "- l1", "+ n2", "- l2", "- l3"]
    assert "changed lines hidden" not in rendered
    assert "⋮" not in rendered


def test_edit_preview_pairs_same_number_add_before_delete() -> None:
    block = ToolBlock(
        tool_call_id="edit-number-order",
        name="edit",
        arguments={
            "file_path": "/workspace/cam.ts",
            "old_string": (
                "keep\n"
                "{ frame: 0, cx: 960, cy: 300, zoom: 1.1 },\n"
                "{ frame: 75, cx: 960, cy: 760, zoom: 1.0 },\n"
                "after"
            ),
            "new_string": (
                "keep\n"
                "{ frame: 0, cx: 960, cy: 190, zoom: 1.15 },\n"
                "{ frame: 75, cx: 960, cy: 320, zoom: 1.0 },\n"
                "after"
            ),
        },
        status="completed",
    )
    lines = build_tool_preview(block).lines
    assert [line.text for line in lines] == [
        "   2+ { frame: 0, cx: 960, cy: 190, zoom: 1.15 },",
        "   2- { frame: 0, cx: 960, cy: 300, zoom: 1.1 },",
        "   3+ { frame: 75, cx: 960, cy: 320, zoom: 1.0 },",
        "   3- { frame: 75, cx: 960, cy: 760, zoom: 1.0 },",
    ]


def test_edit_preview_hunk_jumps_stay_unmarked() -> None:
    keep = "\n".join(f"M{index}" for index in range(7))
    block = ToolBlock(
        tool_call_id="edit-hunks",
        name="edit",
        arguments={
            "file_path": "/workspace/file.md",
            "old_string": f"old1\n{keep}\nold2",
            "new_string": f"new1\n{keep}\nnew2",
        },
        status="completed",
    )
    rendered = _plain(render_transcript(CliState(blocks=[block]), 100))
    assert "Edited /workspace/file.md (+2 -2)" in rendered
    assert "- old1" in rendered
    assert "+ new1" in rendered
    assert "- old2" in rendered
    assert "+ new2" in rendered
    assert "changed lines hidden" not in rendered
    texts = [_without_diff_number(line.text) for line in build_tool_preview(block).lines]
    assert texts == ["+ new1", "- old1", "+ new2", "- old2"]
    assert "⋮" not in rendered


def test_edit_preview_single_hunk_never_shows_separator() -> None:
    block = ToolBlock(
        tool_call_id="edit-single-hunk",
        name="edit",
        arguments={"file_path": "/workspace/notes.md", "old_string": "a\nb\nc", "new_string": "a\nb\nx"},
        status="completed",
    )
    texts = [_without_diff_number(line.text) for line in build_tool_preview(block).lines]
    assert texts == ["+ x", "- c"]


def test_edit_preview_hunk_jumps_do_not_consume_budget() -> None:
    keep_p = "\n".join(f"P{index}" for index in range(7))
    keep_q = "\n".join(f"Q{index}" for index in range(7))
    block = ToolBlock(
        tool_call_id="edit-three-hunks",
        name="edit",
        arguments={
            "file_path": "/workspace/file.md",
            "old_string": f"A\n{keep_p}\nB1\nB2\n{keep_q}\nD",
            "new_string": f"A2\n{keep_p}\nB\n{keep_q}\nD1\nD2",
        },
        status="completed",
    )
    texts = [_without_diff_number(line.text) for line in build_tool_preview(block, limits=UiDisplayLimits(head_lines=8)).lines]
    changed = [text for text in texts if text.startswith(("-", "+"))]
    assert len(changed) == 8
    assert "⋮" not in "".join(texts)
    assert texts == [
        "+ A2", "- A", "+ B", "- B1", "- B2", "+ D1", "+ D2", "- D",
    ]
    assert "changed lines hidden" not in _plain(render_transcript(CliState(blocks=[block]), 100, limits=UiDisplayLimits(head_lines=8)))


def test_edit_preview_hidden_count_counts_only_changed_lines() -> None:
    keep = "\n".join(f"N{index}" for index in range(7))
    old = "\n".join(f"A{index}" for index in range(7)) + f"\n{keep}\n" + "\n".join(f"B{index}" for index in range(7))
    block = ToolBlock(
        tool_call_id="edit-hidden-count",
        name="edit",
        arguments={"file_path": "/workspace/file.md", "old_string": old, "new_string": keep},
        status="completed",
    )
    texts = [_without_diff_number(line.text) for line in build_tool_preview(block, limits=UiDisplayLimits(head_lines=8)).lines]
    changed = [text for text in texts if text.startswith(("-", "+"))]
    assert len(changed) == 8
    assert "⋮" not in "".join(texts)
    assert texts[-1] == "… 6 changed lines hidden · Ctrl+R to review"


def test_edit_preview_wraps_long_lines_before_terminal_edge() -> None:
    block = ToolBlock(
        tool_call_id="edit-wide",
        name="edit",
        arguments={"file_path": "/workspace/f.md", "old_string": "short", "new_string": "x" * 200},
        status="completed",
    )
    rendered = _plain(_capture(_tool(block, False), 80))
    assert all(len(line) < 80 for line in rendered.splitlines())
    assert rendered.count("x") >= 200


def test_edit_preview_empty_new_string_still_shows_deletion() -> None:
    block = ToolBlock(
        tool_call_id="edit-empty-new",
        name="edit",
        arguments={"file_path": "/workspace/f.md", "old_string": "foo", "new_string": ""},
        status="completed",
    )
    rendered = _plain(render_transcript(CliState(blocks=[block]), 100))
    assert "- foo" in rendered
    assert "changed lines hidden" not in rendered


def test_edit_preview_empty_old_string_shows_addition() -> None:
    block = ToolBlock(
        tool_call_id="edit-empty-old",
        name="edit",
        arguments={"file_path": "/workspace/f.md", "old_string": "", "new_string": "foo"},
        status="completed",
    )
    rendered = _plain(render_transcript(CliState(blocks=[block]), 100))
    assert "Edited /workspace/f.md (+1 -0)" in rendered
    assert "+ foo" in rendered


def test_edit_preview_both_empty_strings_has_no_change() -> None:
    args = {"file_path": "/workspace/f.md", "old_string": "", "new_string": ""}
    block = ToolBlock(
        tool_call_id="edit-both-empty", name="edit", arguments=args,
        status="completed",
    )
    assert build_diff_preview("edit", args) is None
    rendered = _plain(render_transcript(CliState(blocks=[block]), 100))
    assert "Edited /workspace/f.md" in rendered
    assert "(+" not in rendered
    assert " - " not in rendered
    assert " + " not in rendered


def test_write_empty_file_has_no_fabricated_diff() -> None:
    args = {"file_path": "/workspace/empty.md", "content": ""}
    block = ToolBlock(
        tool_call_id="write-empty", name="write", arguments=args,
        status="completed",
    )
    assert build_diff_preview("write", args) is None
    rendered = _plain(render_transcript(CliState(blocks=[block]), 100))
    assert "write /workspace/empty.md" in rendered
    assert "+0 lines" not in rendered

    created = ToolBlock(
        tool_call_id="write-empty-created", name="write", arguments=args,
        artifact={"operation": "create"}, status="completed",
    )
    assert "• Created /workspace/empty.md" in _plain(render_transcript(CliState(blocks=[created]), 100))


def test_edit_preview_counts_added_deleted() -> None:
    state = CliState(blocks=[ToolBlock(
        tool_call_id="edit-3",
        name="edit",
        arguments={
            "file_path": "/workspace/notes.md",
            "old_string": "a\nb\nc",
            "new_string": "a\nb\nc\nd\ne",
        },
        status="completed",
    )])
    rendered = _plain(render_transcript(state, 100))
    assert "Edited /workspace/notes.md (+2 -0)" in rendered


def test_edit_preview_without_reliable_diff_omits_counts() -> None:
    state = CliState(blocks=[ToolBlock(
        tool_call_id="edit-4",
        name="edit",
        arguments={"file_path": "/workspace/notes.md"},
        status="completed",
    )])
    rendered = _plain(render_transcript(state, 100))
    assert "Edited /workspace/notes.md" in rendered
    assert "(+" not in rendered


def test_unknown_tool_start_creates_block_with_identity_preserved() -> None:
    state = CliState()
    state.apply(RunEvent(
        type="tool_started", tool_call_id="u-1", name="custom_unknown_tool",
        arguments={"query": "middleware", "to": "agent/"},
    ))
    block = state.blocks[0]
    assert isinstance(block, ToolBlock)
    assert block.name == "custom_unknown_tool"
    assert block.arguments == {"query": "middleware", "to": "agent/"}
    assert block.status == "running"


def test_unknown_tool_output_and_completion_render_without_builder() -> None:
    state = CliState()
    state.apply(RunEvent(
        type="tool_started", tool_call_id="u-2", name="custom_unknown_tool",
        arguments={"query": "middleware"},
    ))
    state.apply(RunEvent(type="tool_output_delta", tool_call_id="u-2", content="entry 0\n"))
    state.apply(RunEvent(
        type="tool_completed", tool_call_id="u-2", name="custom_unknown_tool",
        content="entry 0\nentry 1",
    ))
    block = state.blocks[0]
    assert isinstance(block, ToolBlock)
    assert block.status == "completed"
    assert block.output == "entry 0\nentry 1"
    rendered = _plain(render_transcript(state, 100))
    assert "custom_unknown_tool middleware" in rendered
    assert "entry 1" in rendered


def test_unknown_tool_output_folds_like_generic_output() -> None:
    output = "\n".join(f"row {index}" for index in range(12))
    block = ToolBlock(
        "u-3", "custom_unknown_tool", {"query": "q"},
        output=output, status="completed",
    )
    collapsed = _plain(render_transcript(CliState(blocks=[block]), 100))
    assert "row 11" in collapsed
    assert "row 0" not in collapsed
    assert "… 9 output lines hidden · Ctrl+O to expand" in collapsed


def test_unknown_tool_completion_freezes_status_transitions() -> None:
    state = CliState()
    state.apply(RunEvent(type="tool_started", tool_call_id="u-4", name="custom_unknown_tool", arguments={}))
    state.apply(RunEvent(type="tool_completed", tool_call_id="u-4", name="custom_unknown_tool", content="ok"))
    ok = state.blocks[0]
    assert isinstance(ok, ToolBlock)
    assert ok.status == "completed"
    assert not ok.is_error

    state.apply(RunEvent(type="tool_started", tool_call_id="u-5", name="custom_unknown_tool", arguments={}))
    state.apply(RunEvent(
        type="tool_completed", tool_call_id="u-5", name="custom_unknown_tool",
        content="Error: boom", is_error=True,
    ))
    failed = state.blocks[1]
    assert isinstance(failed, ToolBlock)
    assert failed.status == "error"
    assert failed.is_error
    rendered = _plain(render_transcript(state, 100))
    assert "Failed custom_unknown_tool" in rendered
    assert "boom" in rendered


def test_mcp_card_collapses_arguments_and_folds_output() -> None:
    output = "\n".join(f"row {index}" for index in range(12))
    block = ToolBlock(
        "mcp-fold", "mcp__local__echo", {"q": "python"},
        output=output, status="completed",
        artifact={"structured_content": {"ok": True}, "mcp_server_name": "Local Server", "mcp_tool_name": "echo"},
    )
    collapsed = _plain(_capture(_tool(block, False), 100))
    assert block.name == "mcp__local__echo"
    assert "MCP · Local Server · echo" in collapsed
    assert "mcp__local__echo" not in collapsed
    assert "q: python" in collapsed
    assert "Arguments:" not in collapsed
    assert "Execution: Host" not in collapsed
    assert "Host (outside sandbox)" not in collapsed
    assert "row 11" in collapsed
    assert "row 0" not in collapsed
    assert "output lines hidden" in collapsed

    parallel = ToolBlock(
        "mcp-parallel", "mcp__parallel__search", {"q": "x"},
        output="ok", status="completed",
        artifact={"mcp_server_name": "Parallel", "mcp_tool_name": "search"},
    )
    rendered = _plain(_capture(_tool(parallel, False), 80))
    assert "MCP · Parallel · search" in rendered
    assert "mcp__parallel__search" not in rendered


def test_mcp_card_expands_location_arguments_and_output() -> None:
    output = "\n".join(f"row {index}" for index in range(12))
    block = ToolBlock(
        "mcp-open", "mcp__local__echo", {"q": "python", "count": 2},
        output=output, status="completed",
        artifact={"mcp_server_name": "Local Server", "mcp_tool_name": "echo"},
    )
    expanded = _plain(_capture(_tool(block, True), 100))
    assert "MCP · Local Server · echo" in expanded
    assert "Host (outside sandbox)" in expanded
    assert "q: python" in expanded
    assert "count: 2" in expanded
    assert "Arguments:" not in expanded
    assert "row 0" in expanded and "row 11" in expanded
    assert "output lines hidden" not in expanded


def test_mcp_failure_reuses_the_failure_card() -> None:
    block = ToolBlock(
        "mcp-fail", "mcp__local__echo", {"q": "python"},
        output="Error: boom", status="error", is_error=True,
        artifact={"structured_content": {"ok": False}, "mcp_server_name": "Local Server", "mcp_tool_name": "echo"},
    )
    collapsed = _plain(_capture(_tool(block, False), 100))
    expanded = _plain(_capture(_tool(block, True), 100))
    for rendered in (collapsed, expanded):
        assert "Failed MCP · Local Server · echo" in rendered
        assert "Failed Failed" not in rendered
        assert "boom" in rendered
        assert "Arguments:" not in rendered
        assert "Execution: Host" not in rendered
        assert "Host (outside sandbox)" not in rendered
    title = next(item for item in _tool(block, False).renderables if getattr(item, "plain", "").strip())
    assert title.plain.lstrip()[0] == "•"
    assert title.spans[0].style == "#888888"


def test_mcp_running_card_uses_the_green_spinner() -> None:
    running = ToolBlock("mcp-run", "mcp__local__echo", {"q": "hi"}, status="running")
    title = _tool(running, False).renderable.renderables[0]
    assert title.plain.lstrip()[0] in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    assert title.spans[0].style == "green"
    assert "mcp__local__echo" in title.plain
    assert "MCP ·" not in title.plain
    assert "— running" not in title.plain


def test_mcp_long_arguments_stay_bounded() -> None:
    args = {"payload": "A" * 100_000, "tail": "AFTER_PAYLOAD"}
    block = ToolBlock("mcp-long", "mcp__local__echo", args, output="done", status="completed")
    collapsed = _plain(_capture(_tool(block, False), 80))
    expanded = _plain(_capture(_tool(block, True), 100))
    assert "AAA" in collapsed
    assert "A" * 200 not in collapsed
    assert "AFTER_PAYLOAD" not in collapsed
    assert "Arguments:" not in collapsed
    assert "Host (outside sandbox)" not in collapsed
    assert "omitted" in expanded
    assert "AFTER_PAYLOAD" in expanded
    assert "Host (outside sandbox)" in expanded
    assert len(args["payload"]) == 100_000


def test_mcp_collapsed_summary_is_one_bounded_line() -> None:
    args = {f"k{index}": str(index) for index in range(6)}
    preview = build_tool_preview(ToolBlock(
        "mcp-sum", "mcp__local__echo", args, status="completed",
    ))
    assert len(preview.collapsed_lines) == 1
    text = preview.collapsed_lines[0].text
    assert "\n" not in text
    assert len(text) <= 120
    assert "k0:" in text and "k3:" in text
    assert "k4:" not in text and "k5:" not in text
    assert text.endswith("…")


def test_mcp_waiting_and_interrupted_cards_keep_status_marks() -> None:
    waiting = ToolBlock("mcp-wait", "mcp__local__echo", {"q": "hi"}, status="waiting")
    title = _tool(waiting, False).renderable.renderables[0]
    assert title.plain.lstrip()[0] == "•"
    assert title.spans[0].style == "yellow"
    assert "mcp__local__echo" in title.plain
    assert " — waiting for input" in title.plain
    assert "MCP ·" not in title.plain

    interrupted = ToolBlock(
        "mcp-stop", "mcp__local__echo", {"q": "hi"}, output="partial", status="interrupted",
    )
    title = _tool(interrupted, False).renderable.renderables[0]
    assert title.spans[0].style == "#888888"
    assert "mcp__local__echo" in title.plain
    assert " — interrupted (completion unconfirmed)" in title.plain
    rendered = _plain(_capture(_tool(interrupted, False), 100))
    assert "partial" in rendered


def test_mcp_card_restores_from_saved_transcript() -> None:
    from agent.sessions.transcript import settle_restored_tools

    saved = messages_to_transcript([
        AIMessage(content="", tool_calls=[{
            "id": "mcp-saved", "name": "mcp__local__echo", "args": {"q": "python"},
        }]),
        ToolMessage(
            content="saved body", tool_call_id="mcp-saved", name="mcp__local__echo",
            artifact={"structured_content": {"ok": True}, "mcp_server_name": "Local Server", "mcp_tool_name": "echo"},
        ),
    ])
    state = CliState()
    state.load_transcript(saved)
    block = state.blocks[0]
    assert isinstance(block, ToolBlock)
    assert block.name == "mcp__local__echo"
    assert block.arguments == {"q": "python"}
    assert block.output == "saved body"
    assert block.status == "completed"
    restored = _plain(render_transcript(state, 100))
    assert block.artifact["structured_content"] == {"ok": True}
    assert "MCP · Local Server · echo" in restored
    assert "q: python" in restored
    assert "saved body" in restored

    pending = messages_to_transcript([
        AIMessage(content="", tool_calls=[
            {"id": "mcp-ask", "name": "mcp__local__echo", "args": {"q": "wait"}},
            {"id": "mcp-old", "name": "mcp__docs__search", "args": {"q": "old"}},
        ]),
    ])
    settle_restored_tools(pending, waiting_ids={"mcp-ask"})
    assert pending[0].status == "waiting"
    assert pending[1].status == "interrupted"
    state.load_transcript(pending)
    rendered = _plain(render_transcript(state, 100))
    assert "mcp__local__echo" in rendered
    assert " — waiting for input" in rendered
    assert "mcp__docs__search" in rendered
    assert " — interrupted (completion unconfirmed)" in rendered
    assert state.blocks[0].name == "mcp__local__echo"
    assert state.blocks[1].name == "mcp__docs__search"


def test_mcp_ambiguous_server_name_uses_identity_or_registered_name() -> None:
    from agent.mcp.names import external_tool_name

    registered = external_tool_name("my__srv", "search")
    assert registered == "mcp__my__srv__search"
    block = ToolBlock("mcp-us", registered, {"q": "x"}, output="ok", status="completed")
    fallback = _plain(_capture(_tool(block, False), 120))
    assert registered in fallback
    assert "MCP ·" not in fallback
    assert "MCP my · srv__search" not in fallback
    assert "MCP my__srv · search" not in fallback

    remembered = ToolBlock(
        "mcp-us", registered, {"q": "x"}, output="ok", status="completed",
        artifact={"mcp_server_name": "my__srv", "mcp_tool_name": "search", "structured_content": {"q": "x"}},
    )
    titled = _plain(_capture(_tool(remembered, False), 120))
    assert "MCP · my__srv · search" in titled
    assert remembered.artifact["structured_content"] == {"q": "x"}
    assert registered not in titled


def test_mcp_sanitized_empty_output_does_not_fall_back_to_raw_text() -> None:
    from agent.cli.previews import build_failure_preview, build_mcp_preview

    raw = "\x1b[31m\x1b[0m\x00\u202e"
    block = ToolBlock("mcp-empty", "mcp__local__echo", {"q": "x"}, output=raw, status="completed")
    preview = build_mcp_preview(block)
    assert preview.display_output == ""
    assert preview.display_output is not None
    rendered = _capture(_tool(block, True), 80)
    assert raw not in rendered
    assert "\x00" not in rendered and "\u202e" not in rendered
    assert "\x1b" not in _plain(rendered)

    failed = ToolBlock(
        "mcp-empty-fail", "mcp__local__echo", {"q": "x"},
        output=raw, status="error", is_error=True,
    )
    failure = build_failure_preview(failed)
    base = build_mcp_preview(failed)
    assert failure.display_output == ""
    assert failure.collapsed_lines == base.collapsed_lines
    assert failure.expanded_lines == base.expanded_lines
    assert failure.detail == ""
    assert failure.lines == ()
    failed_render = _capture(_tool(failed, True), 80)
    assert raw not in failed_render
    assert "\x00" not in failed_render and "\u202e" not in failed_render
    assert "\x1b" not in _plain(failed_render)


def test_mcp_failure_preview_keeps_sanitized_output_fields() -> None:
    from agent.cli.previews import build_failure_preview, build_mcp_preview

    raw = "Error: boom\x1b[31m\nsecond line"
    block = ToolBlock(
        "mcp-fail-fields", "mcp__local__echo", {"q": "python"},
        output=raw, status="error", is_error=True,
    )
    failure = build_failure_preview(block)
    base = build_mcp_preview(block)
    assert failure.display_output == base.display_output
    assert failure.display_output is not None
    assert "\x1b" not in failure.display_output
    assert failure.collapsed_lines == base.collapsed_lines
    assert failure.expanded_lines == base.expanded_lines
    assert failure.detail == "Error: boom"
    assert any("boom" in line.text for line in failure.lines)
    expanded = _plain(_capture(_tool(block, True), 100))
    collapsed = _plain(_capture(_tool(block, False), 100))
    assert "boom" in expanded and "second line" in expanded
    assert "boom" in collapsed


def test_mcp_card_strips_terminal_controls_from_arguments_and_output() -> None:
    args = {"q": "\x1b[31mvisible\x1b[0m\x1b]52;c;clipboard\x07\x00\u202e"}
    output = "\x1b[32mbody\x1b[0m\x00ok\u202e"
    block = ToolBlock("mcp-ctrl", "mcp__local__echo", args, output=output, status="completed")
    preview = build_tool_preview(block)
    visible = "\n".join(line.text for line in (*preview.collapsed_lines, *preview.expanded_lines))
    assert preview.display_output is not None
    for text in (preview.display_output, visible):
        assert "\x1b" not in text and "\x00" not in text and "\u202e" not in text
        assert "clipboard" not in text
    for expanded in (False, True):
        rendered = _capture(_tool(block, expanded), 100)
        assert "\x00" not in rendered and "\u202e" not in rendered
        plain = _plain(rendered)
        assert "clipboard" not in plain
        assert "visible" in plain
        assert "body" in plain and "ok" in plain


def test_unknown_tool_preview_falls_back_to_generic_builder() -> None:
    block = ToolBlock(
        "u-6", "custom_unknown_tool", {"query": "middleware"},
        output="found", status="completed",
    )
    preview = build_tool_preview(block)
    assert preview.kind == "custom_unknown_tool"
    assert preview.verb == "custom_unknown_tool"
    assert preview.target == "middleware"
    assert preview.summary == "custom_unknown_tool middleware"
    assert preview.group == "other"
    assert preview.action == "expand"
    rendered = _plain(render_transcript(CliState(blocks=[block]), 100))
    assert "custom_unknown_tool middleware" in rendered
    assert "found" in rendered


def test_unknown_tool_with_artifact_renders_and_keeps_artifact() -> None:
    block = ToolBlock(
        "u-7", "custom_unknown_tool", {"query": "q"},
        output="raw result", artifact={"provider": "custom", "rows": 3},
        status="completed",
    )
    preview = build_tool_preview(block)
    assert preview.kind == "custom_unknown_tool"
    rendered = _plain(render_transcript(CliState(blocks=[block]), 100))
    assert "custom_unknown_tool" in rendered
    assert "raw result" in rendered


def test_unknown_tool_round_trips_through_transcript_restore() -> None:
    blocks = messages_to_transcript([
        AIMessage(content="", tool_calls=[{
            "id": "u-8", "name": "custom_unknown_tool", "args": {"query": "middleware"},
        }]),
        ToolMessage(content="found 3 entries", tool_call_id="u-8", name="custom_unknown_tool"),
    ])
    assert blocks[0].kind == "tool"
    assert blocks[0].name == "custom_unknown_tool"
    assert blocks[0].status == "completed"
    state = CliState()
    state.load_transcript(blocks)
    block = state.blocks[0]
    assert isinstance(block, ToolBlock)
    assert block.name == "custom_unknown_tool"
    rendered = _plain(render_transcript(state, 100))
    assert "custom_unknown_tool middleware" in rendered
    assert "found 3 entries" in rendered


def test_delete_preview_does_not_invent_deleted_line_count() -> None:
    state = CliState(blocks=[ToolBlock(
        tool_call_id="delete-1",
        name="delete",
        arguments={"file_path": "/workspace/old-report.md"},
        status="completed",
    )])
    rendered = _plain(render_transcript(state, 100))
    assert "Deleted /workspace/old-report.md" in rendered
    assert "-83" not in rendered
    assert "lines hidden" not in rendered


def test_write_and_delete_previews_use_real_schema() -> None:
    write = render_transcript(CliState(blocks=[ToolBlock(
        tool_call_id="w1", name="write",
        arguments={"file_path": "/workspace/out.txt", "content": "hello"},
        status="waiting",
    )]), 80)
    assert "Writing workspace/out.txt" in write
    assert "(+1 -0)" in write
    assert "/dev/null" not in write
    assert "+ hello" in _plain(write)
    delete = render_transcript(CliState(blocks=[ToolBlock(
        tool_call_id="d1", name="delete",
        arguments={"file_path": "/workspace/out.txt"},
        status="waiting",
    )]), 80)
    assert "Deleting /workspace/out.txt" in delete


def test_large_new_file_preview_shows_semantic_summary_and_review() -> None:
    lines = ["# DeepSeek 接口调研报告", "", "> 生成时间：待核实", "", "---", *[f"line {index}" for index in range(297)]]
    content = "\n".join(lines) + "\n"
    args = {"file_path": "/workspace/deepseek-api-research.md", "content": content}
    state = CliState(blocks=[ToolBlock(
        tool_call_id="write-302", name="write", arguments=args, status="completed",
    )])
    preview = _plain(render_transcript(state, 100))
    assert "write workspace/deepseek-api-research.md" in preview
    assert "(+302 -0)" in preview
    assert "+ # DeepSeek 接口调研报告" in preview
    assert "+ > 生成时间：待核实" in preview
    assert "… 298 lines hidden · Ctrl+R to review" in preview
    assert "--- /dev/null" not in preview
    assert "+++ b/workspace" not in preview
    assert "@@ -0,0" not in preview
    assert "line 296" not in preview

    expanded = _plain(render_transcript(CliState(blocks=state.blocks, tools_expanded=True), 100))
    assert "DeepAgentCLI" in expanded
    assert "(+302 -0)" in expanded
    assert "+ line 296" in expanded
    assert "hidden" not in expanded
    assert "--- /dev/null" not in expanded
    assert "@@ -0,0" not in expanded


def test_truncated_tool_completion_uses_final_tail_without_snapshot() -> None:
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
        artifact={
            "exit_code": 0, "truncated": True, "max_output_bytes": 8,
            "host_log_path": "/current/workspace/.deep-agent/logs/exec/example.log",
            "agent_log_path": "/workspace/.deep-agent/logs/exec/example.log",
        },
    ))
    output = state.blocks[-1].output
    assert output.startswith("last-eight")
    assert "first-eight" not in output
    assert output.count("Full output saved to:") == 1
    assert "/current/workspace/.deep-agent/logs/exec/example.log" in output
    assert "/workspace/.deep-agent/logs/exec/example.log" in output


def test_execute_truncated_tail_keeps_latest_lines_and_agent_log_path() -> None:
    state = CliState()
    state.apply(RunEvent(
        type="tool_started", tool_call_id="truncated", name="execute",
        arguments={"command": "long-command"},
    ))
    for index in range(7):
        state.apply(RunEvent(type="tool_output_delta", tool_call_id="truncated", content=f"line {index}\n"))
    before = _plain(render_transcript(state, 100))
    assert "4 earlier output lines hidden" in before
    assert "line 6" in before and "line 2" not in before

    state.apply(RunEvent(
        type="tool_output_delta", tool_call_id="truncated", stream="tail_snapshot",
        content="\n".join(f"line {index}" for index in range(3, 8)) + "\n",
    ))
    live_tail = _plain(render_transcript(state, 100))
    assert "2 earlier output lines hidden" in live_tail
    assert "line 3" not in live_tail and "line 7" in live_tail
    assert "Output truncated" in live_tail

    state.tools_expanded = True
    state.apply(RunEvent(
        type="tool_output_delta", tool_call_id="truncated", stream="tail_snapshot",
        content="\n".join(f"line {index}" for index in range(4, 9)) + "\n",
    ))
    expanded_live = _plain(render_transcript(state, 100))
    assert "line 8" in expanded_live and "line 3" not in expanded_live
    state.tools_expanded = False

    state.apply(RunEvent(
        type="tool_completed", tool_call_id="truncated", name="execute",
        artifact={
            "exit_code": 0, "truncated": True, "max_output_bytes": 8,
            "host_log_path": "/host/exec.log", "agent_log_path": "/workspace/exec.log",
        },
    ))
    collapsed = _plain(render_transcript(state, 100))
    assert "Ran long-command" in collapsed
    assert "2 earlier output lines hidden" in collapsed
    assert "line 6" in collapsed and "line 8" in collapsed
    assert "Output truncated · full output: /workspace/exec.log" in collapsed
    assert "/host/exec.log" not in collapsed
    state.tools_expanded = True
    expanded = _plain(render_transcript(state, 100))
    assert "line 0" not in expanded and "line 4" in expanded and "line 8" in expanded
    assert "Output truncated · full output: /workspace/exec.log" in expanded


def test_execute_truncated_log_error_and_host_fallback() -> None:
    failed_save = ToolBlock(
        "save", "execute", {"command": "build"}, output="last line", status="completed",
        artifact={"truncated": True, "log_error": "disk full"},
    )
    assert "Full output could not be saved: disk full" in _plain(_capture(_tool(failed_save, False), 100))
    host_only = ToolBlock(
        "host", "execute", {"command": "build"}, output="last line", status="completed",
        artifact={"truncated": True, "host_log_path": "/host/exec.log"},
    )
    assert normalize_command_execution(host_only).log_path == "/host/exec.log"
    assert "Output truncated · full output: /host/exec.log" in _plain(_capture(_tool(host_only, False), 100))


def test_execute_completion_uses_artifact_instead_of_output_text() -> None:
    state = CliState()
    state.apply(RunEvent(type="tool_started", tool_call_id="exec-marker", name="execute"))
    state.apply(RunEvent(
        type="tool_output_delta", tool_call_id="exec-marker", content="Exit code: 7\nCancelled by user.\n",
    ))
    state.apply(RunEvent(
        type="tool_completed", tool_call_id="exec-marker", name="execute",
        content="Exit code: 7\nCancelled by user.",
        artifact={"exit_code": 0, "truncated": False, "termination_reason": None},
    ))
    assert state.blocks[-1].output == "Exit code: 7\nCancelled by user.\n"
    assert not state.blocks[-1].is_error

    state.apply(RunEvent(type="tool_started", tool_call_id="exec-failed", name="execute"))
    state.apply(RunEvent(type="tool_output_delta", tool_call_id="exec-failed", content="failed\n"))
    state.apply(RunEvent(
        type="tool_completed", tool_call_id="exec-failed", name="execute",
        content="failed\n\nExit code: 2", artifact={"exit_code": 2, "truncated": False}, is_error=True,
    ))
    assert state.blocks[-1].output == "failed\n"
    assert state.blocks[-1].is_error
    assert state.blocks[-1].exit_code == 2


def test_cli_state_stores_generic_tool_artifact() -> None:
    state = CliState()
    state.apply(RunEvent(
        type="tool_started", tool_call_id="search-1", name="web_search",
        arguments={"query": "deepagents"},
    ))
    state.apply(RunEvent(
        type="tool_completed", tool_call_id="search-1", name="web_search",
        content="Web search results for: deepagents\n5 results",
        artifact={
            "provider": "deepseek", "query": "deepagents",
            "results": [{"title": "Deep Agents", "url": "https://example.com"}],
            "response_time": 0.42,
        },
    ))
    block = next(
        block for block in state.blocks
        if isinstance(block, ToolBlock) and block.tool_call_id == "search-1"
    )
    assert block.artifact["provider"] == "deepseek"
    assert block.artifact["response_time"] == 0.42
    assert block.status == "completed"


def test_transcript_restores_tool_artifact() -> None:
    state = CliState()
    state.load_transcript([TranscriptBlock(
        kind="tool", tool_call_id="search-1", name="web_search",
        arguments={"query": "deepagents"},
        content="Web search results for: deepagents\n5 results",
        status="completed",
        artifact={
            "provider": "deepseek",
            "results": [
                {"title": "Deep Agents", "url": "https://example.com"},
                {"title": "Docs", "url": "https://docs.example.com"},
            ],
            "response_time": 0.42,
        },
    )])
    block = state.blocks[0]
    assert isinstance(block, ToolBlock)
    assert block.artifact["provider"] == "deepseek"
    assert len(block.artifact["results"]) == 2
    assert block.status == "completed"


def test_run_cancelled_keeps_streamed_tool_output() -> None:
    state = CliState()
    state.apply(RunEvent(type="tool_started", tool_call_id="exec-c", name="execute"))
    state.apply(RunEvent(type="tool_output_delta", tool_call_id="exec-c", content="already produced\n"))
    state.apply(RunEvent(type="run_cancelled"))
    tool = state.blocks[0]
    assert isinstance(tool, ToolBlock)
    assert tool.output.startswith("already produced")
    assert "Cancelled by user" not in tool.output
    assert tool.artifact["termination_reason"] == "cancelled"
    assert tool.status == "error"
    assert tool.is_error
    assert state.status == "Cancelled"
    rendered = _plain(render_transcript(state, 100))
    assert "• Cancelled" in rendered
    assert "Failed (exit 1)" not in rendered


def test_tool_started_upserts_existing_tool_call() -> None:
    state = CliState()
    state.apply(RunEvent(
        type="tool_started", tool_call_id="edit-1", name="edit",
        arguments={"file_path": "/workspace/a.py", "old_string": "x", "new_string": "y"},
    ))
    state.apply(RunEvent(
        type="tool_started", tool_call_id="edit-1", name="edit",
        arguments={"file_path": "/workspace/a.py", "old_string": "x", "new_string": "y"},
    ))
    tools = [
        block for block in state.blocks
        if isinstance(block, ToolBlock) and block.tool_call_id == "edit-1"
    ]
    assert len(tools) == 1
    assert tools[0].status == "running"


def test_waiting_tool_resume_does_not_duplicate_block() -> None:
    state = CliState()
    state.apply(RunEvent(
        type="tool_started", tool_call_id="edit-1", name="edit",
        arguments={"file_path": "/workspace/a.py", "old_string": "x", "new_string": "y"},
    ))
    state.apply(RunEvent(
        type="interaction_requested",
        result=RunResult(status="waiting_confirmation", pending_tool_calls=[
            {"toolCallId": "edit-1", "name": "edit", "args": {}},
        ]),
    ))
    state.apply(RunEvent(
        type="tool_started", tool_call_id="edit-1", name="edit",
        arguments={"file_path": "/workspace/a.py", "old_string": "x", "new_string": "y"},
    ))
    state.apply(RunEvent(
        type="tool_completed", tool_call_id="edit-1", name="edit", content="ok",
    ))
    tools = [
        block for block in state.blocks
        if isinstance(block, ToolBlock) and block.tool_call_id == "edit-1"
    ]
    assert len(tools) == 1
    assert tools[0].status == "completed"
    assert not tools[0].is_error
    assert len(state.blocks) == 1


def test_completed_tool_is_not_reopened_by_replayed_start() -> None:
    state = CliState()
    state.apply(RunEvent(type="tool_started", tool_call_id="read-1", name="read", arguments={}))
    state.apply(RunEvent(type="tool_completed", tool_call_id="read-1", name="read", content="data"))
    state.apply(RunEvent(type="tool_started", tool_call_id="read-1", name="read", arguments={}))
    tools = [
        block for block in state.blocks
        if isinstance(block, ToolBlock) and block.tool_call_id == "read-1"
    ]
    assert len(tools) == 1
    assert tools[0].status == "completed"


def test_hitl_lifecycle_keeps_a_single_tool_block() -> None:
    state = CliState()
    flow = [
        (RunEvent(type="tool_started", tool_call_id="edit-1", name="edit",
                  arguments={"file_path": "/workspace/a.py", "old_string": "x", "new_string": "y"}), "running"),
        (RunEvent(
            type="interaction_requested",
            result=RunResult(status="waiting_confirmation", pending_tool_calls=[
                {"toolCallId": "edit-1", "name": "edit", "args": {}},
            ]),
        ), "waiting"),
        (RunEvent(type="tool_started", tool_call_id="edit-1", name="edit",
                  arguments={"file_path": "/workspace/a.py", "old_string": "x", "new_string": "y"}), "running"),
        (RunEvent(type="tool_completed", tool_call_id="edit-1", name="edit", content="ok"), "completed"),
    ]
    for event, expected_status in flow:
        state.apply(event)
        tools = [
            block for block in state.blocks
            if isinstance(block, ToolBlock) and block.tool_call_id == "edit-1"
        ]
        assert len(tools) == 1
        assert tools[0].status == expected_status


def test_parallel_interaction_distinguishes_waiting_from_interrupted() -> None:
    state = CliState()
    for tool_call_id, name in (
        ("read-1", "read"), ("grep-1", "grep"), ("edit-1", "edit"),
    ):
        state.apply(RunEvent(type="tool_started", tool_call_id=tool_call_id, name=name, arguments={}))
    state.apply(RunEvent(
        type="interaction_requested",
        result=RunResult(status="waiting_confirmation", pending_tool_calls=[
            {"toolCallId": "edit-1", "name": "edit", "args": {}},
        ]),
    ))
    by_id = {
        block.tool_call_id: block.status
        for block in state.blocks if isinstance(block, ToolBlock)
    }
    assert by_id == {"read-1": "interrupted", "grep-1": "interrupted", "edit-1": "waiting"}


def test_human_input_interaction_marks_only_ask_tool_waiting() -> None:
    state = CliState()
    state.apply(RunEvent(
        type="tool_started", tool_call_id="ask-1", name="request_human_input",
        arguments={"question": "Continue?"},
    ))
    state.apply(RunEvent(type="tool_started", tool_call_id="read-1", name="read", arguments={}))
    state.apply(RunEvent(
        type="interaction_requested",
        result=RunResult(status="waiting_human", human_input={"question": "Continue?"}),
    ))
    by_id = {
        block.tool_call_id: block.status
        for block in state.blocks if isinstance(block, ToolBlock)
    }
    assert by_id == {"ask-1": "waiting", "read-1": "interrupted"}


def test_paused_interaction_interrupts_unfinished_tools() -> None:
    state = CliState()
    state.apply(RunEvent(type="tool_started", tool_call_id="read-1", name="read", arguments={}))
    state.apply(RunEvent(
        type="interaction_requested", result=RunResult(status="paused"),
    ))
    block = next(
        block for block in state.blocks
        if isinstance(block, ToolBlock) and block.tool_call_id == "read-1"
    )
    assert block.status == "interrupted"
    assert not block.is_error
    assert block.exit_code is None


def test_real_hitl_resume_keeps_a_single_tool_card() -> None:
    """The runner replays tool_started after an approval; the card must not duplicate."""
    events: list[RunEvent] = []
    prepared = create_agent(model=scripted_model([
        AIMessage(content="", tool_calls=[{
            "id": "call-write-1", "name": "write",
            "args": {"file_path": "/workspace/note.txt", "content": "hello"},
        }]),
        AIMessage(content="done"),
    ]), backend=StateBackend())
    runner = AgentRunner(prepared=prepared, thread_id="hitl-single-card")
    state = CliState()

    def on_event(event: RunEvent) -> None:
        events.append(event)
        state.apply(event)

    waiting = runner.invoke("write a note", on_event=on_event)
    assert waiting.status == "waiting_confirmation"
    tools = [b for b in state.blocks if isinstance(b, ToolBlock)]
    assert len(tools) == 1 and tools[0].status == "waiting"

    assert runner.approve_tool("call-write-1", on_event=on_event).status == "completed"
    tools = [b for b in state.blocks if isinstance(b, ToolBlock)]
    assert len(tools) == 1
    assert tools[0].status == "completed"
    assert not tools[0].is_error
    started = [e for e in events if e.type == "tool_started" and e.tool_call_id == "call-write-1"]
    assert len(started) >= 2  # the resume replay is the case the upsert guards
    rendered = _plain(render_transcript(state, 100))
    assert rendered.count("write workspace/note.txt") == 1
    runner.close()


def test_terminal_run_events_stop_every_tool_spinner() -> None:
    interaction = RunEvent(
        type="interaction_requested",
        result=RunResult(status="waiting_confirmation", pending_tool_calls=[
            {"toolCallId": str(index), "name": "grep", "args": {}}
            for index in range(3)
        ]),
    )
    for terminal_event, expected_status in (
        (interaction, "waiting"),
        (RunEvent(type="run_completed"), "interrupted"),
        (RunEvent(type="run_failed", content="boom"), "error"),
    ):
        state = CliState()
        for index in range(3):
            state.apply(RunEvent(type="tool_started", tool_call_id=str(index), name="grep"))
        state.apply(terminal_event)
        tools = [block for block in state.blocks if isinstance(block, ToolBlock)]
        assert len(tools) == 3
        assert all(block.status == expected_status and block.revision == 1 for block in tools)
        assert all(block.is_error == (terminal_event.type == "run_failed") for block in tools)
        if terminal_event.type == "run_failed":
            assert all(block.exit_code is None for block in tools)


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
    assert tool.exit_code is None
    rendered = _plain(render_transcript(state, 100))
    assert "Failed " in rendered and "Failed (exit" not in rendered

    state.load_transcript([TranscriptBlock(
        kind="tool", tool_call_id="new", name="execute", content="failed",
        is_error=True, status="error", exit_code=9,
    )])
    assert "Failed (exit 9)" in _plain(render_transcript(state, 100))


def test_restored_interrupted_write_has_static_gray_status() -> None:
    state = CliState()
    state.load_transcript([TranscriptBlock(
        kind="tool", tool_call_id="write-1", name="write",
        arguments={"file_path": "/workspace/report.md", "content": "draft"},
        status="interrupted",
    )])
    block = state.blocks[0]
    assert isinstance(block, ToolBlock)
    title = _tool(block, False).renderable.renderables[0]
    assert title.plain.startswith("• Writing workspace/report.md")
    assert "interrupted (completion unconfirmed)" in title.plain
    assert title.spans[0].style == "#888888"


def test_transcript_header_avoids_duplicate_runtime_context() -> None:
    from agent.version import package_version

    rendered = _plain(render_transcript(CliState(), 120))
    assert f"DeepAgentCLI v{package_version()}" in rendered
    assert "terminal coding agent" in rendered
    for hint in ("Esc interrupt", "F2 pending", "Ctrl+O tools", "Ctrl+R review", "/ commands"):
        assert hint not in rendered
    assert "/workspace/project · SANDBOXED · perm:ask" not in rendered




@pytest.mark.parametrize("limit", [1, 3, 6])
@pytest.mark.parametrize("status", ["running", "completed", "error", "waiting", "interrupted"])
def test_execute_command_and_output_reuse_configured_fold_limit(limit, status) -> None:
    from agent.cli.display import UiDisplayLimits

    command = "python - <<'PY'\n" + "\n".join(f"print('code_{i:02}')" for i in range(10)) + "\nPY"
    block = ToolBlock(
        "inline-python", "execute", {"command": command},
        output="\n".join(f"result_{i:02}" for i in range(10)),
        status=status, is_error=status == "error", exit_code=1 if status == "error" else None,
    )
    limits = UiDisplayLimits(head_lines=limit, tail_lines=limit)
    collapsed = _plain(_capture(_tool(block, False, 120, limits=limits), 120))
    command_part, output_part = collapsed.split("  └ output")
    command_rows = [line for line in command_part.splitlines() if line.strip()]
    assert len(command_rows) == limit + 1  # One additional row announces the hidden command.
    assert f"{12 - limit} more command lines hidden · Ctrl+O to expand" in command_part
    assert "code_09" not in command_part
    assert "result_09" in output_part and "result_00" not in output_part
    if status in {"waiting", "interrupted"}:
        assert ("waiting for input" if status == "waiting" else "completion unconfirmed") in command_part
    expanded = _plain(_capture(_tool(block, True, 120, limits=limits), 120))
    assert all(f"code_{i:02}" in expanded and f"result_{i:02}" in expanded for i in range(10))
    assert "lines hidden" not in expanded
    assert block.arguments["command"] == command


def test_execute_single_line_inline_code_is_folded_after_terminal_wrapping() -> None:
    from agent.cli.display import UiDisplayLimits

    command = 'python -c "' + "print('payload');" * 50 + 'print(\'last_statement\')"'
    block = ToolBlock("python-c", "execute", {"command": command}, status="completed")
    limits = UiDisplayLimits(head_lines=2)
    collapsed = _plain(_capture(_tool(block, False, 80, limits=limits), 80))
    command_rows = [line for line in collapsed.split("  └ output")[0].splitlines() if line.strip()]
    assert len(command_rows) == 3
    assert "command lines hidden" in collapsed
    assert "last_statement" not in collapsed
    expanded = _plain(_capture(_tool(block, True, 80, limits=limits), 80))
    assert "last_statement" in "".join(line.strip() for line in expanded.splitlines())


def _explore_tool(call_id: str, status: str = "completed") -> ToolBlock:
    return ToolBlock(call_id, "read", {"file_path": f"/workspace/{call_id}.py"}, output="x = 1", status=status)


def test_incremental_units_match_a_fresh_render_through_mutations(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.cli.state import touch

    # Both renders must see the same spinner frame; it advances every 100 ms.
    monkeypatch.setattr("agent.cli.rendering._spinner_tick", lambda: 0)
    state = CliState()
    renderer = TranscriptRenderer(ZoneInfo("UTC"))

    def check() -> None:
        assert renderer.render(state, 80) == TranscriptRenderer(ZoneInfo("UTC")).render(state, 80)

    state.add_user("first")
    state.blocks.append(_explore_tool("a"))
    check()
    # A new explore tool must merge into the group that ended at the old tail.
    state.blocks.append(_explore_tool("b"))
    check()
    state.blocks.append(MessageBlock(kind="assistant", content="answer"))
    state.blocks.append(_explore_tool("c", status="running"))
    check()
    touch(state.blocks[1]).output = "changed"
    check()
    state.blocks[-1].status = "completed"
    touch(state.blocks[-1])
    check()
    state.blocks = [block for block in state.blocks if not isinstance(block, MessageBlock) or block.kind != "user"]
    check()
    state.tools_expanded = True
    check()
    state.tools_expanded = False
    del state.blocks[-1]
    check()
    assert renderer.render(state, 60) == TranscriptRenderer(ZoneInfo("UTC")).render(state, 60)


def test_unchanged_transcript_prefix_reuses_unit_objects() -> None:
    from agent.cli.rendering import TranscriptUnitMemo, transcript_units

    state = CliState()
    for index in range(5):
        state.add_user(f"question {index}")
        state.blocks.append(MessageBlock(kind="assistant", content=f"answer {index}"))
    memo = TranscriptUnitMemo()
    first = transcript_units(state, memo=memo)
    state.blocks.append(MessageBlock(kind="assistant", content="streaming"))
    second = transcript_units(state, memo=memo)
    assert len(second) == len(first) + 1
    # The unit at the old tail is rebuilt in case the new block joins its group.
    assert all(left is right for left, right in zip(first[:-1], second))
    assert transcript_units(state, memo=memo) == second


_TITLE_CASES = [
    ("ls", {"path": "agent"}, {}, "Listing agent", "Listed agent", "Failed Listing agent"),
    ("read", {"file_path": "agent/runtime/runner.py"}, {}, "Reading agent/runtime/runner.py", "Read agent/runtime/runner.py", "Failed Reading agent/runtime/runner.py"),
    ("write", {"file_path": "notes", "content": "new"}, {"operation": "create"}, "Writing notes", "Created notes", "Failed Writing notes"),
    ("edit", {"file_path": "notes", "old_string": "old", "new_string": "new"}, {}, "Editing notes", "Edited notes", "Failed Editing notes"),
    ("delete", {"file_path": "notes"}, {}, "Deleting notes", "Deleted notes", "Failed Deleting notes"),
    ("execute", {"command": "pwd"}, {}, "Running pwd", "Ran pwd", "Failed (exit 7) pwd"),
    ("web_search", {"query": "LangGraph release"}, {}, 'Searching Web "LangGraph release"', 'Searched Web "LangGraph release"', 'Failed Searching Web "LangGraph release"'),
    ("web_fetch", {"url": "https://example.com"}, {}, "Fetching https://example.com", "Fetched https://example.com", "Failed Fetching https://example.com"),
    ("request_human_input", {"question": "How would you like to proceed?"}, {}, "Asking How would you like to proceed?", "Asked How would you like to proceed?", "Failed Asking How would you like to proceed?"),
    ("mcp_list", {}, {}, "Listing MCP Servers", "Listed MCP Servers", "Failed Listing MCP Servers"),
    ("mcp_add", {"name": "parallel-mcp"}, {}, "Adding MCP Server parallel-mcp", "Added MCP Server parallel-mcp", "Failed Adding MCP Server parallel-mcp"),
    ("mcp_remove", {"name": "parallel-mcp"}, {}, "Removing MCP Server parallel-mcp", "Removed MCP Server parallel-mcp", "Failed Removing MCP Server parallel-mcp"),
    ("compact_conversation", {}, {}, "Compacting conversation", "Compacted conversation", "Failed Compacting conversation"),
    ("read_task", {"run_id": "run-1"}, {}, "Reading Task run-1", "Read Task run-1", "Failed Reading Task run-1"),
    ("cancel_task", {"run_id": "run-1"}, {}, "Cancelling Task run-1", "Requested Task Cancellation run-1", "Failed Cancelling Task run-1"),
    ("mcp__parallel-mcp__web_search", {"q": "x"}, {"mcp_server_name": "Parallel", "mcp_tool_name": "web_search", "structured_content": {"q": "x"}}, "mcp__parallel-mcp__web_search", "MCP · Parallel · web_search", "Failed MCP · Parallel · web_search"),
]


@pytest.mark.parametrize(("name", "arguments", "artifact", "running", "completed", "failed"), _TITLE_CASES)
def test_tool_titles_follow_status(name, arguments, artifact, running, completed, failed) -> None:
    live = ToolBlock("live", name, arguments, status="running")
    assert build_tool_preview(live).title == running
    done = ToolBlock("done", name, arguments, artifact=artifact or None, status="completed")
    assert build_tool_preview(done).title == completed
    error_artifact = {"exit_code": 7} if name == "execute" else artifact or None
    broken = ToolBlock(
        "bad", name, arguments, output="Error: boom", status="error", is_error=True,
        exit_code=7 if name == "execute" else None, artifact=error_artifact,
    )
    assert build_tool_preview(broken).title == failed
    assert failed.count("Failed") == 1
    if not str(name).startswith("mcp__"):
        assert completed not in build_tool_preview(broken).title
    waiting = ToolBlock("wait", name, arguments, status="waiting")
    rendered = _plain(_capture(_tool(waiting, False), 160))
    assert running in rendered
    assert completed not in rendered
    if name == "request_human_input":
        assert "waiting for input" not in rendered
    else:
        assert "waiting for input" in rendered
    stopped = ToolBlock("stop", name, arguments, status="interrupted")
    stopped_title = build_tool_preview(stopped).title
    assert stopped_title == running
    assert completed not in stopped_title


def test_cancel_task_success_is_a_request_not_a_stopped_task() -> None:
    block = ToolBlock("cancel", "cancel_task", {"run_id": "run-1"}, output="cancellation requested", status="completed")
    assert build_tool_preview(block).title == "Requested Task Cancellation run-1"
    assert "Cancelled Task" not in build_tool_preview(block).title


def test_mcp_management_titles_ignore_result_text() -> None:
    added = ToolBlock(
        "add", "mcp_add", {"name": "parallel-mcp"},
        output="The server was not added.", status="completed",
    )
    removed = ToolBlock(
        "remove", "mcp_remove", {"name": "parallel-mcp"},
        output="Removed nothing.", status="completed",
    )
    assert build_tool_preview(added).title == "Added MCP Server parallel-mcp"
    assert build_tool_preview(removed).title == "Removed MCP Server parallel-mcp"


def test_file_tool_preview_titles_multi_file_groups_directly() -> None:
    created = [
        ToolBlock(str(index), "write", {"file_path": f"/workspace/f{index}.py", "content": "x"},
                  artifact={"operation": "create"}, status="completed")
        for index in range(3)
    ]
    edited = [
        ToolBlock(str(index), "edit", {"file_path": f"/workspace/f{index}.py", "old_string": "a", "new_string": "b"},
                  status="completed")
        for index in range(2)
    ]
    overwritten = [
        ToolBlock(str(index), "write", {"file_path": f"/workspace/f{index}.py", "content": "y"},
                  artifact={"operation": "overwrite"}, status="completed")
        for index in range(2)
    ]
    assert file_tool_preview(aggregate_file_mutations(
        [normalize_file_mutation(block) for block in created],
    )).title == "Created 3 files"
    assert file_tool_preview(aggregate_file_mutations(
        [normalize_file_mutation(block) for block in edited],
    )).title == "Edited 2 files"
    assert file_tool_preview(aggregate_file_mutations(
        [normalize_file_mutation(block) for block in overwritten],
    )).title == "Wrote 2 files"


def test_explore_child_titles_follow_each_item_status() -> None:
    blocks = [
        ToolBlock("ls", "ls", {"path": "agent"}, status="completed"),
        ToolBlock("read", "read", {"file_path": "agent/runtime/runner.py"}, status="running"),
    ]
    rendered = _plain(_capture(_explore_group(blocks), 100))
    assert "Exploring 2 items" in rendered
    assert "Listed agent" in rendered
    assert "Reading agent/runtime/runner.py" in rendered
    assert "Explored" not in rendered


def test_historical_grep_and_glob_use_generic_preview() -> None:
    grep = build_tool_preview(ToolBlock(
        "g", "grep", {"pattern": "build_tool_preview"}, status="completed",
    ))
    glob = build_tool_preview(ToolBlock(
        "gl", "glob", {"pattern": "**/*.py"}, status="running",
    ))
    assert grep.group == "other" and glob.group == "other"
    assert grep.title.startswith("grep ")
    assert glob.title.startswith("glob ")
    assert "Searched" not in grep.title and "Finding" not in glob.title and "Found" not in glob.title
    rendered = _plain(render_transcript(CliState(blocks=[
        ToolBlock("g", "grep", {"pattern": "build_tool_preview"}, status="completed"),
        ToolBlock("gl", "glob", {"pattern": "**/*.py"}, status="running"),
    ]), 100))
    assert "Exploring" not in rendered and "Explored" not in rendered
