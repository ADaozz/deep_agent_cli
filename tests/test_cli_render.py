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
from agent.cli.state import CliState, MessageBlock, ToolBlock, TurnSummaryBlock
from agent.config import Settings
from agent.runner import AgentRunner, RunEvent, RunResult, TurnTiming
from agent.session import SessionStore, TranscriptBlock, messages_to_transcript
from agent.tools.examples import build_example_tools
from agent.factory import DEFAULT_FS_TOOLS, create_agent
from tests.cli_support import (
    paint as _paint,
    plain as _plain,
    with_painted_app as _with_painted_app,
    without_diff_number as _without_diff_number,
)
from tests.conftest import scripted_model


def test_user_bubble_has_prefix_and_colored_blank_rows(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    rendered = _capture(_message(MessageBlock(kind="user", content="hello"), False)[0], 30)
    lines = rendered.splitlines()
    assert len(lines) == 3
    assert _plain(lines[0]).strip() == ""
    assert _plain(lines[1]).lstrip().startswith("› hello")
    assert _plain(lines[2]).strip() == ""
    assert all("48;2;48;48;48" in line for line in lines)


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
        assert "Ctrl+B expand" in status
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
        assert app.queue_window not in app.application.renderer._last_screen.visible_windows_to_write_positions
        assert not app.state.messages_expanded
        assert app.runner.control.pending_message_texts() == [text]
        assert app.state.blocks[-1].content == "sent content " * 80

    _with_painted_app("queue-expand", scenario)


def test_editor_has_matching_blank_rows_and_prefix() -> None:
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        body = app.application.layout.container.content
        assert not any(getattr(child, "style", None) == "class:editor-border" for child in body.children)
        editor = next(child.content for child in body.children if hasattr(getattr(child, "content", None), "children"))
        top, middle, bottom = editor.children
        prefix, input_window = middle.children
        assert top.height == bottom.height == 1
        assert top.style == bottom.style == prefix.style == input_window.style == "class:editor"
        assert prefix.content.text == "› "
        assert input_window.content is app.editor_control


def test_back_to_bottom_hint_is_centered_above_editor() -> None:
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        body = app.application.layout.container.content
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
        body = app.application.layout.container.content
        divider = next(child for child in body.children if getattr(child, "content", None) is app.interaction_divider_window)
        with set_app(app.application):
            assert not divider.filter()
            app.buffer.text = "/"
            app.buffer.complete_state = CompletionState(app.buffer.document, [Completion("/help")])
            assert not divider.filter()
            app.interaction = InteractionController(
                kind="model", title="Select model", question="Choose a model",
                fields=[{"id": "model", "type": "single_select", "label": "Model", "options": []}],
            )
            assert divider.filter()
            app.interaction = InteractionController.approval([{"toolCallId": "x", "name": "execute", "args": {}}])
            assert divider.filter()
            app.interaction = None
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


def test_thinking_collapses_to_latest_five_lines_by_default() -> None:
    lines = [f"think {index}" for index in range(8)]
    state = CliState(blocks=[MessageBlock(kind="assistant", thinking="\n".join(lines), content="done")])
    collapsed = _plain(render_transcript(state, 100))
    assert "… 3 earlier thinking lines hidden · Ctrl+T to expand" in collapsed
    assert "think 0" not in collapsed and "think 2" not in collapsed
    assert all(f"think {index}" in collapsed for index in range(3, 8))

    state.thinking_collapsed = False
    expanded = _plain(render_transcript(state, 100))
    assert "earlier thinking lines hidden" not in expanded
    assert all(f"think {index}" in expanded for index in range(8))

    short = CliState(blocks=[MessageBlock(
        kind="assistant", thinking="\n".join(f"short {index}" for index in range(5)), content="",
    )])
    short_view = _plain(render_transcript(short, 100))
    assert "earlier thinking lines hidden" not in short_view
    assert all(f"short {index}" in short_view for index in range(5))


def test_explore_tools_collapse_into_summary() -> None:
    assert {"ls", "read_file"}.issubset(DEFAULT_FS_TOOLS)
    assert "grep" not in DEFAULT_FS_TOOLS
    assert "glob" not in DEFAULT_FS_TOOLS
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
    assert "Explored 7 items" in collapsed
    assert "… 2 more" in collapsed
    assert 'Search "bar"' in collapsed
    assert 'Glob "*.py"' in collapsed
    assert "List /workspace" in collapsed
    assert 'Search "foo"' not in collapsed
    assert "Ctrl+O to expand" in collapsed
    assert "/workspace/a.py" not in collapsed
    assert "Ran pytest" in collapsed
    assert "write workspace/out.txt" in collapsed
    state.tools_expanded = True
    expanded = render_transcript(state, 80)
    assert "read /workspace/a.py" in expanded
    assert "items hidden" not in expanded


@pytest.mark.parametrize("count", [1, 5, 6, 74])
def test_explore_preview_shows_only_last_five_in_order(count: int) -> None:
    blocks = [
        ToolBlock(str(index), "read_file", {"file_path": f"file-{index}.py"}, status="completed")
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
        ToolBlock(str(index), ("ls", "glob", "grep")[index % 3], {}, status="running")
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
        "long", "read_file",
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
    block = ToolBlock("read", "read_file", {"file_path": "app.py"}, status=status, is_error=is_error)
    rendered = _plain(_capture(_explore_group([block]), 100))
    assert expected in rendered
    if status != "completed":
        assert "Explored" not in rendered


@pytest.mark.parametrize(("name", "arguments", "artifact", "running", "completed"), [
    ("edit_file", {"file_path": "notes", "old_string": "old", "new_string": "new"}, {}, "Editing", "Edited"),
    ("write_file", {"file_path": "notes", "content": "new"}, {"operation": "create"}, "Writing", "Created"),
    ("write_file", {"file_path": "notes", "content": "new"}, {"operation": "overwrite"}, "Writing", "Wrote"),
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
        ("write_file", {"file_path": "note.txt", "content": "hello"}),
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
        "参数校验失败，`request_human_input` 没有执行，也没有产生任何副作用。\n"
        "请按下面的说明修正参数后重新调用。\n\n错误：\n"
        "- fields: 收到 str → 必须是原生 JSON 数组\n"
        "  收到内容：[serialized fields]\n"
        "- recommendation: 收到 str → 必须是 JSON 对象\n"
        "  收到内容：{serialized recommendation}\n\n"
        "`request_human_input` 的参数 schema：\n{large schema}"
    )
    state = CliState(blocks=[ToolBlock(
        "ask", "request_human_input", {"question": "下一步怎么处理？"},
        output=output, status="error", is_error=True,
    )])
    collapsed = _plain(render_transcript(state, 100))
    assert "Failed (exit 1) ask 下一步怎么处理？" in collapsed
    assert "fields: 收到 str" in collapsed
    assert "recommendation: 收到 str" in collapsed
    assert "schema" not in collapsed
    assert "serialized" not in collapsed
    state.tools_expanded = True
    expanded = _plain(render_transcript(state, 100))
    assert "{large schema}" in expanded
    assert "serialized fields" in expanded


def test_failed_mutation_shows_error_instead_of_unapplied_diff() -> None:
    state = CliState(blocks=[ToolBlock(
        "write", "write_file", {"file_path": "/workspace/out.txt", "content": "hello"},
        output="Error: permission denied", status="error", is_error=True,
    )])
    for expanded in (False, True):
        state.tools_expanded = expanded
        rendered = _plain(render_transcript(state, 100))
        assert "Failed (exit 1) write workspace/out.txt" in rendered
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
    assert "".join(line[4:] for line in lines[divider + 1:]).replace(" ", "") == output.replace(" ", "")
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
    assert "a" * 180 in "".join(line.strip() for line in title.splitlines())


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
        "wrap", "read_file", {"file_path": "a" * 180},
        output=output, status="error", is_error=True,
    )
    _assert_card_indentation(_capture(_tool(block, expanded, width), width), width, output)


def test_explore_group_keeps_last_five_and_surfaces_older_failures() -> None:
    blocks = [ToolBlock(str(index), "read_file", {"file_path": f"file-{index}.py"}, status="completed") for index in range(8)]
    blocks[0].status = "error"
    blocks[0].is_error = True
    blocks[0].output = "Error: permission denied"
    group = _explore_group(blocks, 100)
    rendered = _plain(_capture(group, 100))
    assert "Explore 8 items · 1 failed" in rendered
    assert "… 3 more" in rendered
    assert "└ Read file-7.py" in rendered
    assert "Failed (exit 1) Read file-0.py: Error: permission denied" in rendered
    assert group.renderables[1].spans[0].style == "#888888"


def test_explore_group_caps_failure_preview_and_terminal_width() -> None:
    blocks = [ToolBlock(
        str(index), "glob", {"pattern": f"file-{index}.py"},
        output="Error: search failed", status="error", is_error=True,
    ) for index in range(10)]
    rendered = _plain(_capture(_explore_group(blocks, 80), 80))
    assert "Explore 10 items · 10 failed" in rendered
    assert "… 7 more failed" in rendered
    assert rendered.count("Failed (exit 1)") == 3
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
    assert "Ran pytest tests/" in collapsed
    assert "└ output" in collapsed
    assert "26 earlier output lines hidden" in collapsed
    assert "line 25" not in collapsed
    assert collapsed.index("26 earlier output lines hidden") < collapsed.index("line 26")
    assert "line 29" in collapsed
    assert "line 0" not in collapsed
    state.tools_expanded = True
    expanded = render_transcript(state, 80)
    assert "line 0" in expanded
    assert "output lines hidden" not in expanded


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
    assert "2 earlier output lines hidden · Ctrl+O to expand" in collapsed
    assert "line 2" in collapsed and "line 5" in collapsed
    assert "line 1" not in collapsed

    state.apply(RunEvent(type="tool_output_delta", tool_call_id="stream", content="line 6\n"))
    collapsed = _plain(render_transcript(state, 100))
    assert "3 earlier output lines hidden" in collapsed
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
    assert "4 earlier output lines hidden" in collapsed
    assert "line 4" in collapsed and "line 7" in collapsed

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
    assert f"execute pytest -q — {expected}" in rendered
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
    assert "execute pytest -q — interrupted (completion unconfirmed)" in rendered
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
        name="edit_file",
        arguments={"file_path": "/workspace/tests/test_cache.py", "old_string": old, "new_string": new},
        status="waiting",
    )])
    rendered = render_transcript(state, 80)
    assert "edit /workspace/tests/test_cache.py" in rendered
    assert "waiting for input" in rendered
    assert "--- a/workspace/tests/test_cache.py" not in rendered
    assert "+++ b/workspace/tests/test_cache.py" not in rendered
    assert "@@" not in rendered
    assert "- keep-0" in rendered
    assert "keep-15" not in rendered
    assert "Ctrl+R to review" in rendered
    review = render_review([("edit_file", {
        "file_path": "/workspace/tests/test_cache.py", "old_string": old, "new_string": new,
    })], 80)
    assert "keep-15" in review
    assert "--- a/workspace/tests/test_cache.py" in review


def test_edit_preview_excludes_context_and_metadata_lines() -> None:
    state = CliState(blocks=[ToolBlock(
        tool_call_id="edit-ctx",
        name="edit_file",
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
        tool_call_id="edit-numbered", name="edit_file",
        arguments={
            "file_path": "/workspace/notes.md",
            "old_string": "first\nsecond\nthird",
            "new_string": "first\nchanged\nthird",
        },
        status="completed",
    )
    rendered = render_transcript(CliState(blocks=[block]), 100)
    plain = _plain(rendered)
    assert "2 - second" in plain
    assert "2 + changed" in plain
    assert re.search(r"\x1b\[32m\+1\x1b\[0m", rendered)
    assert re.search(r"\x1b\[31m-1\x1b\[0m", rendered)
    review = _plain(render_review([("edit_file", block.arguments)], 100))
    assert "2 -second" in review
    assert "2 +changed" in review


def test_consecutive_edits_to_same_file_share_one_card_and_review() -> None:
    path = "/workspace/report.md"
    blocks = [ToolBlock(
        tool_call_id=f"edit-{index}", name="edit_file",
        arguments={"file_path": path, "old_string": f"old {index}", "new_string": f"entry {index}"},
        status="completed",
    ) for index in range(4)]
    state = CliState(blocks=blocks)
    rendered = _plain(render_transcript(state, 100))
    assert rendered.count(f"Edited {path}") == 1
    assert f"Edited {path} (+4 -4)" in rendered
    for index in range(4):
        assert f"+ entry {index}" in rendered
    app = CliApplication.__new__(CliApplication)
    app.state = state
    app.interaction = None
    calls = app._review_calls()
    assert len(calls) == 4
    review = _plain(render_review(calls, 100))
    for index in range(4):
        assert f"+entry {index}" in review


def test_edit_group_stops_at_other_file_or_message() -> None:
    def edit(identifier: str, path: str) -> ToolBlock:
        return ToolBlock(
            tool_call_id=identifier, name="edit_file",
            arguments={"file_path": path, "old_string": "x", "new_string": "y"},
            status="completed",
        )

    path = "/workspace/report.md"
    state = CliState(blocks=[
        edit("one", path), edit("other", "/workspace/other.md"), edit("two", path),
        MessageBlock(kind="assistant", content="done"), edit("three", path),
    ])
    rendered = _plain(render_transcript(state, 100))
    assert rendered.count(f"Edited {path}") == 3


def test_diff_background_fills_each_visual_row_in_preview_and_review(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    block = ToolBlock(
        tool_call_id="edit-background", name="edit_file",
        arguments={
            "file_path": "/workspace/notes.md",
            "old_string": "old",
            "new_string": "x" * 80,
        },
        status="completed",
    )
    for rendered in (
        render_transcript(CliState(blocks=[block]), 40),
        render_review([("edit_file", block.arguments)], 40),
        render_transcript(CliState(blocks=[ToolBlock(
            tool_call_id="write-background", name="write_file",
            arguments={"file_path": "/workspace/new.md", "content": "new"}, status="completed",
        )]), 40),
        render_review([("write_file", {"file_path": "/workspace/new.md", "content": "new"})], 40),
    ):
        for color in ("95;0;0", "0;95;0"):
            rows = re.findall(rf"\x1b\[[0-9;]*48;2;{color}m([^\n]*?)\x1b\[0m", rendered)
            if color == "0;95;0":
                assert rows
            if rows:
                assert all(len(row) == 39 for row in rows)
                assert all(row.endswith(" ") for row in rows if len(row.strip()) < 39)


def test_large_write_preview_has_numbered_full_width_green_rows(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    content = "\n".join(["#!/usr/bin/env node", "/**", *[f"line {index}" for index in range(116)]])
    block = ToolBlock(
        "script", "write_file",
        {"file_path": "workspace/login-coolcollege.mjs", "content": content},
        status="completed",
    )
    rendered = render_transcript(CliState(blocks=[block]), 60)
    plain = _plain(rendered)
    assert re.search(r"(?m)^\s+1 \+#!/usr/bin/env node", plain)
    assert re.search(r"(?m)^\s+2 \+/\*\*", plain)
    assert "… 112 lines hidden · Ctrl+R to review" in plain
    green_rows = re.findall(r"\x1b\[[0-9;]*48;2;0;95;0m([^\n]*?)\x1b\[0m", rendered)
    assert green_rows and all(len(row) == 59 for row in green_rows)

    review = _plain(render_review([("write_file", block.arguments)], 60))
    assert re.search(r"(?m)^\s+118 \+line 115", review)


def test_edit_context_stays_unhighlighted_while_changed_rows_fill_width(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    args = {
        "file_path": "/workspace/example.py",
        "old_string": "keep\nold\nend",
        "new_string": "keep\nnew\nend",
    }
    rendered = render_review([("edit_file", args)], 50)
    plain = _plain(rendered)
    assert re.search(r"(?m)^\s+2 -old", plain)
    assert re.search(r"(?m)^\s+2 \+new", plain)
    assert re.search(r"(?m)^\s+1  keep", plain)
    assert "\x1b[48;2;" not in next(
        line for line in rendered.splitlines() if "keep" in line
    )
    for color in ("95;0;0", "0;95;0"):
        rows = re.findall(rf"\x1b\[[0-9;]*48;2;{color}m([^\n]*?)\x1b\[0m", rendered)
        assert rows and all(len(row) == 49 for row in rows)


@pytest.mark.parametrize("view", ["preview", "review"])
@pytest.mark.parametrize("depth,green,red", [
    ("DEPTH_4_BIT", "42", "41"),
    ("DEPTH_8_BIT", "48;5;22", "48;5;52"),
    ("DEPTH_24_BIT", "48;2;0;95;0", "48;2;95;0;0"),
])
def test_edit_highlights_keep_red_and_green_in_terminal_output(view, depth, green, red) -> None:
    from io import StringIO
    from prompt_toolkit import ANSI
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.formatted_text import to_formatted_text
    from prompt_toolkit.output.color_depth import ColorDepth
    from prompt_toolkit.output.vt100 import Vt100_Output
    from prompt_toolkit.styles import Style

    block = ToolBlock("edit-colors", "edit_file", {
        "file_path": "/workspace/example.py", "old_string": "old", "new_string": "new",
    }, status="completed")
    if view == "preview":
        fragments, _ = TranscriptRenderer().render(CliState(blocks=[block]), 60)
    else:
        fragments = to_formatted_text(ANSI(render_review([("edit_file", block.arguments)], 60)))
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


def test_edit_review_keeps_context_headers_and_full_diff() -> None:
    args = {
        "file_path": "/workspace/notes.md",
        "old_string": "first\nsecond\nthird",
        "new_string": "first\nchanged\nthird",
    }
    review = _plain(render_review([("edit_file", args)], 100))
    assert "--- a/workspace/notes.md" in review
    assert "+++ b/workspace/notes.md" in review
    assert "@@" in review
    assert " first" in review
    assert "-second" in review
    assert "+changed" in review


def test_edit_preview_pure_deletion_over_budget() -> None:
    old = "\n".join(f"line-{index}" for index in range(10))
    block = ToolBlock(
        tool_call_id="edit-del",
        name="edit_file",
        arguments={"file_path": "/workspace/f.md", "old_string": old, "new_string": ""},
        status="completed",
    )
    state = CliState(blocks=[block])
    rendered = _plain(render_transcript(state, 100))
    assert "Edited /workspace/f.md (+0 -10)" in rendered
    assert "- line-0" in rendered
    assert "- line-7" in rendered
    assert "- line-8" not in rendered
    assert "- line-9" not in rendered
    assert "… 2 changed lines hidden · Ctrl+R to review" in rendered
    changed = [line for line in build_tool_preview(block).lines if _without_diff_number(line.text).startswith(("-", "+"))]
    assert len(changed) == 8
    assert all(_without_diff_number(line.text).startswith("-") for line in changed)


def test_edit_with_empty_old_string_stays_edited_and_keeps_review_diff() -> None:
    new = "\n".join(f"line-{index}" for index in range(10))
    block = ToolBlock(
        tool_call_id="edit-add",
        name="edit_file",
        arguments={"file_path": "/workspace/f.md", "old_string": "", "new_string": new},
        status="completed",
    )
    rendered = _plain(render_transcript(CliState(blocks=[block]), 100))
    assert "Edited /workspace/f.md (+10 -0)" in rendered
    assert "+ line-0" in rendered
    review = _plain(render_review([("edit_file", block.arguments)], 100))
    assert "+line-0" in review
    assert "+line-9" in review


def test_consecutive_creates_collapse_and_ctrl_o_expands_file_list() -> None:
    blocks = [ToolBlock(
        tool_call_id=f"create-{index}", name="write_file",
        arguments={
            "file_path": f"/workspace/file-{index}.md",
            "content": f"content {index}",
        },
        artifact={"operation": "create"},
        status="completed",
    ) for index in range(18)]
    state = CliState(blocks=blocks)
    collapsed = _plain(render_transcript(state, 100))
    assert collapsed.count("Created 18 files") == 1
    assert "├ … 13 more" in collapsed
    assert "└ /workspace/file-17.md" in collapsed
    assert "/workspace/file-0.md" not in collapsed
    assert "Ctrl+O to expand" in collapsed

    state.tools_expanded = True
    expanded = _plain(render_transcript(state, 100))
    assert "Created 18 files" in expanded
    assert all(f"/workspace/file-{index}.md" in expanded for index in range(18))
    assert "more" not in expanded
    assert "Ctrl+O to expand" not in expanded
    app = CliApplication.__new__(CliApplication)
    app.state = state
    app.interaction = None
    review = _plain(render_review(app._review_calls(), 100))
    assert "+content 0" in review
    assert "+content 17" in review


def test_write_operation_artifact_controls_create_and_overwrite_titles() -> None:
    path = "/workspace/report.md"
    args = {"file_path": path, "content": "hello"}
    created = ToolBlock("write-new", "write_file", args, artifact={"operation": "create"}, status="completed")
    overwritten = ToolBlock("write-old", "write_file", args, artifact={"operation": "overwrite"}, status="completed")
    unknown = ToolBlock("write-legacy", "write_file", args, status="completed")
    edited = ToolBlock(
        "edit-old-empty", "edit_file",
        {"file_path": path, "old_string": "", "new_string": "hello"},
        artifact={"operation": "create"}, status="completed",
    )
    assert "• Created /workspace/report.md" in _plain(render_transcript(CliState(blocks=[created]), 100))
    assert "• Wrote /workspace/report.md" in _plain(render_transcript(CliState(blocks=[overwritten]), 100))
    assert "• write workspace/report.md" in _plain(render_transcript(CliState(blocks=[unknown]), 100))
    assert "• Edited /workspace/report.md" in _plain(render_transcript(CliState(blocks=[edited]), 100))


def test_file_mutation_normalizer_produces_semantics_and_compact_rows() -> None:
    path = "/workspace/report.md"
    created = ToolBlock("create", "write_file", {"file_path": path, "content": "x"},
                        artifact={"operation": "create"}, status="completed")
    overwritten = ToolBlock("overwrite", "write_file", {
        "file_path": path, "content": "\n".join(f"line-{index}" for index in range(10)),
    }, artifact={"operation": "overwrite"}, status="completed")
    edited = ToolBlock("modify", "edit_file", {
        "file_path": path, "old_string": "old", "new_string": "new",
    }, status="completed")
    deleted = ToolBlock("delete", "delete", {"file_path": path}, status="completed")
    assert normalize_file_mutation(created).operation == "create"
    assert normalize_file_mutation(edited).operation == "modify"
    assert normalize_file_mutation(overwritten).operation == "overwrite"
    assert normalize_file_mutation(deleted).operation == "delete"
    compact = normalize_file_mutation(overwritten).compact_lines
    assert len([line for line in compact if line.style == "add"]) == 6
    assert compact[-1].text == "… 4 lines hidden · Ctrl+R to review"
    assert normalize_file_mutation(ToolBlock("legacy", "write_file", overwritten.arguments,
                                            status="completed")) is None


def test_create_aggregation_consumes_semantics_without_tool_identity() -> None:
    mutations = [FileMutationPreview(f"/workspace/{index}.py", "create") for index in range(7)]
    collapsed = aggregate_file_mutations(mutations)
    expanded = aggregate_file_mutations(mutations, expanded=True)
    assert collapsed.paths == tuple(mutation.path for mutation in mutations)
    assert collapsed.compact_lines[0].text == "  ├ … 2 more"
    assert collapsed.compact_lines[-1].text == "  Ctrl+O to expand"
    assert len(expanded.compact_lines) == 7


def test_edit_preview_many_deletions_keep_one_addition() -> None:
    old = "\n".join(f"keep-{index}" for index in range(20))
    block = ToolBlock(
        tool_call_id="edit-floor-plus",
        name="edit_file",
        arguments={"file_path": "/workspace/file.md", "old_string": old, "new_string": "replaced"},
        status="completed",
    )
    state = CliState(blocks=[block])
    rendered = _plain(render_transcript(state, 100))
    assert "Edited /workspace/file.md (+1 -20)" in rendered
    assert "+ replaced" in rendered
    assert "- keep-6" in rendered
    assert "- keep-7" not in rendered
    assert "⋮" in rendered
    assert "… 13 changed lines hidden · Ctrl+R to review" in rendered
    texts = [_without_diff_number(line.text) for line in build_tool_preview(block).lines]
    assert texts == [
        "",
        *[f"- keep-{index}" for index in range(7)],
        "  ⋮",
        "+ replaced",
        "",
        "… 13 changed lines hidden · Ctrl+R to review",
    ]
    review = _plain(render_review([("edit_file", block.arguments)], 100))
    assert "-keep-15" in review
    assert "+replaced" in review


def test_edit_preview_many_additions_keep_one_deletion() -> None:
    new = "\n".join(f"add-{index}" for index in range(20))
    block = ToolBlock(
        tool_call_id="edit-floor-minus",
        name="edit_file",
        arguments={"file_path": "/workspace/file.md", "old_string": "keep", "new_string": new},
        status="completed",
    )
    rendered = _plain(render_transcript(CliState(blocks=[block]), 100))
    assert "Edited /workspace/file.md (+20 -1)" in rendered
    assert "- keep" in rendered
    assert "+ add-6" in rendered
    assert "+ add-7" not in rendered
    assert "… 13 changed lines hidden · Ctrl+R to review" in rendered
    texts = [_without_diff_number(line.text) for line in build_tool_preview(block).lines]
    assert texts == [
        "",
        "- keep",
        *[f"+ add-{index}" for index in range(7)],
        "",
        "… 13 changed lines hidden · Ctrl+R to review",
    ]
    assert "  ⋮" not in texts


def test_edit_preview_under_budget_shows_all_without_hidden_hint() -> None:
    block = ToolBlock(
        tool_call_id="edit-small",
        name="edit_file",
        arguments={"file_path": "/workspace/notes.md", "old_string": "l1\nl2\nl3", "new_string": "n1\nn2"},
        status="completed",
    )
    rendered = _plain(render_transcript(CliState(blocks=[block]), 100))
    assert "Edited /workspace/notes.md (+2 -3)" in rendered
    for line in ("- l1", "- l2", "- l3", "+ n1", "+ n2"):
        assert line in rendered
    assert "changed lines hidden" not in rendered
    assert "⋮" not in rendered


def test_edit_preview_marks_hunk_jumps_with_separator() -> None:
    keep = "\n".join(f"M{index}" for index in range(7))
    block = ToolBlock(
        tool_call_id="edit-hunks",
        name="edit_file",
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
    assert texts == ["", "- old1", "+ new1", "  ⋮", "- old2", "+ new2"]


def test_edit_preview_single_hunk_never_shows_separator() -> None:
    block = ToolBlock(
        tool_call_id="edit-single-hunk",
        name="edit_file",
        arguments={"file_path": "/workspace/notes.md", "old_string": "a\nb\nc", "new_string": "a\nb\nx"},
        status="completed",
    )
    texts = [_without_diff_number(line.text) for line in build_tool_preview(block).lines]
    assert texts == ["", "- c", "+ x"]


def test_edit_preview_separator_does_not_consume_budget() -> None:
    keep_p = "\n".join(f"P{index}" for index in range(7))
    keep_q = "\n".join(f"Q{index}" for index in range(7))
    block = ToolBlock(
        tool_call_id="edit-three-hunks",
        name="edit_file",
        arguments={
            "file_path": "/workspace/file.md",
            "old_string": f"A\n{keep_p}\nB1\nB2\n{keep_q}\nD",
            "new_string": f"A2\n{keep_p}\nB\n{keep_q}\nD1\nD2",
        },
        status="completed",
    )
    texts = [_without_diff_number(line.text) for line in build_tool_preview(block).lines]
    changed = [text for text in texts if text.startswith(("-", "+"))]
    assert len(changed) == 8
    assert texts.count("  ⋮") == 2
    assert texts == [
        "", "- A", "+ A2", "  ⋮", "- B1", "- B2", "+ B", "  ⋮", "- D", "+ D1", "+ D2",
    ]
    assert "changed lines hidden" not in _plain(render_transcript(CliState(blocks=[block]), 100))


def test_edit_preview_hidden_count_counts_only_changed_lines() -> None:
    keep = "\n".join(f"N{index}" for index in range(7))
    old = "\n".join(f"A{index}" for index in range(7)) + f"\n{keep}\n" + "\n".join(f"B{index}" for index in range(7))
    block = ToolBlock(
        tool_call_id="edit-hidden-count",
        name="edit_file",
        arguments={"file_path": "/workspace/file.md", "old_string": old, "new_string": keep},
        status="completed",
    )
    texts = [_without_diff_number(line.text) for line in build_tool_preview(block).lines]
    changed = [text for text in texts if text.startswith(("-", "+"))]
    assert len(changed) == 8
    assert texts.count("  ⋮") == 1
    assert texts[-1] == "… 6 changed lines hidden · Ctrl+R to review"


def test_edit_preview_wraps_long_lines_before_terminal_edge() -> None:
    block = ToolBlock(
        tool_call_id="edit-wide",
        name="edit_file",
        arguments={"file_path": "/workspace/f.md", "old_string": "short", "new_string": "x" * 200},
        status="completed",
    )
    rendered = _plain(_capture(_tool(block, False), 80))
    assert all(len(line) < 80 for line in rendered.splitlines())
    assert rendered.count("x") >= 200


def test_edit_preview_empty_new_string_still_shows_deletion() -> None:
    block = ToolBlock(
        tool_call_id="edit-empty-new",
        name="edit_file",
        arguments={"file_path": "/workspace/f.md", "old_string": "foo", "new_string": ""},
        status="completed",
    )
    rendered = _plain(render_transcript(CliState(blocks=[block]), 100))
    assert "- foo" in rendered
    assert "changed lines hidden" not in rendered
    review = _plain(render_review([("edit_file", block.arguments)], 100))
    assert "-foo" in review


def test_edit_preview_empty_old_string_shows_addition() -> None:
    block = ToolBlock(
        tool_call_id="edit-empty-old",
        name="edit_file",
        arguments={"file_path": "/workspace/f.md", "old_string": "", "new_string": "foo"},
        status="completed",
    )
    rendered = _plain(render_transcript(CliState(blocks=[block]), 100))
    assert "Edited /workspace/f.md (+1 -0)" in rendered
    assert "+ foo" in rendered
    review = _plain(render_review([("edit_file", block.arguments)], 100))
    assert "+foo" in review


def test_edit_preview_both_empty_strings_has_no_change() -> None:
    args = {"file_path": "/workspace/f.md", "old_string": "", "new_string": ""}
    block = ToolBlock(
        tool_call_id="edit-both-empty", name="edit_file", arguments=args,
        status="completed",
    )
    assert build_diff_preview("edit_file", args) is None
    rendered = _plain(render_transcript(CliState(blocks=[block]), 100))
    assert "Edited /workspace/f.md" in rendered
    assert "(+" not in rendered
    assert " - " not in rendered
    assert " + " not in rendered
    assert "Nothing to review." in _plain(render_review([("edit_file", args)], 100))


def test_write_empty_file_has_no_fabricated_diff() -> None:
    args = {"file_path": "/workspace/empty.md", "content": ""}
    block = ToolBlock(
        tool_call_id="write-empty", name="write_file", arguments=args,
        status="completed",
    )
    assert build_diff_preview("write_file", args) is None
    rendered = _plain(render_transcript(CliState(blocks=[block]), 100))
    assert "write /workspace/empty.md" in rendered
    assert "+0 lines" not in rendered
    review = _plain(render_review([("write_file", args)], 100))
    assert "Nothing to review." in review

    created = ToolBlock(
        tool_call_id="write-empty-created", name="write_file", arguments=args,
        artifact={"operation": "create"}, status="completed",
    )
    assert "• Created /workspace/empty.md" in _plain(render_transcript(CliState(blocks=[created]), 100))


def test_edit_preview_counts_added_deleted() -> None:
    state = CliState(blocks=[ToolBlock(
        tool_call_id="edit-3",
        name="edit_file",
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
        name="edit_file",
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
    assert "… 4 output lines hidden · Ctrl+O to expand" in collapsed


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
    assert "Failed (exit 1) custom_unknown_tool" in rendered
    assert "boom" in rendered


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
        tool_call_id="w1", name="write_file",
        arguments={"file_path": "/workspace/out.txt", "content": "hello"},
        status="waiting",
    )]), 80)
    assert "write workspace/out.txt" in write
    assert "+1 line" in write
    assert "/dev/null" not in write
    assert "+hello" in write
    delete = render_transcript(CliState(blocks=[ToolBlock(
        tool_call_id="d1", name="delete",
        arguments={"file_path": "/workspace/out.txt"},
        status="waiting",
    )]), 80)
    assert "delete /workspace/out.txt" in delete


def test_large_new_file_preview_shows_semantic_summary_and_review() -> None:
    lines = ["# Tavily 接口调研报告", "", "> 生成时间：待核实", "", "---", *[f"line {index}" for index in range(297)]]
    content = "\n".join(lines) + "\n"
    args = {"file_path": "/workspace/tavily-api-research.md", "content": content}
    state = CliState(blocks=[ToolBlock(
        tool_call_id="write-302", name="write_file", arguments=args, status="completed",
    )])
    preview = _plain(render_transcript(state, 100))
    assert "write workspace/tavily-api-research.md" in preview
    assert "+302 lines" in preview
    assert "+# Tavily 接口调研报告" in preview
    assert "+> 生成时间：待核实" in preview
    assert "… 296 lines hidden · Ctrl+R to review" in preview
    assert "--- /dev/null" not in preview
    assert "+++ b/workspace" not in preview
    assert "@@ -0,0" not in preview
    assert "line 296" not in preview

    review = _plain(render_review([("write_file", args)], 100))
    assert "+302 lines" in review
    assert "+line 296" in review
    assert "--- /dev/null" not in review
    assert "@@ -0,0" not in review


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
    assert "3 earlier output lines hidden" in before
    assert "line 6" in before and "line 2" not in before

    state.apply(RunEvent(
        type="tool_output_delta", tool_call_id="truncated", stream="tail_snapshot",
        content="\n".join(f"line {index}" for index in range(3, 8)) + "\n",
    ))
    live_tail = _plain(render_transcript(state, 100))
    assert "1 earlier output lines hidden" in live_tail
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
    assert "1 earlier output lines hidden" in collapsed
    assert "line 5" in collapsed and "line 8" in collapsed
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
            "provider": "tavily", "query": "deepagents",
            "results": [{"title": "Deep Agents", "url": "https://example.com"}],
            "response_time": 0.42,
        },
    ))
    block = next(
        block for block in state.blocks
        if isinstance(block, ToolBlock) and block.tool_call_id == "search-1"
    )
    assert block.artifact["provider"] == "tavily"
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
            "provider": "tavily",
            "results": [
                {"title": "Deep Agents", "url": "https://example.com"},
                {"title": "Docs", "url": "https://docs.example.com"},
            ],
            "response_time": 0.42,
        },
    )])
    block = state.blocks[0]
    assert isinstance(block, ToolBlock)
    assert block.artifact["provider"] == "tavily"
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
        type="tool_started", tool_call_id="edit-1", name="edit_file",
        arguments={"file_path": "/workspace/a.py", "old_string": "x", "new_string": "y"},
    ))
    state.apply(RunEvent(
        type="tool_started", tool_call_id="edit-1", name="edit_file",
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
        type="tool_started", tool_call_id="edit-1", name="edit_file",
        arguments={"file_path": "/workspace/a.py", "old_string": "x", "new_string": "y"},
    ))
    state.apply(RunEvent(
        type="interaction_requested",
        result=RunResult(status="waiting_confirmation", pending_tool_calls=[
            {"toolCallId": "edit-1", "name": "edit_file", "args": {}},
        ]),
    ))
    state.apply(RunEvent(
        type="tool_started", tool_call_id="edit-1", name="edit_file",
        arguments={"file_path": "/workspace/a.py", "old_string": "x", "new_string": "y"},
    ))
    state.apply(RunEvent(
        type="tool_completed", tool_call_id="edit-1", name="edit_file", content="ok",
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
    state.apply(RunEvent(type="tool_started", tool_call_id="read-1", name="read_file", arguments={}))
    state.apply(RunEvent(type="tool_completed", tool_call_id="read-1", name="read_file", content="data"))
    state.apply(RunEvent(type="tool_started", tool_call_id="read-1", name="read_file", arguments={}))
    tools = [
        block for block in state.blocks
        if isinstance(block, ToolBlock) and block.tool_call_id == "read-1"
    ]
    assert len(tools) == 1
    assert tools[0].status == "completed"


def test_hitl_lifecycle_keeps_a_single_tool_block() -> None:
    state = CliState()
    flow = [
        (RunEvent(type="tool_started", tool_call_id="edit-1", name="edit_file",
                  arguments={"file_path": "/workspace/a.py", "old_string": "x", "new_string": "y"}), "running"),
        (RunEvent(
            type="interaction_requested",
            result=RunResult(status="waiting_confirmation", pending_tool_calls=[
                {"toolCallId": "edit-1", "name": "edit_file", "args": {}},
            ]),
        ), "waiting"),
        (RunEvent(type="tool_started", tool_call_id="edit-1", name="edit_file",
                  arguments={"file_path": "/workspace/a.py", "old_string": "x", "new_string": "y"}), "running"),
        (RunEvent(type="tool_completed", tool_call_id="edit-1", name="edit_file", content="ok"), "completed"),
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
        ("read-1", "read_file"), ("grep-1", "grep"), ("edit-1", "edit_file"),
    ):
        state.apply(RunEvent(type="tool_started", tool_call_id=tool_call_id, name=name, arguments={}))
    state.apply(RunEvent(
        type="interaction_requested",
        result=RunResult(status="waiting_confirmation", pending_tool_calls=[
            {"toolCallId": "edit-1", "name": "edit_file", "args": {}},
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
    state.apply(RunEvent(type="tool_started", tool_call_id="read-1", name="read_file", arguments={}))
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
    state.apply(RunEvent(type="tool_started", tool_call_id="read-1", name="read_file", arguments={}))
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
            "id": "call-write-1", "name": "write_file",
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
            assert all(block.exit_code == 1 for block in tools)


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
    assert "Failed (exit 1)" in _plain(render_transcript(state, 100))

    state.load_transcript([TranscriptBlock(
        kind="tool", tool_call_id="new", name="execute", content="failed",
        is_error=True, status="error", exit_code=9,
    )])
    assert "Failed (exit 9)" in _plain(render_transcript(state, 100))


def test_restored_interrupted_write_has_static_gray_status() -> None:
    state = CliState()
    state.load_transcript([TranscriptBlock(
        kind="tool", tool_call_id="write-1", name="write_file",
        arguments={"file_path": "/workspace/report.md", "content": "draft"},
        status="interrupted",
    )])
    block = state.blocks[0]
    assert isinstance(block, ToolBlock)
    title = _tool(block, False).renderable.renderables[0]
    assert title.plain.startswith("• write workspace/report.md")
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


