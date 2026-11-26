"""Golden snapshot tests locking the TUI rendering specification (docs/tui-rendering-spec.md).

These tests intentionally lock exact layout, indentation, tree glyphs, line numbers,
spacing, folding notices, ANSI colors, width/resize behavior, and live-vs-restored
parity. Do not update snapshots unless rendering changes were explicitly requested
and reflected in docs/tui-rendering-spec.md.
"""
from __future__ import annotations

import re

from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, ToolMessage
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
import pytest

from agent.cli import rendering
from agent.cli.app import CliApplication
from agent.cli.display import UiDisplayLimits
from agent.cli.previews import mcp_preview_title
from agent.cli.rendering import TranscriptRenderer, render_transcript
from agent.cli.state import CliState, MessageBlock, SubtaskView, ToolBlock
from agent.runtime.contracts import RunEvent
from agent.runtime.runner import AgentRunner
from agent.sessions.transcript import messages_to_transcript
from tests.cli_support import paint as _paint, plain as _plain
from tests.conftest import scripted_model


@pytest.fixture(autouse=True)
def _fixed_spinner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin Braille spinner to frame 4 ('⠼') for deterministic snapshots."""
    monkeypatch.setattr(rendering, "_spinner_tick", lambda: 4)
    monkeypatch.delenv("NO_COLOR", raising=False)


def _snapshot(
    state: CliState,
    width: int = 80,
    *,
    limits: UiDisplayLimits = UiDisplayLimits(),
) -> list[str]:
    renderer = TranscriptRenderer(limits=limits, show_header=False)
    fragments, _ = renderer.render(state, width)
    text = "".join(part for _, part in fragments)
    return [line.rstrip() for line in text.splitlines()]


def _ansi(
    state: CliState,
    width: int = 80,
    *,
    limits: UiDisplayLimits = UiDisplayLimits(),
) -> str:
    return render_transcript(state, width, limits=limits)


# ---------------------------------------------------------------------------
# 1. Generic statuses & tool title matrix
# ---------------------------------------------------------------------------


def test_golden_generic_statuses_and_ansi_markers() -> None:
    blocks = [
        ToolBlock("c-run", "execute", {"command": "npm run build"}, status="running"),
        ToolBlock("c-ok", "execute", {"command": "npm run build"}, output="ok", artifact={"exit_code": 0, "running": False}, status="completed"),
        ToolBlock("c-wait", "edit", {"file_path": "/workspace/app.py", "old_string": "a", "new_string": "b"}, status="waiting"),
        ToolBlock("c-fail-edit", "edit", {"file_path": "/workspace/app.py"}, output="Permission denied", status="error", is_error=True),
        ToolBlock("c-fail-cmd", "execute", {"command": "npm run build"}, output="boom", exit_code=1, artifact={"exit_code": 1, "running": False}, status="error", is_error=True),
        ToolBlock("c-timeout", "execute", {"command": "npm run build"}, output="", exit_code=124, artifact={"exit_code": 124, "termination_reason": "timeout", "running": False}, status="error", is_error=True),
        ToolBlock("c-cancel", "execute", {"command": "npm run build"}, output="", exit_code=130, artifact={"exit_code": 130, "termination_reason": "cancelled", "running": False}, status="error", is_error=True),
        ToolBlock("c-spawn", "execute", {"command": "npm run build"}, output="missing", exit_code=1, artifact={"exit_code": 1, "termination_reason": "spawn_error", "running": False}, status="error", is_error=True),
        ToolBlock("c-int", "execute", {"command": "npm run build"}, status="interrupted"),
    ]
    state = CliState(blocks=blocks)
    lines = _snapshot(state, 100)
    assert "⠼ Running npm run build" in lines
    assert "• Ran npm run build" in lines
    assert "• Editing /workspace/app.py — waiting for input" in lines
    assert "• Failed Editing /workspace/app.py" in lines
    assert "• Failed (exit 1) npm run build" in lines
    assert "• Timed out npm run build" in lines
    assert "• Cancelled npm run build" in lines
    assert "• Failed to start npm run build" in lines
    assert "• Running npm run build — interrupted (completion unconfirmed)" in lines

    colored = _ansi(state, 100)
    # Green spinner for running, green dot for success, yellow dot for waiting, #888888 dot for error/interrupted.
    assert "\x1b[32m⠼ \x1b[0m" in colored
    assert "\x1b[32m• \x1b[0m" in colored
    assert "\x1b[33m• \x1b[0m" in colored
    assert "\x1b[38;2;136;136;136m• \x1b[0m" in colored
    # Bold white title and dim status suffix.
    assert "\x1b[1mRan npm run build\x1b[0m" in colored
    assert "\x1b[2m — waiting for input\x1b[0m" in colored
    assert "\x1b[2m — interrupted (completion unconfirmed)\x1b[0m" in colored


def test_golden_native_and_management_tool_titles() -> None:
    blocks = [
        ToolBlock("d-run", "delete", {"file_path": "/workspace/temp.py"}, status="running"),
        ToolBlock("d-ok", "delete", {"file_path": "/workspace/temp.py"}, status="completed"),
        ToolBlock("d-err", "delete", {"file_path": "/workspace/temp.py"}, output="missing", status="error", is_error=True),
        ToolBlock("rt-run", "read_task", {"run_id": "run-7"}, status="running"),
        ToolBlock("rt-ok", "read_task", {"run_id": "run-7"}, status="completed"),
        ToolBlock("ct-run", "cancel_task", {"run_id": "run-7"}, status="running"),
        ToolBlock("ct-ok", "cancel_task", {"run_id": "run-7"}, status="completed"),
        ToolBlock("ml-run", "mcp_list", {}, status="running"),
        ToolBlock("ml-ok", "mcp_list", {}, status="completed"),
        ToolBlock("ma-run", "mcp_add", {"name": "docs"}, status="running"),
        ToolBlock("ma-ok", "mcp_add", {"name": "docs"}, status="completed"),
        ToolBlock("mr-run", "mcp_remove", {"name": "docs"}, status="running"),
        ToolBlock("mr-ok", "mcp_remove", {"name": "docs"}, status="completed"),
        ToolBlock("cmp-run", "compact_conversation", {}, status="running"),
        ToolBlock("cmp-ok", "compact_conversation", {}, status="completed"),
    ]
    assert _snapshot(CliState(blocks=blocks), 100) == [
        "",
        "⠼ Deleting /workspace/temp.py",
        "",
        "• Deleted /workspace/temp.py",
        "",
        "• Failed Deleting /workspace/temp.py",
        "  └ output",
        "    missing",
        "",
        "⠼ Reading Task run-7",
        "",
        "• Read Task run-7",
        "",
        "⠼ Cancelling Task run-7",
        "",
        "• Requested Task Cancellation run-7",
        "",
        "⠼ Listing MCP Servers",
        "",
        "• Listed MCP Servers",
        "",
        "⠼ Adding MCP Server docs",
        "",
        "• Added MCP Server docs",
        "",
        "⠼ Removing MCP Server docs",
        "",
        "• Removed MCP Server docs",
        "",
        "⠼ Compacting conversation",
        "",
        "• Compacted conversation",
    ]


# ---------------------------------------------------------------------------
# 2. Explore (single, multi, mixed status, collapsed vs expanded)
# ---------------------------------------------------------------------------


def test_golden_explore_single_multi_mixed_and_expanded() -> None:
    single = CliState(blocks=[ToolBlock("s1", "ls", {"path": "/workspace"}, status="completed")])
    assert _snapshot(single, 80) == [
        "",
        "• Explored 1 item",
        "  └ Listed /workspace",
        "  Ctrl+O to expand",
    ]

    running = CliState(blocks=[
        ToolBlock("e1", "ls", {"path": "/workspace"}, status="completed"),
        ToolBlock("e2", "read", {"file_path": "/workspace/config.py"}, status="completed"),
        ToolBlock("e3", "read", {"file_path": "/workspace/app.py"}, status="running"),
    ])
    assert _snapshot(running, 80) == [
        "",
        "⠼ Exploring 3 items",
        "  ├ Listed /workspace",
        "  ├ Read /workspace/config.py",
        "  └ Reading /workspace/app.py",
        "  Ctrl+O to expand",
    ]

    eight = CliState(blocks=[
        ToolBlock(f"r{i}", "read", {"file_path": f"/workspace/f{i}.py"}, output=f"body {i}", status="completed")
        for i in range(8)
    ])
    assert _snapshot(eight, 80) == [
        "",
        "• Explored 8 items",
        "  ├ … 3 more",
        "  ├ Read /workspace/f3.py",
        "  ├ Read /workspace/f4.py",
        "  ├ Read /workspace/f5.py",
        "  ├ Read /workspace/f6.py",
        "  └ Read /workspace/f7.py",
        "  Ctrl+O to expand",
    ]

    mixed = CliState(blocks=[
        ToolBlock("m1", "ls", {"path": "/workspace"}, status="completed"),
        ToolBlock("m2", "read", {"file_path": "/workspace/missing.py"}, output="File not found", status="error", is_error=True),
        ToolBlock("m3", "read", {"file_path": "/workspace/hold.py"}, status="interrupted"),
    ])
    assert _snapshot(mixed, 100) == [
        "",
        "• Explore 3 items · 1 failed · 1 interrupted",
        "  ├ Listed /workspace",
        "  ├ Failed Reading /workspace/missing.py",
        "  └ Reading /workspace/hold.py",
        "  ├ Failed Reading /workspace/missing.py: File not found",
        "  Ctrl+O to expand",
    ]

    expanded = CliState(blocks=running.blocks[:2], tools_expanded=True)
    expanded.blocks[0].output = "config.py\napp.py"
    expanded.blocks[1].output = "x = 1"
    assert _snapshot(expanded, 80) == [
        "",
        "• Listed /workspace",
        "  config.py",
        "  app.py",
        "",
        "• Read /workspace/config.py",
        "  x = 1",
    ]


# ---------------------------------------------------------------------------
# 3. Create / Edit / Overwrite (single, multi, empty, replace_all, highlights)
# ---------------------------------------------------------------------------


def test_golden_create_single_empty_multi_and_expanded() -> None:
    empty = CliState(blocks=[
        ToolBlock("c0", "write", {"file_path": "/workspace/empty.py", "content": ""}, artifact={"operation": "create"}, status="completed"),
    ])
    assert _snapshot(empty, 80) == [
        "",
        "• Created /workspace/empty.py",
    ]

    long_content = "import os\nimport sys\n\ndef main():\n    pass\n    return 0\n"
    single = CliState(blocks=[
        ToolBlock("c1", "write", {"file_path": "/workspace/app.py", "content": long_content}, artifact={"operation": "create"}, status="completed"),
    ])
    assert _snapshot(single, 80) == [
        "",
        "• Created /workspace/app.py (+6 -0)",
        "  └   1+ import os",
        "      2+ import sys",
        "      3+",
        "      4+ def main():",
        "      ⋮",
        "  … 2 changed lines hidden · Ctrl+R to review",
    ]

    single.diffs_expanded = True
    assert _snapshot(single, 80) == [
        "",
        "• Created /workspace/app.py (+6 -0)",
        "  └   1+ import os",
        "      2+ import sys",
        "      3+",
        "      4+ def main():",
        "      5+     pass",
        "      6+     return 0",
    ]

    multi = CliState(blocks=[
        ToolBlock("mc1", "write", {"file_path": "/workspace/a.py", "content": long_content}, artifact={"operation": "create"}, status="completed"),
        ToolBlock("mc2", "write", {"file_path": "/workspace/b.py", "content": "x = 1\n"}, artifact={"operation": "create"}, status="completed"),
    ])
    assert _snapshot(multi, 80) == [
        "",
        "• Created 2 files (+7 -0)",
        "  ├ /workspace/a.py (+6 -0)",
        "  │   1+ import os",
        "  │   2+ import sys",
        "  │   3+",
        "  │   4+ def main():",
        "  │   ⋮",
        "  └ /workspace/b.py (+1 -0)",
        "      1+ x = 1",
        "  … 2 changed lines hidden · Ctrl+R to review",
    ]


def test_golden_edit_single_consecutive_multi_and_replace_all() -> None:
    consecutive = CliState(blocks=[
        ToolBlock(
            "e1", "edit",
            {"file_path": "/workspace/config.py", "old_string": "timeout", "new_string": "headers"},
            artifact={"edit_start_lines": [475], "edit_occurrences": 1}, status="completed",
        ),
        ToolBlock(
            "e2", "edit",
            {"file_path": "/workspace/config.py", "old_string": "a", "new_string": "b\nc"},
            artifact={"edit_start_lines": [24], "edit_occurrences": 1}, status="completed",
        ),
    ])
    assert _snapshot(consecutive, 80) == [
        "",
        "• Edited /workspace/config.py (+3 -2)",
        "  └  24+ b",
        "     24- a",
        "     25+ c",
        "    475+ headers",
        "      ⋮",
        "  … 1 changed lines hidden · Ctrl+R to review",
    ]

    multi_repeated = CliState(blocks=[
        ToolBlock(
            "m1", "edit",
            {"file_path": "/workspace/config.py", "old_string": "old1", "new_string": "new1"},
            artifact={"edit_start_lines": [10], "edit_occurrences": 1}, status="completed",
        ),
        ToolBlock(
            "m2", "edit",
            {"file_path": "/workspace/client.py", "old_string": "timeout=10", "new_string": "timeout=30"},
            artifact={"edit_start_lines": [56], "edit_occurrences": 1}, status="completed",
        ),
        ToolBlock(
            "m3", "edit",
            {"file_path": "/workspace/config.py", "old_string": "old2", "new_string": "new2"},
            artifact={"edit_start_lines": [30], "edit_occurrences": 1}, status="completed",
        ),
    ])
    assert _snapshot(multi_repeated, 80) == [
        "",
        "• Edited 2 files (+3 -3)",
        "  ├ /workspace/config.py (+2 -2)",
        "  │  10+ new1",
        "  │  10- old1",
        "  │  30+ new2",
        "  │  30- old2",
        "  └ /workspace/client.py (+1 -1)",
        "     56+ timeout=30",
        "     56- timeout=10",
    ]

    replace_all = CliState(blocks=[
        ToolBlock(
            "ra1", "edit",
            {"file_path": "/workspace/app.py", "old_string": "old", "new_string": "old\nnew"},
            artifact={"edit_start_lines": [2, 4], "edit_occurrences": 2}, status="completed",
        ),
    ])
    assert _snapshot(replace_all, 80) == [
        "",
        "• Edited /workspace/app.py (+2 -0)",
        "  └   3+ new",
        "      6+ new",
    ]


def test_golden_overwrite_single_and_multi() -> None:
    single = CliState(blocks=[
        ToolBlock(
            "ow1", "write",
            {"file_path": "/workspace/config.yaml", "content": "version: 2\nenabled: true\ntimeout: 120\nretries: 3\nextra: 1\n"},
            artifact={"operation": "overwrite"}, status="completed",
        ),
    ])
    assert _snapshot(single, 80) == [
        "",
        "• Wrote /workspace/config.yaml (+5 -0)",
        "  └   1+ version: 2",
        "      2+ enabled: true",
        "      3+ timeout: 120",
        "      4+ retries: 3",
        "      ⋮",
        "  … 1 changed lines hidden · Ctrl+R to review",
    ]

    multi = CliState(blocks=[
        ToolBlock("ow2", "write", {"file_path": "/workspace/a.yaml", "content": "a: 1\n"}, artifact={"operation": "overwrite"}, status="completed"),
        ToolBlock("ow3", "write", {"file_path": "/workspace/b.yaml", "content": "b: 2\n"}, artifact={"operation": "overwrite"}, status="completed"),
    ])
    assert _snapshot(multi, 80) == [
        "",
        "• Wrote 2 files (+2 -0)",
        "  ├ /workspace/a.yaml (+1 -0)",
        "  │   1+ a: 1",
        "  └ /workspace/b.yaml (+1 -0)",
        "      1+ b: 2",
    ]


def test_golden_diff_ansi_colors_and_tree_isolation() -> None:
    state = CliState(blocks=[
        ToolBlock(
            "hi1", "edit",
            {"file_path": "/workspace/demo.py", "old_string": "def f():\n    return 1", "new_string": "def f():\n    return 2"},
            artifact={"edit_start_lines": [10], "edit_occurrences": 1}, status="completed",
        ),
        ToolBlock(
            "hi2", "edit",
            {"file_path": "/workspace/other.py", "old_string": "x = 1", "new_string": "x = 2"},
            artifact={"edit_start_lines": [20], "edit_occurrences": 1}, status="completed",
        ),
    ])
    colored = _ansi(state, 90)
    # Stat colors (+2 in #13a10e, -2 in #c50f1f).
    assert "\x1b[38;2;19;161;14m+2\x1b[0m" in colored
    assert "\x1b[38;2;197;15;31m-2\x1b[0m" in colored
    # Diff add/delete backgrounds (#005f00 / #5f0000).
    assert "48;2;0;95;0" in colored
    assert "48;2;95;0;0" in colored
    # Syntax foreground (Dark+ keyword #569CD6) overlaid onto diff bg.
    assert re.search(r"\x1b\[[0-9;]*38;2;86;156;214[0-9;]*48;2;(?:0;95;0|95;0;0)mreturn", colored)
    # Gray (dim) tree connectors NEVER receive diff add/delete background.
    assert "\x1b[2m  │\x1b[0m" in colored
    assert "\x1b[2m  ├ \x1b[0m" in colored
    assert "\x1b[2m  └ \x1b[0m" in colored
    assert not re.search(r"48;2;(?:0;95;0|95;0;0)m[^\x1b]*[├│└]", colored)


# ---------------------------------------------------------------------------
# 4. Execute & write_stdin (sync, yield/PTY, truncated log once, no extra card)
# ---------------------------------------------------------------------------


def test_golden_execute_sync_yield_pty_truncated_and_write_stdin_hidden() -> None:
    running = CliState(blocks=[
        ToolBlock(
            "ex1", "execute",
            {"command": "npm run dev", "tty": True},
            output="VITE ready in 321 ms\nLocal: http://localhost:5173/\n",
            artifact={"execution_id": "exec_123", "running": True},
            status="running",
        ),
    ])
    assert _snapshot(running, 80) == [
        "",
        "⠼ Running npm run dev · exec_123 · interactive",
        "  └ output",
        "    VITE ready in 321 ms",
        "    Local: http://localhost:5173/",
    ]

    truncated = CliState(blocks=[
        ToolBlock(
            "ex2", "execute",
            {"command": "pytest -q"},
            output="\n".join(f"row {i}" for i in range(6)) + "\n\n[Output truncated: full output saved to /workspace/.deep-agent/logs/exec/a.log]",
            artifact={
                "execution_id": "exec_2",
                "running": False,
                "exit_code": 0,
                "truncated": True,
                "agent_log_path": "/workspace/.deep-agent/logs/exec/a.log",
            },
            status="completed",
        ),
    ])
    assert _snapshot(truncated, 100) == [
        "",
        "• Ran pytest -q",
        "  └ output",
        "    … 3 earlier output lines hidden · Ctrl+O to expand",
        "    row 3",
        "    row 4",
        "    row 5",
        "  Output truncated · full output: /workspace/.deep-agent/logs/exec/a.log",
    ]

    # write_stdin never creates its own card; updates belong to the owning execute card.
    live = CliState(session_id="s1")
    live.apply(RunEvent(type="tool_started", tool_call_id="call-ex", name="execute", arguments={"command": "cat", "interactive": True}))
    live.apply(RunEvent(type="tool_completed", tool_call_id="call-ex", name="execute", content="", artifact={"execution_id": "exec_9", "running": True, "output": "hi\n"}))
    live.apply(RunEvent(type="tool_started", tool_call_id="call-ws", name="write_stdin", arguments={"execution_id": "exec_9", "chars": "bye\n"}))
    live.apply(RunEvent(type="tool_output_delta", tool_call_id="call-ex", name="execute", content="bye\n", stream="merged", result={"execution_id": "exec_9", "thread_id": "s1"}))
    live.apply(RunEvent(type="tool_completed", tool_call_id="call-ws", name="write_stdin", content="bye\n", artifact={"execution_id": "exec_9", "running": True}))
    assert len(live.blocks) == 1
    assert _snapshot(live, 80) == [
        "",
        "⠼ Running cat · exec_9 · interactive",
        "  └ output",
        "    hi",
        "    bye",
    ]


# ---------------------------------------------------------------------------
# 5. Web, MCP, SubAgent, Human Input
# ---------------------------------------------------------------------------


def test_golden_web_and_mcp_cards() -> None:
    web = CliState(blocks=[
        ToolBlock("ws", "web_search", {"query": "LangGraph latest version"}, output="result body", artifact={"results": [{}] * 10, "response_time": 0.8}, status="completed"),
        ToolBlock("wf", "web_fetch", {"url": "https://example.com/docs"}, output="page body", artifact={"title": "Documentation"}, status="completed"),
    ])
    assert _snapshot(web, 80) == [
        "",
        '• Searched Web "LangGraph latest version"',
        "  … 10 results · 0.8s · Ctrl+O to expand",
        "",
        "• Fetched https://example.com/docs",
        "  … Documentation · Ctrl+O to expand",
    ]

    mcp_running = ToolBlock(
        "m-run", "mcp__parallel__web_search", {"query": "LangGraph"},
        artifact={"mcp_server_name": "Parallel", "mcp_tool_name": "web_search"}, status="running",
    )
    mcp_done = ToolBlock(
        "m-done", "mcp__parallel__web_search", {"query": "LangGraph"},
        output="\n".join(f"item {i}" for i in range(5)),
        artifact={"mcp_server_name": "Parallel", "mcp_tool_name": "web_search"}, status="completed",
    )
    mcp_no_server = ToolBlock(
        "m-nosrv", "mcp__x__ping", {}, artifact={"mcp_tool_name": "ping"}, status="completed",
    )
    mcp_raw = ToolBlock("m-raw", "mcp__x__raw", {}, status="completed")
    assert mcp_preview_title("mcp__parallel__web_search", mcp_done.artifact) == "MCP · Parallel · web_search"
    assert mcp_preview_title("mcp__x__ping", mcp_no_server.artifact) == "MCP · ping"
    assert mcp_preview_title("mcp__x__raw", None) == "mcp__x__raw"

    assert _snapshot(CliState(blocks=[mcp_running, mcp_done, mcp_no_server, mcp_raw]), 80) == [
        "",
        "⠼ MCP · Parallel · web_search",
        "  query: LangGraph",
        "",
        "• MCP · Parallel · web_search",
        "  query: LangGraph",
        "  … 2 output lines hidden · Ctrl+O to expand",
        "  item 2",
        "  item 3",
        "  item 4",
        "",
        "• MCP · ping",
        "",
        "• mcp__x__raw",
    ]

    assert _snapshot(CliState(blocks=[mcp_done], tools_expanded=True), 80) == [
        "",
        "• MCP · Parallel · web_search",
        "  Host (outside sandbox)",
        "    query: LangGraph",
        "  item 0",
        "  item 1",
        "  item 2",
        "  item 3",
        "  item 4",
    ]


def test_golden_subagent_nested_background_and_ctrl_a_expand() -> None:
    nested = ToolBlock(
        "t1", "start_task", {"description": "Review execution architecture"},
        status="running",
        subtasks=(
            SubtaskView("sub-1", "Review sandbox lifecycle", "completed", result="Found duplicated state projections."),
            SubtaskView("sub-2", "Review permissions", "running"),
        ),
    )
    bg = ToolBlock(
        "t2", "start_task", {"description": "Review configuration", "run_in_background": True},
        output="\n".join(f"finding {i}" for i in range(6)),
        status="completed",
    )
    state = CliState(blocks=[nested, bg])
    assert _snapshot(state, 100) == [
        "",
        "⠼ Running SubAgent · Review execution architecture",
        "  ├ • SubAgent · Review sandbox lifecycle",
        "  │   Found duplicated state projections.",
        "  └ ⠼ Running SubAgent · Review permissions",
        "",
        "• SubAgent · Review configuration · background",
        "  … 3 output lines hidden · Ctrl+A to expand",
        "  finding 3",
        "  finding 4",
        "  finding 5",
    ]

    state.tasks_expanded = True
    assert _snapshot(state, 100) == [
        "",
        "⠼ Running SubAgent · Review execution architecture",
        "  ├ • SubAgent · Review sandbox lifecycle",
        "  │   Found duplicated state projections.",
        "  └ ⠼ Running SubAgent · Review permissions",
        "",
        "• SubAgent · Review configuration · background",
        "  finding 0",
        "  finding 1",
        "  finding 2",
        "  finding 3",
        "  finding 4",
        "  finding 5",
    ]


def test_golden_human_input_plain_and_secret_ref_only() -> None:
    blocks = [
        ToolBlock("h1", "request_human_input", {"question": "Which database should I use?"}, status="waiting"),
        ToolBlock("h2", "request_human_input", {"question": "Which database should I use?"}, output="PostgreSQL", status="completed"),
        ToolBlock("h3", "request_human_input", {"question": "Provide the API credential"}, output="API_KEY", status="completed"),
    ]
    assert _snapshot(CliState(blocks=blocks), 80) == [
        "",
        "• Asking Which database should I use?",
        "",
        "• Asked Which database should I use?",
        "  PostgreSQL",
        "",
        "• Asked Provide the API credential",
        "  API_KEY",
    ]


# ---------------------------------------------------------------------------
# 6. Widths, Unicode wide characters, long paths, and history restore parity
# ---------------------------------------------------------------------------


def test_golden_unicode_long_path_and_narrow_width() -> None:
    long_path = "/workspace/非常长的目录名称/sub_directory_alpha/beta_module/implementation_file.py"
    block = ToolBlock(
        "u1", "edit",
        {"file_path": long_path, "old_string": "旧值 = 1", "new_string": "新值 = 2"},
        artifact={"edit_start_lines": [12], "edit_occurrences": 1}, status="completed",
    )
    narrow = _snapshot(CliState(blocks=[block]), 42)
    # Path is preserved in full (wrapped across lines, never ellipsized).
    joined = "".join(line.strip() for line in narrow if "12+" not in line and "12-" not in line)
    assert long_path in joined
    assert "  └  12+ 新值 = 2" in narrow
    assert "     12- 旧值 = 1" in narrow


def test_golden_history_restore_matches_live_rendering() -> None:
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"id": "c-ed", "name": "edit", "args": {"file_path": "/workspace/app.py", "old_string": "a", "new_string": "b"}},
                {"id": "c-ex", "name": "execute", "args": {"command": "echo hi", "interactive": True}},
            ],
        ),
        ToolMessage(content="Edited", tool_call_id="c-ed", name="edit", artifact={"edit_start_lines": [5], "edit_occurrences": 1}),
        ToolMessage(content="part1\n", tool_call_id="c-ex", name="execute", artifact={"execution_id": "ex-1", "running": True, "output": "part1\n"}),
        AIMessage(content="", tool_calls=[{"id": "c-ws", "name": "write_stdin", "args": {"execution_id": "ex-1", "chars": ""}}]),
        ToolMessage(content="part2\n", tool_call_id="c-ws", name="write_stdin", artifact={"execution_id": "ex-1", "running": False, "exit_code": 0, "output": "part2\n"}),
    ]
    restored = CliState()
    restored.load_transcript(messages_to_transcript(messages))

    live = CliState(session_id="s-live")
    live.apply(RunEvent(type="tool_started", tool_call_id="c-ed", name="edit", arguments={"file_path": "/workspace/app.py", "old_string": "a", "new_string": "b"}))
    live.apply(RunEvent(type="tool_completed", tool_call_id="c-ed", name="edit", content="Edited", artifact={"edit_start_lines": [5], "edit_occurrences": 1}))
    live.apply(RunEvent(type="tool_started", tool_call_id="c-ex", name="execute", arguments={"command": "echo hi", "interactive": True}))
    live.apply(RunEvent(type="tool_completed", tool_call_id="c-ex", name="execute", content="part1\n", artifact={"execution_id": "ex-1", "running": True, "output": "part1\n"}))
    live.apply(RunEvent(type="tool_started", tool_call_id="c-ws", name="write_stdin", arguments={"execution_id": "ex-1", "chars": ""}))
    live.apply(RunEvent(type="tool_output_delta", tool_call_id="c-ex", name="execute", content="part2\n", stream="execution_finished", result={"execution_id": "ex-1", "running": False, "exit_code": 0, "thread_id": "s-live"}))
    live.apply(RunEvent(type="tool_completed", tool_call_id="c-ws", name="write_stdin", content="part2\n", artifact={"execution_id": "ex-1", "running": False, "exit_code": 0}))

    assert _snapshot(restored, 90) == _snapshot(live, 90) == [
        "",
        "• Edited /workspace/app.py (+1 -1)",
        "  └   5+ b",
        "      5- a",
        "",
        "• Ran echo hi",
        "  └ output",
        "    part1",
        "    part2",
    ]


# ---------------------------------------------------------------------------
# 7. Queue & layout vertical order snapshot
# ---------------------------------------------------------------------------


def test_golden_queue_layout_vertical_order_across_lifecycle() -> None:
    import asyncio

    async def driver() -> None:
        runner = AgentRunner(
            model=scripted_model([AIMessage(content="unused")]),
            backend=StateBackend(),
            thread_id="golden-queue-order",
        )
        with create_pipe_input() as pipe:
            output = DummyOutput()
            output.get_size = lambda: Size(rows=24, columns=80)  # type: ignore[method-assign]
            app = CliApplication(runner, input=pipe, output=output)
            app.state.blocks.append(MessageBlock(kind="user", content="hello"))
            app.run_events._apply_event(RunEvent(type="run_started"))
            app.runner.control.steer("queued step 1")
            _paint(app)

            windows = {w.content: w for w in app.application.layout.find_all_windows()}
            pos = app.application.renderer._last_screen.visible_windows_to_write_positions
            t_pos = pos[app.transcript_window]
            q_pos = pos[windows[app.queue_control]]
            s_pos = pos[windows[app.status_control]]
            e_pos = pos[windows[app.editor_control]]
            f_pos = pos[windows[app.footer_control]]
            # Exact vertical ordering: Transcript < Queue < Working Status < Editor < Footer.
            assert t_pos.ypos + t_pos.height <= q_pos.ypos
            assert q_pos.ypos + q_pos.height == s_pos.ypos
            assert s_pos.ypos < e_pos.ypos < f_pos.ypos
            runner.close()

    asyncio.run(driver())


# ---------------------------------------------------------------------------
# 8. Phase 2 boundary snapshots: >4-digit alignment, empty overwrite,
#    summary-only edit without metadata, and short-terminal queue scrolling
# ---------------------------------------------------------------------------


def test_golden_long_line_numbers_align_across_single_multi_collapsed_and_expanded() -> None:
    single = CliState(blocks=[
        ToolBlock(
            "big-1", "edit",
            {"file_path": "/workspace/huge.py", "old_string": "a\nb\nc", "new_string": "A\nB\nC"},
            artifact={"edit_start_lines": [12345], "edit_occurrences": 1}, status="completed",
        ),
        ToolBlock(
            "big-0", "edit",
            {"file_path": "/workspace/huge.py", "old_string": "x", "new_string": "X"},
            artifact={"edit_start_lines": [99], "edit_occurrences": 1}, status="completed",
        ),
    ])
    # Max line is 12347 (5 digits) -> unified gutter = 6 (1 space after └/│ + 5 digits).
    assert _snapshot(single, 80) == [
        "",
        "• Edited /workspace/huge.py (+4 -4)",
        "  └    99+ X",
        "       99- x",
        "    12345+ A",
        "    12345- a",
        "        ⋮",
        "  … 4 changed lines hidden · Ctrl+R to review",
    ]

    single.diffs_expanded = True
    assert _snapshot(single, 80) == [
        "",
        "• Edited /workspace/huge.py (+4 -4)",
        "  └    99+ X",
        "       99- x",
        "    12345+ A",
        "    12345- a",
        "    12346+ B",
        "    12346- b",
        "    12347+ C",
        "    12347- c",
    ]

    multi = CliState(blocks=[
        ToolBlock(
            "m-big", "edit",
            {"file_path": "/workspace/huge.py", "old_string": "a\nb\nc", "new_string": "A\nB\nC"},
            artifact={"edit_start_lines": [12345], "edit_occurrences": 1}, status="completed",
        ),
        ToolBlock(
            "m-small", "edit",
            {"file_path": "/workspace/small.py", "old_string": "k", "new_string": "K"},
            artifact={"edit_start_lines": [7], "edit_occurrences": 1}, status="completed",
        ),
    ])
    assert _snapshot(multi, 80) == [
        "",
        "• Edited 2 files (+4 -4)",
        "  ├ /workspace/huge.py (+3 -3)",
        "  │ 12345+ A",
        "  │ 12345- a",
        "  │ 12346+ B",
        "  │ 12346- b",
        "  │     ⋮",
        "  └ /workspace/small.py (+1 -1)",
        "        7+ K",
        "        7- k",
        "  … 2 changed lines hidden · Ctrl+R to review",
    ]


def test_golden_overwrite_final_empty_hides_stale_previous_content() -> None:
    same_path = CliState(blocks=[
        ToolBlock("ow-1", "write", {"file_path": "/workspace/notes.txt", "content": "old line 1\nold line 2\n"}, artifact={"operation": "overwrite"}, status="completed"),
        ToolBlock("ow-2", "write", {"file_path": "/workspace/notes.txt", "content": ""}, artifact={"operation": "overwrite"}, status="completed"),
    ])
    assert _snapshot(same_path, 80) == [
        "",
        "• Wrote /workspace/notes.txt",
    ]

    multi = CliState(blocks=[
        ToolBlock("ow-a1", "write", {"file_path": "/workspace/a.txt", "content": "stale\n"}, artifact={"operation": "overwrite"}, status="completed"),
        ToolBlock("ow-b1", "write", {"file_path": "/workspace/b.txt", "content": "keep\n"}, artifact={"operation": "overwrite"}, status="completed"),
        ToolBlock("ow-a2", "write", {"file_path": "/workspace/a.txt", "content": ""}, artifact={"operation": "overwrite"}, status="completed"),
    ])
    assert _snapshot(multi, 80) == [
        "",
        "• Wrote 2 files (+1 -0)",
        "  ├ /workspace/a.txt",
        "  └ /workspace/b.txt (+1 -0)",
        "      1+ keep",
    ]


def test_golden_edit_without_valid_position_metadata_renders_summary_only() -> None:
    blocks = [
        ToolBlock("ed-run", "edit", {"file_path": "/workspace/app.py", "old_string": "a", "new_string": "b"}, status="running"),
        ToolBlock("ed-wait", "edit", {"file_path": "/workspace/app.py", "old_string": "a", "new_string": "b"}, status="waiting"),
        ToolBlock("ed-nometa", "edit", {"file_path": "/workspace/legacy.py", "old_string": "a", "new_string": "b"}, status="completed"),
        ToolBlock("ed-oldkey", "edit", {"file_path": "/workspace/scalar.py", "old_string": "a", "new_string": "b"}, artifact={"edit_start_line": 7}, status="completed"),
    ]
    assert _snapshot(CliState(blocks=blocks), 100) == [
        "",
        "⠼ Editing /workspace/app.py",
        "",
        "• Editing /workspace/app.py — waiting for input",
        "",
        "• Edited 2 files",
        "  ├ /workspace/legacy.py",
        "  └ /workspace/scalar.py",
    ]


def test_golden_short_terminal_queue_scrolls_to_bottom_and_updates_on_resize() -> None:
    import asyncio

    async def driver() -> None:
        runner = AgentRunner(
            model=scripted_model([AIMessage(content="unused")]),
            backend=StateBackend(),
            thread_id="golden-queue-scroll",
        )
        with create_pipe_input() as pipe:
            size = [Size(rows=12, columns=60)]
            output = DummyOutput()
            output.get_size = lambda: size[0]  # type: ignore[method-assign]
            app = CliApplication(runner, input=pipe, output=output)
            app.state.blocks.append(MessageBlock(kind="user", content="hello"))
            app.buffer.text = "line1\nline2"
            app.run_events._apply_event(RunEvent(type="run_started"))
            for i in range(8):
                app.runner.control.steer(f"queued-item-{i}")
            _paint(app)

            total = app._queue_preview_cache[2]
            preferred = app._queue_height().preferred
            actual_height = app.queue_window.render_info.window_height
            assert actual_height >= 1
            assert actual_height < preferred < total
            # Scroll down far enough to reach the actual bottom row even when compressed below preferred.
            app._scroll_queue(100)
            _paint(app)
            expected_max = max(0, total - actual_height)
            assert app._queue_scroll == expected_max
            assert app.queue_window.vertical_scroll == expected_max

            screen = app.application.renderer._last_screen
            q_pos = screen.visible_windows_to_write_positions[app.queue_window]
            visible_queue = "\n".join(
                "".join(screen.data_buffer[y][x].char for x in range(q_pos.xpos, q_pos.xpos + q_pos.width)).rstrip()
                for y in range(q_pos.ypos, q_pos.ypos + q_pos.height)
            )
            assert "queued-item-7" in visible_queue

            # Grow terminal: scroll offset clamps cleanly to the new larger viewport.
            size[0] = Size(rows=30, columns=60)
            _paint(app)
            grown_height = app.queue_window.render_info.window_height
            assert grown_height > actual_height
            assert app._queue_scroll == max(0, total - grown_height)
            assert app.queue_window.vertical_scroll == app._queue_scroll
            runner.close()

    asyncio.run(driver())

