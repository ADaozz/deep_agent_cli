from __future__ import annotations

import json
import shlex
import time
from difflib import unified_diff
from io import StringIO
from pathlib import PurePosixPath
from typing import Any

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text

from agent.cli.state import CliState, MessageBlock, ToolBlock


def render_transcript(
    state: CliState,
    width: int,
) -> str:
    renderables: list[Any] = []
    renderables.extend(_header())
    if state.todos:
        renderables.append(_todos(state.todos))
    for block in state.blocks:
        if isinstance(block, MessageBlock):
            renderables.extend(_message(block, state.thinking_collapsed))
        else:
            renderables.append(_tool(block, state.tools_expanded))
    return _capture(Group(*renderables), width)


def render_interaction(controller: Any, width: int) -> str:
    if controller is None:
        return ""
    return _capture(controller.render(), width)


def _message(block: MessageBlock, thinking_collapsed: bool) -> list[Any]:
    items: list[Any] = []
    if block.kind == "user":
        style = "white on #404040" if block.pending else "white on #303030"
        label = f"⟳ {block.content}" if block.pending else block.content
        content: list[Any] = []
        if label:
            content.append(Markdown(label))
        for index, ref in enumerate(block.attachments, 1):
            content.append(Text(
                f"▣ Image #{index}  {ref.filename} · {_format_bytes(ref.size)}",
                style="bright_cyan",
            ))
        items.append(Padding(Group(*content), (0, 1), style=style))
    elif block.kind == "assistant":
        if block.thinking:
            value = "Thinking…" if thinking_collapsed else block.thinking
            items.append(Padding(Markdown(value), (1, 1, 0, 1), style="italic #888888"))
        if block.content:
            items.append(Padding(Markdown(block.content), (1, 1, 0, 1)))
    elif block.kind == "error":
        items.append(Padding(Text(f"Error: {block.content}", style="red"), (1, 1, 0, 1)))
    else:
        items.append(Padding(Text(block.content, style="yellow"), (1, 1, 0, 1)))
    return items


def _header() -> list[Any]:
    title = Text.assemble(("DeepAgent", "bold bright_cyan"), ("  terminal coding agent", "dim"))
    hints = Text(
        "Esc interrupt · Ctrl+C clear/exit · / commands · Ctrl+O tools · drag to copy",
        style="dim",
    )
    items: list[Any] = [title, hints]
    items.extend([Rule(style="#303030"), Text("")])
    return items


def _todos(todos: list[dict[str, str]]) -> Any:
    lines: list[Text] = [Text("Plan", style="bold bright_cyan")]
    for item in todos:
        status = item.get("status")
        symbol, style = {
            "completed": ("✓", "green"),
            "in_progress": ("●", "yellow"),
        }.get(status, ("○", "dim"))
        lines.append(Text.assemble((f"{symbol} ", style), (str(item.get("content") or ""), style)))
    return Padding(Group(*lines), (0, 1, 1, 1), style="on #1e2a30")


def _tool(block: ToolBlock, expanded: bool) -> Any:
    symbol = "●"
    suffix = f" {_spinner()}" if block.status == "running" else " — waiting for input" if block.status == "waiting" else ""
    color = "red" if block.is_error else "yellow" if block.status in {"running", "waiting"} else "green"
    title = Text.assemble((f"{symbol} ", color), (_tool_summary(block.name, block.arguments), "bold"), (suffix, "dim"))
    body: list[Any] = [title]
    output = block.output.strip()
    # Always show tool results (especially execute); collapse only very long blobs.
    if output:
        lines = output.splitlines()
        limit = 40 if expanded else (20 if block.name == "execute" else 8)
        if len(lines) > limit:
            skipped = len(lines) - limit
            lines = [f"… ({skipped} earlier lines, Ctrl+O to expand)", *lines[-limit:]]
        body.append(Text("\n".join(f"  {line}" for line in lines), style="dim" if not block.is_error else "red"))
    elif block.status == "completed" and block.name == "execute":
        body.append(Text("  (no output)", style="dim"))
    if block.name == "edit_file" and not block.is_error:
        diff = _edit_diff(block.arguments)
        if diff:
            body.append(Syntax(diff, "diff", theme="ansi_dark", word_wrap=True))
    if expanded and block.arguments:
        body.append(Syntax(json.dumps(block.arguments, ensure_ascii=False, indent=2), "json", theme="ansi_dark"))
    style = "on #321f1f" if block.is_error else "on #242424"
    return Padding(Group(*body), (1, 1, 0, 1), style=style)


def _tool_summary(name: str, args: dict[str, Any]) -> str:
    display = {"read_file": "read", "write_file": "write", "edit_file": "edit"}.get(name, name)
    if name == "execute":
        command = str(args.get("command") or "…")
        return f"execute {command}"
    if name in {"request_human_input", "handoff_to_human"}:
        question = str(args.get("question") or "input required").replace("\n", " ")
        label = "handoff" if name == "handoff_to_human" else "ask"
        return f"{label} {question[:100]}"
    path = args.get("file_path") or args.get("path")
    if path:
        label = str(PurePosixPath(str(path)))
        offset = args.get("offset")
        limit = args.get("limit")
        if offset is not None or limit is not None:
            start = int(offset or 1)
            label += f":{start}-{start + int(limit) - 1}" if limit else f":{start}"
        return f"{display} {label}"
    preferred = ("query", "pattern", "to", "subject")
    values = []
    for key in preferred:
        value = args.get(key)
        if isinstance(value, (str, int, float, bool)) and str(value):
            values.append(shlex.quote(str(value))[:80])
        if len(values) == 2:
            break
    return " ".join([display, *values])


def _edit_diff(args: dict[str, Any]) -> str:
    old = args.get("old_string") or args.get("old_text")
    new = args.get("new_string") or args.get("new_text")
    if not isinstance(old, str) or not isinstance(new, str):
        return ""
    path = str(args.get("file_path") or args.get("path") or "file")
    return "\n".join(unified_diff(
        old.splitlines(), new.splitlines(), fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
    ))


def _spinner() -> str:
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    return frames[int(time.monotonic() * 10) % len(frames)]


def _format_bytes(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MiB"
    if size >= 1024:
        return f"{size / 1024:.0f} KiB"
    return f"{size} B"


def _capture(renderable: Any, width: int) -> str:
    stream = StringIO()
    console = Console(
        file=stream,
        force_terminal=True,
        color_system="truecolor",
        width=max(20, width),
        soft_wrap=True,
    )
    console.print(renderable)
    return stream.getvalue().rstrip("\n")
