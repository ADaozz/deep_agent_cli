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


def test_golden_write_running_waiting_completed_and_failed() -> None:
    content = "import os\nimport sys\n\ndef main():\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c\n"
    args = {"file_path": "/workspace/app.py", "content": content}

    running = CliState(blocks=[ToolBlock("w-run", "write", args, status="running")])
    assert _snapshot(running, 80) == [
        "",
        "⠼ Writing workspace/app.py (+8 -0)",
        "     1+ import os",
        "     2+ import sys",
        "     3+",
        "     4+ def main():",
        "     ⋮",
        "  … 4 changed lines hidden · Ctrl+R to review",
    ]
    running_ansi = _ansi(running, 80)
    assert "\x1b[32m⠼ \x1b[0m" in running_ansi
    assert "48;2;0;95;0" in running_ansi

    running.diffs_expanded = True
    assert _snapshot(running, 80) == [
        "",
        "⠼ Writing workspace/app.py (+8 -0)",
        "     1+ import os",
        "     2+ import sys",
        "     3+",
        "     4+ def main():",
        "     5+     a = 1",
        "     6+     b = 2",
        "     7+     c = 3",
        "     8+     return a + b + c",
    ]

    waiting = CliState(blocks=[ToolBlock("w-wait", "write", args, status="waiting")])
    assert _snapshot(waiting, 80) == [
        "",
        "• Writing workspace/app.py (+8 -0) — waiting for input",
        "     1+ import os",
        "     2+ import sys",
        "     3+",
        "     4+ def main():",
        "     ⋮",
        "  … 4 changed lines hidden · Ctrl+R to review",
    ]
    assert "\x1b[33m• \x1b[0m" in _ansi(waiting, 80)

    created = CliState(blocks=[
        ToolBlock("w-create", "write", args, artifact={"operation": "create"}, status="completed"),
    ])
    assert _snapshot(created, 80) == [
        "",
        "• Created /workspace/app.py (+8 -0)",
        "  └   1+ import os",
        "      2+ import sys",
        "      3+",
        "      4+ def main():",
        "      ⋮",
        "  … 4 changed lines hidden · Ctrl+R to review",
    ]

    overwritten = CliState(blocks=[
        ToolBlock("w-over", "write", args, artifact={"operation": "overwrite"}, status="completed"),
    ])
    assert _snapshot(overwritten, 80) == [
        "",
        "• Wrote /workspace/app.py (+8 -0)",
        "  └   1+ import os",
        "      2+ import sys",
        "      3+",
        "      4+ def main():",
        "      ⋮",
        "  … 4 changed lines hidden · Ctrl+R to review",
    ]

    failed = CliState(blocks=[
        ToolBlock(
            "w-fail", "write", args,
            output="Permission denied: read-only path",
            status="error", is_error=True,
        ),
    ])
    assert _snapshot(failed, 80) == [
        "",
        "• Failed Writing workspace/app.py",
        "  └ output",
        "    Permission denied: read-only path",
    ]


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


# ---------------------------------------------------------------------------
# 9. Interaction Panels (docs/tui-rendering-spec.md §7):
#    Normal Human Input, Tool Approval, Secure Credential Input, Behaviors
# ---------------------------------------------------------------------------


def _interaction_lines(controller: Any, width: int = 80) -> list[str]:
    from agent.cli.rendering import render_interaction

    raw = render_interaction(controller, width)
    return [line.rstrip() for line in _plain(raw).splitlines()]


def test_golden_interaction_human_input_modes_dedup_recommendation_and_cjk() -> None:
    from agent.cli.interactions import InteractionController
    from agent.cli.rendering import render_interaction
    from agent.tools.human_input import build_human_input_tools

    # 1) Single-select with inline (recommended), concise rationale, Other:, cyan theme, no separate Recommendation block.
    single = InteractionController.human({
        "title": "Choose a database",
        "question": "Which database should the project use?",
        "recommendation": {"value": "pg", "reason": "Matches production schema"},
        "fields": [{
            "id": "db",
            "type": "single_select",
            "label": "Choose a database",  # duplicates title -> deduplicated
            "required": True,
            "options": [
                {"value": "pg", "label": "PostgreSQL"},
                {"value": "sqlite", "label": "SQLite"},
                {"value": "mysql", "label": "MySQL"},
            ],
        }],
    })
    assert _interaction_lines(single, 80) == [
        "Choose a database",
        "Which database should the project use?",
        "",
        "→ PostgreSQL (recommended)",
        "    Matches production schema",
        "  SQLite",
        "  MySQL",
        "  Other:",
        "",
        "↑↓ select · Enter confirm · Esc close",
    ]
    single_ansi = render_interaction(single, 80)
    assert "\x1b[1;96mChoose a database\x1b[0m" in single_ansi
    assert "\x1b[1;36m→ PostgreSQL (recommended)\x1b[0m" in single_ansi
    assert "Recommended:" not in _plain(single_ansi)

    # Selecting Other: opens custom entry with Esc back.
    single.option_index = 3
    assert single.accept() is False
    assert single.custom_entry is True
    assert _interaction_lines(single, 80) == [
        "Choose a database",
        "Which database should the project use?",
        "",
        "Other: enter your answer",
        "",
        "Enter submit · Ctrl+J newline · Esc back",
    ]

    # 2) Multi-select with [x]/[ ], Space toggle, Other:.
    multi = InteractionController.human({
        "title": "Select features",
        "question": "Which features should be enabled?",
        "fields": [{
            "id": "features",
            "type": "multi_select",
            "label": "Reply",
            "required": True,
            "options": [
                {"value": "search", "label": "Search"},
                {"value": "cache", "label": "Caching"},
            ],
        }],
    })
    multi.toggle()
    assert _interaction_lines(multi, 80) == [
        "Select features",
        "Which features should be enabled?",
        "",
        "→ [x] Search",
        "  [ ] Caching",
        "  [ ] Other:",
        "",
        "↑↓ select · Space toggle · Enter confirm · Esc close",
    ]

    # 3) Plain text input: no options, deduplicated title/question/reason, Chinese & long wrapping.
    plain_text = InteractionController.human({
        "title": "部署目标环境",
        "question": "部署目标环境？",  # duplicates title -> omitted
        "reason": "需要确认目标集群后再执行发布脚本，避免误操作影响线上流量。",
        "fields": [{"id": "env", "type": "text", "label": "回复", "required": True}],
    })
    plain_lines = _interaction_lines(plain_text, 44)
    assert plain_lines[0] == "部署目标环境"
    assert sum("部署目标环境" in line for line in plain_lines) == 1
    assert "Other:" not in "\n".join(plain_lines)
    assert plain_lines[-1] == "Enter submit · Ctrl+J newline · Esc close"

    # 4) Multi-field progress shows only current field + step progress.
    multi_field = InteractionController.human({
        "title": "Configure deployment",
        "question": "Provide deployment parameters.",
        "fields": [
            {"id": "region", "type": "single_select", "label": "Region", "required": True,
             "options": [{"value": "us", "label": "US East"}]},
            {"id": "tag", "type": "text", "label": "Release tag", "required": True},
        ],
    })
    assert _interaction_lines(multi_field, 80) == [
        "Configure deployment",
        "Provide deployment parameters.",
        "1/2  Region",
        "",
        "→ US East",
        "  Other:",
        "",
        "↑↓ select · Enter confirm · Esc close",
    ]
    assert multi_field.accept() is False
    assert _interaction_lines(multi_field, 80) == [
        "Configure deployment",
        "Provide deployment parameters.",
        "2/2  Release tag",
        "",
        "Enter submit · Ctrl+J newline · Shift+Tab back · Esc close",
    ]

    # 5) Tool description enforces objective option labels (forbids 1st/2nd person) & clear credential details.
    tool = build_human_input_tools()[0]
    assert "first- or second-person" in tool.description
    assert "secret" in tool.description
    assert "which service" in tool.description.lower()


def test_golden_interaction_tool_approval_all_categories_batch_and_colors() -> None:
    from agent.cli.interactions import InteractionController
    from agent.cli.rendering import render_interaction

    # 1) Execute (network ON + sandbox scope)
    exec_ctrl = InteractionController.approval(
        [{"toolCallId": "c1", "name": "execute", "args": {"command": "npm install", "network": True}}],
        execution_scope="Sandbox (restricted)",
    )
    assert _interaction_lines(exec_ctrl, 80) == [
        "Approval required",
        "",
        "Command:",
        "npm install",
        "",
        "Network access: internet, localhost and LAN",
        "Execution scope: Sandbox (restricted)",
        "",
        "→ Reject",
        "  Run",
        "",
        "↑↓ select · Enter confirm · Esc close · F2 reopen",
    ]
    exec_ansi = render_interaction(exec_ctrl, 80)
    assert "\x1b[1;33mApproval required\x1b[0m" in exec_ansi
    assert "\x1b[1;33m→ Reject\x1b[0m" in exec_ansi
    assert "Decision" not in _plain(exec_ansi)
    assert "Deny this tool call" not in _plain(exec_ansi)
    assert "Approve and execute" not in _plain(exec_ansi)
    # Default selection is Reject.
    assert exec_ctrl.option_index == 0

    # 2) Write / Edit / Delete
    write_ctrl = InteractionController.approval([{
        "toolCallId": "w1", "name": "write",
        "args": {"file_path": "/workspace/app.py", "content": "a = 1\nb = 2\n"},
    }])
    assert _interaction_lines(write_ctrl, 80) == [
        "Approval required",
        "",
        "Write file: /workspace/app.py",
        "Content: 2 lines (12 bytes)",
        "",
        "→ Reject",
        "  Run",
        "",
        "↑↓ select · Enter confirm · Esc close · F2 reopen",
    ]

    edit_ctrl = InteractionController.approval([{
        "toolCallId": "e1", "name": "edit",
        "args": {"file_path": "/workspace/app.py", "old_string": "a = 1", "new_string": "a = 2\nc = 3", "replace_all": True},
    }])
    assert "Edit file: /workspace/app.py" in _interaction_lines(edit_ctrl, 80)
    assert "Change: replace 1 line with 2 lines (all occurrences)" in _interaction_lines(edit_ctrl, 80)

    del_ctrl = InteractionController.approval([{
        "toolCallId": "d1", "name": "delete", "args": {"file_path": "/workspace/old.py"},
    }])
    assert "Delete file: /workspace/old.py" in _interaction_lines(del_ctrl, 80)

    # 3) Web search & fetch
    ws_ctrl = InteractionController.approval([{
        "toolCallId": "ws1", "name": "web_search", "args": {"query": "LangGraph interrupts"},
    }])
    assert _interaction_lines(ws_ctrl, 80)[2:4] == [
        'Web search: "LangGraph interrupts"',
        "Network access: DeepSeek API (host)",
    ]
    wf_ctrl = InteractionController.approval([{
        "toolCallId": "wf1", "name": "web_fetch", "args": {"url": "https://example.com/spec"},
    }])
    assert _interaction_lines(wf_ctrl, 80)[2:4] == [
        "Fetch URL: https://example.com/spec",
        "Network access: DeepSeek API (host)",
    ]

    # 4) External MCP (with red Host outside sandbox risk)
    mcp_ctrl = InteractionController.approval(
        [{"toolCallId": "m1", "name": "mcp__github__list_issues", "args": {"repo": "ADaozz/deep_agent_cli"}}],
        mcp_tool_lookup=lambda n: ("GitHub", "list_issues") if n == "mcp__github__list_issues" else None,
    )
    assert _interaction_lines(mcp_ctrl, 80) == [
        "Approval required",
        "",
        "MCP tool: GitHub · list_issues",
        "Execution: Host (outside sandbox)",
        "Arguments:",
        "  repo: ADaozz/deep_agent_cli",
        "",
        "→ Reject",
        "  Run",
        "",
        "↑↓ select · Enter confirm · Esc close · F2 reopen",
    ]
    assert "\x1b[31mExecution: Host (outside sandbox)\x1b[0m" in render_interaction(mcp_ctrl, 80)

    # 5) MCP add (stdio) & remove
    mcp_add_ctrl = InteractionController.approval([{
        "toolCallId": "ma1", "name": "mcp_add",
        "args": {"name": "localdb", "transport": "stdio", "command": "python3", "args": ["srv.py"], "auth_type": "none"},
    }])
    mcp_add_ansi = render_interaction(mcp_add_ctrl, 100)
    assert "Add MCP server: localdb" in _plain(mcp_add_ansi)
    assert "\x1b[31mHost command (runs outside the sandbox): python3 srv.py\x1b[0m" in mcp_add_ansi

    # 6) Write stdin (write / close / cancel)
    stdin_write = InteractionController.approval([{
        "toolCallId": "st1", "name": "write_stdin",
        "args": {"execution_id": "exec_9", "chars": "yes\n", "action": "write"},
    }])
    assert _interaction_lines(stdin_write, 80)[2:4] == [
        "Send stdin to process: exec_9",
        r"Input: yes\n",
    ]
    stdin_close = InteractionController.approval([{
        "toolCallId": "st2", "name": "write_stdin",
        "args": {"execution_id": "exec_9", "action": "close"},
    }])
    assert "Close stdin (EOF) for process: exec_9" in _interaction_lines(stdin_close, 80)
    stdin_cancel = InteractionController.approval([{
        "toolCallId": "st3", "name": "write_stdin",
        "args": {"execution_id": "exec_9", "action": "cancel"},
    }])
    assert "Cancel running process: exec_9" in _interaction_lines(stdin_cancel, 80)

    # 7) Batch approval preserves every action distinctly
    batch = InteractionController.approval(
        [
            {"toolCallId": "b1", "name": "write", "args": {"file_path": "/workspace/a.py", "content": "x = 1\n"}},
            {"toolCallId": "b2", "name": "execute", "args": {"command": "pytest -q"}},
        ],
        execution_scope="Sandbox (restricted)",
    )
    assert _interaction_lines(batch, 80) == [
        "Approval required (2 actions)",
        "",
        "1. Write file: /workspace/a.py",
        "   Content: 1 line (6 bytes)",
        "",
        "2. Command:",
        "   pytest -q",
        "   Network access: off (default)",
        "   Execution scope: Sandbox (restricted)",
        "",
        "→ Reject",
        "  Run",
        "",
        "↑↓ select · Enter confirm · Esc close · F2 reopen",
    ]


def test_golden_interaction_secure_credential_session_persistent_runtime_auth_and_overwrite() -> None:
    from agent.cli.interactions import InteractionController
    from agent.cli.rendering import render_interaction
    from agent.secrets.store import SecretMetadata
    from agent.tools.human_interaction import build_runtime_auth_request

    # 1) Session credential (sandbox scope): single masked input, yellow heading, zero options, red validation error.
    sess_ctrl = InteractionController.human({
        "interactionId": "cred-sess",
        "title": "API Token",
        "question": "Required to run API tests inside the sandbox.",
        "fields": [{
            "id": "tok",
            "type": "secret",
            "label": "API Token",
            "required": True,
            "secret_name": "TEST_API_TOKEN",
            "secret_scope": "sandbox",
        }],
    })
    assert _interaction_lines(sess_ctrl, 80) == [
        "Credential required",
        "",
        "API Token",
        "Required to run API tests inside the sandbox.",
        "",
        "Available only for this session (TEST_API_TOKEN · sandbox).",
        "The agent receives only the credential name.",
        "",
        "Enter submit · Esc close · F2 reopen",
    ]
    sess_ansi = render_interaction(sess_ctrl, 80)
    assert "\x1b[1;33mCredential required\x1b[0m" in sess_ansi
    assert "Other:" not in _plain(sess_ansi)
    assert "Secure Input" not in _plain(sess_ansi)
    assert "Reference:" not in _plain(sess_ansi)
    assert "Scope:" not in _plain(sess_ansi)
    assert "Storage:" not in _plain(sess_ansi)

    # Required validation error in red.
    assert sess_ctrl.accept_secret("") is False
    assert "\x1b[31mThis field is required.\x1b[0m" in render_interaction(sess_ctrl, 80)

    # 2) Persistent credential (host scope + trusted MCP consumer match) + separate confirmation step.
    pers_ctrl = InteractionController.human(
        {
            "interactionId": "cred-mcp",
            "question": "Required to connect the GitHub MCP server.",
            "fields": [{
                "id": "tok",
                "type": "secret",
                "label": "GitHub Personal Access Token",
                "required": True,
                "secret_name": "GITHUB_TOKEN",
                "secret_scope": "host",
            }],
        },
        secret_consumers={"GITHUB_TOKEN": ("github",)},
    )
    assert _interaction_lines(pers_ctrl, 90) == [
        "Credential required",
        "",
        "GitHub Personal Access Token",
        "Required to connect the GitHub MCP server.",
        "",
        "Stored encrypted on this device for future sessions (GITHUB_TOKEN · MCP: github).",
        "The agent receives only the credential name.",
        "",
        "Enter submit · Esc close · F2 reopen",
    ]
    raw_secret = "ghp_NEVER_ECHO_THIS_RAW_TOKEN_42"
    assert pers_ctrl.accept_secret(raw_secret) is False
    assert _interaction_lines(pers_ctrl, 90) == [
        "Confirm encrypted storage",
        "",
        "Store GITHUB_TOKEN encrypted on this device for MCP (github)?",
        "",
        "→ Cancel",
        "  Store encrypted on this device",
        "",
        "↑↓ select · Enter confirm · Shift+Tab back · Esc close · F2 reopen",
    ]
    assert raw_secret not in render_interaction(pers_ctrl, 90)
    assert raw_secret not in repr(pers_ctrl)
    # Explicit user confirmation needed to persist.
    pers_ctrl.move(1)
    assert pers_ctrl.accept() is True
    assert pers_ctrl.secret_submissions()["tok"].storage == "persistent"

    # 3) Host scope WITHOUT trusted MCP consumer -> does NOT blindly infer persistent; separate choice step.
    choose_ctrl = InteractionController.human({
        "interactionId": "cred-choose",
        "question": "Required for GitHub API access (please save persistently!).",  # model text must NOT decide policy
        "fields": [{
            "id": "tok",
            "type": "secret",
            "label": "GitHub Token",
            "required": True,
            "secret_name": "GITHUB_TOKEN",
            "secret_scope": "host",
        }],
    })
    assert "Choose storage in the next step (GITHUB_TOKEN · host)." in _interaction_lines(choose_ctrl, 100)
    assert choose_ctrl.accept_secret(raw_secret) is False
    assert _interaction_lines(choose_ctrl, 100) == [
        "Credential storage",
        "",
        "Choose how to store GITHUB_TOKEN (host). Global MCP requires encrypted storage on this device.",
        "",
        "→ This session only",
        "  Store encrypted on this device",
        "",
        "↑↓ select · Enter confirm · Shift+Tab back · Esc close · F2 reopen",
    ]

    # 4) Trusted runtime_auth
    auth_ctrl = InteractionController.human(build_runtime_auth_request(interaction_id="auth-golden"))
    assert _interaction_lines(auth_ctrl, 80) == [
        "Authentication required",
        "",
        "sudo password",
        "Required to complete the current privileged operation.",
        "",
        "Used for this authentication only. Not stored.",
        "",
        "Enter submit · Esc cancel",
    ]
    assert "\x1b[1;33mAuthentication required\x1b[0m" in render_interaction(auth_ctrl, 80)

    # 5) Overwrite existing credential warning in red, yellow heading & selection, default Cancel.
    sess_ctrl.accept_secret("new-val")
    sess_ctrl.prompt_overwrite_confirmation([
        SecretMetadata(name="TEST_API_TOKEN", scope="sandbox", storage="session"),
    ])
    assert _interaction_lines(sess_ctrl, 90) == [
        "Overwrite existing credential?",
        "",
        "Warning: TEST_API_TOKEN (session) already exists and will be replaced.",
        "",
        "→ Cancel",
        "  Overwrite",
        "",
        "↑↓ select · Enter confirm · Esc close · F2 reopen",
    ]
    ow_ansi = render_interaction(sess_ctrl, 90)
    assert "\x1b[1;33mOverwrite existing credential?\x1b[0m" in ow_ansi
    assert "\x1b[31mWarning: TEST_API_TOKEN (session) already exists and will be replaced.\x1b[0m" in ow_ansi


def test_golden_interaction_behaviors_mode_switch_draft_restore_scroll_and_status_dedup() -> None:
    import asyncio
    from prompt_toolkit.document import Document
    from prompt_toolkit.keys import Keys
    from agent.cli.interactions import InteractionController
    from agent.cli.rendering import render_interaction_viewport
    from agent.sessions.images import ImageAttachmentRef

    async def driver() -> None:
        model = scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "call-mixed",
                "name": "request_human_input",
                "args": {
                    "reason": "Need target service and token",
                    "question": "Configure external integration",
                    "fields": [
                        {"id": "service", "type": "text", "label": "Service name", "required": True},
                        {
                            "id": "api_key", "type": "secret", "label": "Service API Key",
                            "required": True, "secret_name": "SVC_KEY", "secret_scope": "sandbox",
                        },
                    ],
                },
            }]),
            AIMessage(content="Configured."),
        ])
        runner = AgentRunner(model=model, backend=StateBackend(), thread_id="beh-golden")
        waiting = runner.invoke("setup")
        assert waiting.status == "waiting_human"

        with create_pipe_input() as pipe:
            size = [Size(rows=16, columns=50)]
            output = DummyOutput()
            output.get_size = lambda: size[0]  # type: ignore[method-assign]
            app = CliApplication(runner, input=pipe, output=output)

            # Pre-existing draft, cursor, and attachment must survive opening & closing human input.
            app.buffer.set_document(Document("keep my draft", cursor_position=4))
            att = ImageAttachmentRef(id="img-1", filename="shot.png", mime_type="image/png", size=1024, storage_key="a" * 64 + ".png")
            app.state.attachments.append(att)

            def paint_frame() -> None:
                app.application.before_render.fire()
                _paint(app)

            app.run_events._handle_result(waiting)
            assert app.dialogs.current is not None
            assert app.dialogs.current.panel_mode == "human"
            assert app.buffer.text == ""
            paint_frame()
            assert app.application.layout.current_control is app.editor_control

            # Status bar must NOT duplicate open panel shortcut hints.
            app.state.status = "Select · Enter confirm · Esc close · F2 reopen"
            assert app._status_visible() is False
            assert app._status_text() == []
            app.state.status = "Waiting for input"
            assert app._status_visible() is True

            # Advance from plain text field -> secret field: mode, yellow border editor, and focus switch together!
            app.buffer.text = "acme"
            app.run_dispatch._submit_buffer("steer")
            assert app.dialogs.current.panel_mode == "secret"
            assert app.dialogs.current.accepts_secret is True
            paint_frame()
            assert app.application.layout.current_control is app.secret_editor
            # Verify yellow secret editor border & prompt classes are painted on screen.
            screen = app.application.renderer._last_screen
            painted_styles = {
                screen.data_buffer[y][x].style
                for y in range(size[0].rows)
                for x in range(size[0].columns)
            }
            assert any("class:secret-editor-border" in st for st in painted_styles)
            assert any("class:secret-editor-prompt" in st for st in painted_styles)

            # Shift+Tab back to normal text field restores cyan human mode, editor focus, and wipes secret buffer.
            app.secret_buffer.text = "typed-secret-never-leaks"
            stab = next(b for b in app.bindings.bindings if b.keys == (Keys.BackTab,))

            class _Ev:
                current_buffer = app.secret_buffer

            stab.handler(_Ev())
            assert app.dialogs.current.panel_mode == "human"
            assert app.dialogs.current.index == 0
            assert app.secret_buffer.text == ""
            assert app.buffer.text == "acme"
            paint_frame()
            assert app.application.layout.current_control is app.editor_control

            # Esc closes panel, restores draft/cursor/attachments, and restores pending status hint.
            app.interaction_flow._finish_interaction(cancelled=True)
            assert app.dialogs.current is None
            assert app.buffer.text == "keep my draft"
            assert app.buffer.cursor_position == 4
            assert app.state.attachments == [att]
            assert app.state.status == "Input still pending · F2 to decide"
            assert app._status_visible() is True

            # F2 reopens pending interaction cleanly.
            assert app.interaction_flow._reopen_pending_interaction() is True
            assert app.dialogs.current is not None and app.dialogs.current.kind == "human"

            # Short & narrow viewport clips long no-option secret panel while pinning heading and footer hints.
            long_secret = InteractionController.human({
                "question": " ".join(f"segment-{i}" for i in range(40)),
                "fields": [{
                    "id": "k", "type": "secret", "label": "Very Long Token Name",
                    "required": True, "secret_name": "LONG_TOK", "secret_scope": "sandbox",
                }],
            })
            clipped, _ = render_interaction_viewport(long_secret, width=36, height=7, offset=0)
            clipped_lines = [line.rstrip() for line in _plain(clipped).splitlines()]
            assert len(clipped_lines) <= 7
            assert clipped_lines[0] == "Credential required"
            assert "…" in clipped_lines
            assert any("Enter submit" in line for line in clipped_lines)

            runner.close()

    asyncio.run(driver())


