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
from collections.abc import Callable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from agent.cli.gitinfo import GitSummary, parse_status
from agent.cli.interactions import InteractionController
from agent.cli.clipboard import ClipboardImage
from agent.cli.app import (
    FOOTER_LINES,
    CliApplication,
    format_context_usage,
    format_context_window,
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
from agent.config import Settings
from agent.config import ModelProfile
from agent.runner import AgentRunner, RunEvent, RunResult, TurnTiming
from agent.session import SessionStore, TranscriptBlock, messages_to_transcript
from agent.tools.examples import build_example_tools
from agent.factory import DEFAULT_FS_TOOLS, create_agent
from tests.conftest import scripted_model


_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _without_diff_number(text: str) -> str:
    return re.sub(r"^\s*\d+ (?=[+-])", "", text)


def _plain(rendered: str) -> str:
    """Strip SGR sequences so assertions survive Rich splitting a run mid-word."""
    return _ANSI_SGR.sub("", rendered)


async def _await_cli_io(awaitable, *, timeout: float = 5.0):  # type: ignore[no-untyped-def]
    """Drive the loop while testing CLI I/O without Application.run's refresh timer."""
    task = asyncio.ensure_future(awaitable)
    deadline = asyncio.get_running_loop().time() + timeout
    while not task.done() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    if not task.done():
        task.cancel()
        raise TimeoutError("CLI I/O did not finish")
    return await task


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
        body = app.application.layout.container.content
        assert not any(getattr(child, "style", None) == "class:editor-border" for child in body.children)
        editor = body.children[5].content
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
        hint = body.children[2].content
        divider = body.children[3].content
        editor = body.children[5].content
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
        divider = body.children[3]
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
        with app._capture_filesystem_warnings():
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
            assert item.plain.lstrip()[0] in ("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if status == "running" else "●")
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
        assert "● Inspect files" in rendered
        assert re.search(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏] Run tests", rendered)
    thread = runner.thread_id
    runner.close()
    store.close()
    reopened = SessionStore(tmp_path / "plan.sqlite3")
    resumed = AgentRunner(model=scripted_model([AIMessage(content="unused")]),
                          backend=StateBackend(), session_store=reopened)
    assert resumed.switch_session(thread).todos == updated_plan
    resumed.close()
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
        assert f"Explored {count} items" in rendered
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
    assert "● Failed (exit 7) git push origin main" in collapsed
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


def test_explore_group_keeps_last_five_and_surfaces_older_failures() -> None:
    blocks = [ToolBlock(str(index), "read_file", {"file_path": f"file-{index}.py"}, status="completed") for index in range(8)]
    blocks[0].status = "error"
    blocks[0].is_error = True
    blocks[0].output = "Error: permission denied"
    group = _explore_group(blocks, 100)
    rendered = _plain(_capture(group, 100))
    assert "Explored 8 items · 1 failed" in rendered
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
    assert "Explored 10 items · 10 failed" in rendered
    assert "… 7 more failed" in rendered
    assert rendered.count("Failed (exit 1)") == 3
    assert all(len(line) < 80 for line in rendered.splitlines())


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
    for index in range(6):
        state.apply(RunEvent(type="tool_output_delta", tool_call_id="stream", content=f"line {index}\n"))
    collapsed = _plain(render_transcript(state, 100))
    assert "execute pytest -q" in collapsed
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
    assert "line 0" in expanded and "line 7" in expanded
    assert "earlier output lines hidden" not in expanded

    state.tools_expanded = False
    collapsed = _plain(render_transcript(state, 100))
    assert "4 earlier output lines hidden" in collapsed
    assert "line 4" in collapsed and "line 7" in collapsed


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
    assert f"● {expected} pytest -q" in rendered
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
    assert title.plain.startswith("● ")


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
        for color in ("74;32;40", "36;92;56"):
            rows = re.findall(rf"\x1b\[[0-9;]*48;2;{color}m([^\n]*?)\x1b\[0m", rendered)
            if color == "36;92;56":
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
    green_rows = re.findall(r"\x1b\[[0-9;]*48;2;36;92;56m([^\n]*?)\x1b\[0m", rendered)
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
    for color in ("74;32;40", "36;92;56"):
        rows = re.findall(rf"\x1b\[[0-9;]*48;2;{color}m([^\n]*?)\x1b\[0m", rendered)
        assert rows and all(len(row) == 49 for row in rows)


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
    assert collapsed.count("Create 18 files") == 1
    assert "├ … 13 more" in collapsed
    assert "└ /workspace/file-17.md" in collapsed
    assert "/workspace/file-0.md" not in collapsed
    assert "Ctrl+O to expand" in collapsed

    state.tools_expanded = True
    expanded = _plain(render_transcript(state, 100))
    assert "Create 18 files" in expanded
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
    assert "● Create /workspace/report.md" in _plain(render_transcript(CliState(blocks=[created]), 100))
    assert "● Wrote /workspace/report.md" in _plain(render_transcript(CliState(blocks=[overwritten]), 100))
    assert "● write workspace/report.md" in _plain(render_transcript(CliState(blocks=[unknown]), 100))
    assert "● Edited /workspace/report.md" in _plain(render_transcript(CliState(blocks=[edited]), 100))


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
    assert "● Create /workspace/empty.md" in _plain(render_transcript(CliState(blocks=[created]), 100))


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
    assert "● Cancelled" in rendered
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
    assert title.plain.startswith("● write workspace/report.md")
    assert "interrupted (completion unconfirmed)" in title.plain
    assert title.spans[0].style == "#888888"


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


def _escape(app: CliApplication) -> None:
    for binding in app.bindings.bindings:
        if binding.keys == ("escape",):
            class Event:
                current_buffer = app.buffer

            binding.handler(Event())
            return
    raise AssertionError("Escape binding missing")


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

        for _ in range(3):
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


def test_streamed_transcript_follows_only_while_tail_is_visible() -> None:
    def scenario(app: CliApplication) -> None:
        assert not app.transcript_away_from_bottom()
        app._apply_event(RunEvent(type="assistant_delta", content="new answer\n" * 8))
        _paint(app)
        assert app.transcript_window.vertical_scroll == app.transcript_max_scroll()

        app.scroll_transcript(-6)
        _paint(app)
        pinned = app.transcript_window.vertical_scroll
        assert app.transcript_away_from_bottom()
        app._apply_event(RunEvent(type="assistant_delta", content="new answer\n" * 20))
        _paint(app)
        assert app.transcript_window.vertical_scroll == pinned

        app.follow_transcript()
        _paint(app)
        assert app._transcript_anchor is None
        assert app.transcript_window.vertical_scroll == app.transcript_max_scroll()
        assert not app.transcript_away_from_bottom()

        app.scroll_transcript(-5)
        _paint(app)
        assert app.transcript_offset(0) == 0
        assert app._transcript_anchor is None
        assert not app.transcript_away_from_bottom()

    _with_painted_app("stream-follow", scenario)


def test_back_to_bottom_hint_click_and_escape_priority() -> None:
    from prompt_toolkit.mouse_events import MouseEventType

    def scenario(app: CliApplication) -> None:
        app.state.running = True
        app.runner.control.begin_run()
        app.scroll_transcript(-5)
        _paint(app)
        assert app.transcript_away_from_bottom()
        handlers = app.application.renderer.mouse_handlers.mouse_handlers
        assert handlers[app.transcript_viewport_rows()][1] is not None

        _escape(app)
        _paint(app)
        assert app._transcript_anchor is None
        assert not app.runner.control.cancel_requested
        assert not app.transcript_away_from_bottom()

        app.scroll_transcript(-5)
        _paint(app)
        handlers = app.application.renderer.mouse_handlers.mouse_handlers
        click = MouseEvent(
            position=Point(x=1, y=app.transcript_viewport_rows()),
            event_type=MouseEventType.MOUSE_DOWN,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
        app.application.layout.update_parents_relations()
        with set_app(app.application):
            assert handlers[app.transcript_viewport_rows()][1](click) is None
        _paint(app)
        assert app._transcript_anchor is None
        assert app.transcript_window.vertical_scroll == app.transcript_max_scroll()

        _escape(app)
        assert app.runner.control.cancel_requested

    _with_painted_app("back-to-bottom", scenario)


def test_escape_cancels_slash_command_before_scrolling_or_cancelling_run() -> None:
    def scenario(app: CliApplication) -> None:
        app.state.running = True
        app.runner.control.begin_run()
        app.scroll_transcript(-5)
        app.buffer.text = "/model"
        _escape(app)
        assert app.buffer.text == ""
        assert app.transcript_away_from_bottom()
        assert not app.runner.control.cancel_requested
        assert app.application.ttimeoutlen <= 0.1
        assert app.application.timeoutlen <= 0.2

    _with_painted_app("slash-escape", scenario)


def test_escape_returns_to_bottom_before_cancelling_interaction() -> None:
    def scenario(app: CliApplication) -> None:
        app.interaction = InteractionController.approval([{
            "toolCallId": "call-write", "name": "write_file", "args": {},
        }])
        app._finish_interaction = lambda *, cancelled=False: cancelled_calls.append(cancelled)  # type: ignore[method-assign]
        app.scroll_transcript(-5)
        _paint(app)
        _escape(app)
        assert app._transcript_anchor is None
        assert cancelled_calls == []
        _escape(app)
        assert cancelled_calls == [True]

    cancelled_calls: list[bool] = []
    _with_painted_app("interaction-back-to-bottom", scenario)


def test_back_to_bottom_hint_sits_immediately_above_interaction_divider() -> None:
    def scenario(app: CliApplication) -> None:
        app.interaction = InteractionController.approval([{"toolCallId": "x", "name": "execute", "args": {}}])
        app.scroll_transcript(-5)
        _paint(app)
        screen = app.application.renderer._last_screen
        assert screen is not None
        columns = app.application.output.get_size().columns
        rows = [
            "".join(screen.data_buffer[y][x].char for x in range(columns))
            for y in range(app.application.output.get_size().rows)
        ]
        hint_row = next(y for y, row in enumerate(rows) if "↓ Back to bottom · esc" in row)
        divider_row = next(
            y for y, row in enumerate(rows)
            if "─" in row and "class:interaction-divider" in screen.data_buffer[y][0].style
        )
        assert divider_row == hint_row + 1

    _with_painted_app("hint-above-divider", scenario)


def test_transcript_uses_full_width_without_scrollbar_margin() -> None:
    def scenario(app: CliApplication) -> None:
        window = app.transcript_window
        info = window.render_info
        assert info is not None
        assert window.right_margins == []
        assert info.window_width == app.application.output.get_size().columns
        assert app._width() == info.window_width

    _with_painted_app("full-width-transcript", scenario)


def test_footer_puts_workspace_model_and_resume_id_on_first_line(monkeypatch) -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        thread_id="footer-model",
        settings=Settings(),
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        monkeypatch.setattr(app, "_width", lambda: 140)
        footer = "".join(fragment[1] for fragment in app._footer_text())
        lines = footer.splitlines()
        assert len(lines) == FOOTER_LINES
        assert lines[0].startswith(f" {app._workspace()} · qwen3.5-plus · footer-m")
        assert lines[0].rstrip().endswith("⎇ no git")
        assert "default ·" not in lines[0]
        assert "qwen3.5-plus" not in lines[1]
        assert "Ready" in lines[1]


def test_footer_uses_terminal_palette_for_workspace_resume_id_and_model(monkeypatch) -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        thread_id="footer-colors",
        settings=Settings(),
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        monkeypatch.setattr(app, "_width", lambda: 140)
        fragments = list(app._footer_text())
        first_line = "".join(value for _, value in fragments).splitlines()[0]
        assert first_line.startswith(f" {app._workspace()} · qwen3.5-plus · footer-c")
        assert first_line.rstrip().endswith("⎇ no git")
        assert any(style == "class:footer-workspace" and str(app._workspace()) in value for style, value in fragments)
        assert ("class:footer-resume-id", "footer-c") in fragments
        assert ("class:footer-model", "qwen3.5-plus") in fragments
        for label, color in (
            ("footer-workspace", "ansigreen"),
            ("footer-resume-id", "ansicyan"),
            ("footer-model", "ansiyellow"),
        ):
            assert app.application.style.get_attrs_for_style_str(f"class:{label}").color == color


def test_footer_keeps_the_right_side_in_narrow_terminal(monkeypatch) -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), thread_id="narrow-resume", settings=Settings(),
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        monkeypatch.setattr(app, "_width", lambda: 20)
        lines = "".join(value for _, value in app._footer_text()).splitlines()
        # The right side wins in a narrow terminal; the left side truncates.
        assert lines[0].rstrip().endswith("⎇ no git")
        assert "narrow-r" in lines[0]
        assert len(lines[0]) <= 20
        assert len(lines[1]) <= 20


def test_footer_reports_workspace_git_and_context_usage() -> None:
    settings = Settings.from_mapping({
        "llm": {
            "default": "local/metered",
            "models": {"local": {"models": {"metered": {"model": "qwen3.5-plus", "context_window": "1m"}}}},
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
        assert len(lines) == FOOTER_LINES
        assert lines[0].rstrip().endswith("⎇ main · 5 changed")
        assert lines[1].rstrip().endswith("1.0m Context · 3.9% used")


def test_footer_hides_context_meter_without_a_configured_window(monkeypatch) -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        thread_id="footer-unknown",
        settings=Settings(),
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        monkeypatch.setattr(app, "_width", lambda: 140)
        app.state.apply(RunEvent(type="usage", result={"total_tokens": 500}))
        lines = "".join(fragment[1] for fragment in app._footer_text()).splitlines()
        assert len(lines) == FOOTER_LINES
        assert lines[0].startswith(f" {app._workspace()} · qwen3.5-plus · footer-u")
        assert "Context" not in lines[1]


def test_footer_places_context_on_status_line_when_git_unavailable() -> None:
    settings = Settings.from_mapping({
        "llm": {
            "default": "token-plan/metered",
            "models": {"token-plan": {"models": {"metered": {"model": "auto", "context_window": "1m"}}}},
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
        assert len(lines) == FOOTER_LINES
        assert "auto · token-plan" in lines[0]
        assert lines[0].rstrip().endswith("⎇ no git")
        assert lines[1].rstrip().endswith("1.0m Context")


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
        settings=Settings.from_mapping({"llm": {"default": "local/test", "models": {"local": {"context_window": "128k", "models": {"test": {}}}}}}),
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
        settings=Settings.from_mapping({"llm": {"default": "local/test", "models": {"local": {"context_window": "128k", "models": {"test": {}}}}}}),
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
        settings=Settings.from_mapping({"llm": {"default": "local/test", "models": {"local": {"context_window": "128k", "models": {"test": {}}}}}}),
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
    assert "Explored 3 items" in _plain("".join(part[1] for part in fragments))

    for _ in range(3):
        renderer.render(state, 80)
    assert len(rendered) == 2

    state.blocks.append(explore(3, "read_file"))
    renderer.render(state, 80)
    assert len(rendered) == 3  # the group gained a member

    state.blocks.extend(explore(index, "read_file") for index in range(4, 7))
    fragments, _ = renderer.render(state, 80)
    text = _plain("".join(part[1] for part in fragments))
    assert "Explored 7 items" in text
    assert "… 2 more" in text
    assert "Read f6.py" in text
    assert "Read f0.py" not in text

    state.tools_expanded = True
    fragments, _ = renderer.render(state, 80)
    assert len(rendered) == 11  # cached header, seven tool units built
    expanded = _plain("".join(part[1] for part in fragments))
    assert "read f0.py" in expanded
    assert "read f6.py" in expanded
    assert "Ctrl+O to expand" not in expanded


def test_expanded_tool_cache_survives_collapse_and_invalidates_changed_tool(monkeypatch) -> None:
    import agent.cli.rendering as rendering

    original = rendering._render_unit
    rendered: list[int] = []

    def counting(renderable: object, width: int) -> object:
        rendered.append(width)
        return original(renderable, width)

    monkeypatch.setattr(rendering, "_render_unit", counting)
    state = CliState(blocks=[
        ToolBlock(str(index), "read_file", {"file_path": f"f{index}.py"}, output="old", status="completed")
        for index in range(6)
    ])
    renderer = TranscriptRenderer()
    state.tools_expanded = True
    first = renderer.render_document(state, 80)
    assert len(rendered) == 7  # header and six tool cards
    assert renderer.render_document(state, 80) is first

    state.tools_expanded = False
    renderer.render_document(state, 80)
    assert len(rendered) == 8  # one collapsed summary
    state.tools_expanded = True
    assert renderer.render_document(state, 80) is first
    assert len(rendered) == 8

    tool = state.blocks[0]
    assert isinstance(tool, ToolBlock)
    touch(tool).output = "new"
    changed = renderer.render_document(state, 80)
    assert changed is not first
    assert len(rendered) == 9  # only the changed card
    assert any(
        "new" in "".join(part[1] for part in changed.get_line(index))
        for index in range(changed.line_count)
    )


def test_expanded_document_splits_only_requested_tool_lines(monkeypatch) -> None:
    import agent.cli.rendering as rendering

    original = rendering.split_lines
    calls: list[int] = []

    def counting(fragments):  # type: ignore[no-untyped-def]
        calls.append(1)
        return original(fragments)

    monkeypatch.setattr(rendering, "split_lines", counting)
    state = CliState(
        blocks=[
            ToolBlock(str(index), "read_file", {"file_path": f"f{index}.py"}, output="one\ntwo", status="completed")
            for index in range(20)
        ],
        tools_expanded=True,
    )
    renderer = TranscriptRenderer()
    document = renderer.render_document(state, 80)
    assert calls == []
    assert "two" in "".join(part[1] for part in document.get_line(document.line_count - 1))
    assert len(calls) == 1
    document.get_line(document.line_count - 2)
    assert len(calls) == 1
    assert renderer.render_document(state, 80) is document
    assert len(calls) == 1


def test_transcript_renderer_matches_uncached_render() -> None:
    state = CliState()
    state.add_user("hello there")
    state.todos = [{"content": "Inspect files", "status": "completed"}]
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
