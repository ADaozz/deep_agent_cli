"""Configured folding limits must reach both the cached renderer and builders."""
from dataclasses import replace
import re

from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.data_structures import Size
import pytest

from agent.cli.app import CliApplication
from agent.cli.previews import build_failure_preview, build_tool_preview
from agent.cli.rendering import TranscriptRenderer, _capture, _message, render_transcript
from agent.cli.state import CliState, MessageBlock, ToolBlock
from agent.config import Settings
from agent.runner import AgentRunner, RunEvent
from tests.conftest import scripted_model


def plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def limits(**values):
    return Settings.from_mapping({"ui": values}).ui_display_limits


def test_thinking_title_hint_indent_and_expand() -> None:
    block = MessageBlock(kind="assistant", thinking="\n".join(f"思考 {i}" for i in range(41)))
    rows = plain(_capture(_message(block, True)[0], 100)).splitlines()
    title = next(i for i, row in enumerate(rows) if row.strip() == "Thinking")
    assert rows[title + 1].strip() == "… 36 earlier thinking lines hidden · Ctrl+T to expand"
    assert [row.strip() for row in rows[title + 2:]] == [f"思考 {i}" for i in range(36, 41)]
    title_indent = len(rows[title]) - len(rows[title].lstrip())
    assert all(len(row) - len(row.lstrip()) == title_indent + 2 for row in rows[title + 1:])
    expanded = plain(_capture(_message(block, False)[0], 100))
    assert expanded.count("Thinking") == 1
    assert "hidden" not in expanded
    assert "思考 0" in expanded
    assert "… 36 earlier" in plain(_capture(_message(block, True)[0], 100))
    assert "Thinking" not in plain(render_transcript(CliState(blocks=[MessageBlock(kind="assistant", content="回答")]), 80))


def test_thinking_limit_counts_wrapped_rows_and_short_blocks_have_no_hint() -> None:
    configured = limits(thinking_tail_lines=3)
    short = MessageBlock(kind="assistant", thinking="一\n二\n三")
    text = plain(_capture(_message(short, True, configured)[0], 80))
    assert "Thinking" in text and "hidden" not in text
    long = MessageBlock(kind="assistant", thinking="首行\n" + "很长的思考正文" * 20)
    text = plain(_capture(_message(long, True, limits(thinking_tail_lines=1))[0], 30))
    assert "Thinking" in text
    # 中文长行折成显示行，隐藏提示本身保持单行。
    assert "… 11 earlier thinking" in text
    rows = text.splitlines()
    title = next(i for i, row in enumerate(rows) if row.strip() == "Thinking")
    assert len(rows[title + 2:]) == 1
    assert "首行" not in text


@pytest.mark.parametrize("width", [20, 30, 80, 120])
@pytest.mark.parametrize("content", [
    "A single long paragraph with English words and sentences. " * 20,
    "中文长段落和 English 混排正文。" * 30,
    "旧段落\n\n" + "A paragraph that wraps across the terminal. " * 20 + "\n\n最后一行",
    "a" * 500 + "\n\t" + "有制表符的内容" * 20,
])
def test_collapsed_thinking_keeps_exact_last_five_screen_rows(width, content):
    """正文预算按显示行计算，缩进、空行和宽字符也占据实际宽度。"""
    block = MessageBlock(kind="assistant", thinking=content)
    expanded = plain(_capture(_message(block, False)[0], width)).splitlines()
    collapsed = plain(_capture(_message(block, True)[0], width)).splitlines()
    expanded_title = next(i for i, row in enumerate(expanded) if row.strip() == "Thinking")
    collapsed_title = next(i for i, row in enumerate(collapsed) if row.strip() == "Thinking")
    original_rows = expanded[expanded_title + 1:]
    kept_rows = collapsed[collapsed_title + 2:]
    assert len(kept_rows) == 5
    assert [row.rstrip() for row in kept_rows] == [row.rstrip() for row in original_rows[-5:]]
    assert f"… {len(original_rows) - 5} earlier" in collapsed[collapsed_title + 1]


def test_thinking_preview_reflows_after_resize_and_streaming():
    """流式追加与终端宽度变化都重新计算正文显示行。"""
    state = CliState()
    state.apply(RunEvent(type="thinking_delta", content="English paragraph that wraps. " * 40))
    renderer = TranscriptRenderer()
    for width in (80, 25, 100):
        fragments, _ = renderer.render(state, width)
        rows = "".join(value for _, value in fragments).splitlines()
        title = next(i for i, row in enumerate(rows) if row.strip() == "Thinking")
        assert len(rows[title + 2:]) == 5
    state.apply(RunEvent(type="thinking_delta", content=state.blocks[0].thinking + "\n这是后来追加的长段落。" * 30))
    fragments, _ = renderer.render(state, 25)
    rows = "".join(value for _, value in fragments).splitlines()
    title = next(i for i, row in enumerate(rows) if row.strip() == "Thinking")
    assert len(rows[title + 2:]) == 5
    state.thinking_collapsed = False
    fragments, _ = renderer.render(state, 25)
    assert "English" in "".join(value for _, value in fragments)


def test_cached_renderer_respects_config_and_cache_changes() -> None:
    state = CliState(blocks=[MessageBlock(kind="assistant", thinking="\n".join(f"idea {i}" for i in range(8)))])
    renderer = TranscriptRenderer(limits=limits(thinking_tail_lines=3))
    first, _ = renderer.render(state, 100)
    text = "".join(value for _, value in first)
    assert "5 earlier thinking lines hidden" in text
    assert "idea 4" not in text
    renderer._limits = replace(renderer._limits, thinking_tail_lines=2)
    updated, _ = renderer.render(state, 100)
    text = "".join(value for _, value in updated)
    assert "6 earlier thinking lines hidden" in text
    assert "idea 5" not in text


def test_command_and_generic_output_limits() -> None:
    output = "\n".join(f"output {i}" for i in range(6))
    configured = limits(execute_tail_lines=2, tool_tail_lines=2, expanded_tool_lines=3)
    command = ToolBlock("command", "execute", {"command": "test"}, output=output)
    generic = ToolBlock("generic", "custom_tool", {}, output=output)
    for block in (command, generic):
        state = CliState(blocks=[block])
        text = plain(render_transcript(state, 100, limits=configured))
        assert "output 3" not in text and "output 4" in text and "output 5" in text
        state.tools_expanded = True
        expanded = plain(render_transcript(state, 100, limits=configured))
        if block is command:
            assert "output 0" in expanded
        else:
            assert "output 2" not in expanded and "output 3" in expanded


def test_file_preview_and_create_list_limits() -> None:
    configured = limits(write_preview_lines=2, edit_preview_changed_lines=2, create_preview_items=2)
    write = ToolBlock("write", "write_file", {"file_path": "notes", "content": "一\n二\n三\n四"})
    preview = build_tool_preview(write, limits=configured)
    assert len([line for line in preview.lines if line.style == "add"]) == 2
    assert any("2 lines hidden" in line.text for line in preview.lines)
    edit = ToolBlock("edit", "edit_file", {
        "file_path": "notes", "old_string": "旧一\n旧二\n旧三", "new_string": "新一\n新二\n新三",
    }, status="completed")
    preview = build_tool_preview(edit, limits=configured)
    changed = [line for line in preview.lines if line.style in {"add", "delete"}]
    assert len(changed) == 2 and {line.style for line in changed} == {"add", "delete"}
    assert any("4 changed lines hidden" in line.text for line in preview.lines)
    edit_two = replace(edit, tool_call_id="edit-two")
    grouped = plain(render_transcript(CliState(blocks=[edit, edit_two]), 100, limits=configured))
    assert "10 changed lines hidden" in grouped
    creates = [ToolBlock(str(i), "write_file", {"file_path": f"file{i}"}, artifact={"operation": "create"}, status="completed") for i in range(5)]
    state = CliState(blocks=creates)
    text = plain(render_transcript(state, 100, limits=configured))
    assert "3 more" in text and "file2" not in text and "file3" in text and "file4" in text
    state.tools_expanded = True
    assert "file0" in plain(render_transcript(state, 100, limits=configured))


def test_exploration_and_failure_summary_limits() -> None:
    configured = limits(explore_preview_items=2, explore_failure_items=1, failure_preview_lines=2, command_failure_tail_lines=2)
    blocks = [ToolBlock(str(i), "read_file", {"file_path": f"file{i}"}, output="错误一\n错误二\n错误三", status="error", is_error=True) for i in range(4)]
    text = plain(render_transcript(CliState(blocks=blocks), 120, limits=configured))
    assert "3 more failed" in text and "file1" not in text and "file2" in text and "file3" in text
    preview = build_tool_preview(blocks[0], limits=configured)
    assert "错误三" not in "\n".join(line.text for line in preview.lines)
    command = ToolBlock("failed", "execute", {"command": "test"}, output="错误一\n错误二\n错误三", status="error", is_error=True)
    assert "错误一" not in "\n".join(line.text for line in build_failure_preview(command, limits=configured).lines)


def test_app_uses_configured_renderer_editor_and_menu() -> None:
    settings = Settings.from_mapping({"ui": {"thinking_tail_lines": 3, "editor_max_lines": 2, "completion_menu_lines": 4}})
    runner = AgentRunner(model=scripted_model([AIMessage(content="unused")]), backend=StateBackend(), settings=settings)
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.state.blocks.append(MessageBlock(kind="assistant", thinking="\n".join(f"idea {i}" for i in range(8))))
        fragments, _ = app._renderer.render(app.state, 100)
        assert "5 earlier thinking lines hidden" in "".join(value for _, value in fragments)
        app.buffer.text = "一\n二\n三\n四"
        editor = next(w for w in app.application.layout.find_all_windows() if w.content is app.editor_control)
        assert editor.height().max == 2
        menu = app.application.layout.container.floats[0].content
        assert menu.content.height.max == 4


@pytest.mark.parametrize("rows,configured_max,expected", [(6, 10, 2), (2, 10, 1), (30, 10, 10), (60, 10, 10), (30, 2, 2)])
def test_editor_height_respects_terminal_and_config_limits(monkeypatch, rows, configured_max, expected) -> None:
    settings = Settings.from_mapping({"ui": {"editor_max_lines": configured_max}})
    runner = AgentRunner(model=scripted_model([AIMessage(content="unused")]), backend=StateBackend(), settings=settings)
    with create_pipe_input() as pipe:
        output = DummyOutput()
        monkeypatch.setattr(output, "get_size", lambda: Size(rows=rows, columns=80))
        app = CliApplication(runner, input=pipe, output=output)
        app.buffer.text = "line\n" * 30
        editor = next(w for w in app.application.layout.find_all_windows() if w.content is app.editor_control)
        assert editor.height().max == expected
        assert editor.height().preferred == expected


@pytest.mark.parametrize("name,arguments,target", [
    ("write_file", {"file_path": "notes", "content": "one\ntwo\nthree"}, "write notes"),
    ("edit_file", {"file_path": "notes", "old_string": "old", "new_string": "new"}, "edit notes"),
    ("custom_tool", {}, "custom_tool"),
])
def test_failure_dispatch_preserves_tool_summary_and_configured_limit(name, arguments, target) -> None:
    block = ToolBlock("failed", name, arguments, output="error one\nerror two\nerror three", status="error", is_error=True)
    preview = build_tool_preview(block, limits=limits(failure_preview_lines=1))
    assert preview.kind == "failure"
    assert preview.target == target
    assert preview.lines[0].text == "error one"
    assert len(preview.lines) == 2
    assert "2 output lines hidden" in preview.lines[1].text
