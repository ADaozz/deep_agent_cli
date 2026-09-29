from __future__ import annotations

import json
import shlex
import time
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from difflib import unified_diff
from io import StringIO
from pathlib import PurePosixPath
from typing import Any, TypeGuard
from zoneinfo import ZoneInfo

from prompt_toolkit import ANSI
from prompt_toolkit.formatted_text import (
    FormattedText,
    StyleAndTextTuples,
    to_formatted_text,
)
from prompt_toolkit.formatted_text.utils import split_lines
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from agent.cli.state import CliState, MessageBlock, ToolBlock, TurnSummaryBlock
from agent.config import DEFAULT_UI_TIMEZONE


# FilesystemMiddleware explore tools from DEFAULT_FS_TOOLS (factory.py).
EXPLORE_TOOLS = frozenset({"ls", "read_file", "glob", "grep"})
MUTATION_TOOLS = frozenset({"edit_file", "write_file", "delete"})
DIFF_PREVIEW_LINES = 10
WRITE_PREVIEW_LINES = 6
EXECUTE_TAIL_LINES = 8

EXPLORE_PREVIEW_LIMIT = 5
EXPLORE_FAILURE_PREVIEW_LIMIT = 3


def render_transcript(
    state: CliState,
    width: int,
    timezone: ZoneInfo | None = None,
) -> str:
    return "\n".join(
        _capture(unit.build(), width) for unit in transcript_units(state, timezone or ZoneInfo(DEFAULT_UI_TIMEZONE), width)
    )


# --- Incremental transcript rendering -------------------------------------
#
# The transcript is split into render units: the header, the plan, one message,
# one tool, or one collapsed explore group. A unit is pushed through Rich and
# parsed into prompt_toolkit fragments exactly once, then replayed from cache
# until its fingerprint changes. While streaming, only the active block is
# dirty, so frozen history never pays for Markdown/Syntax parsing again.


@dataclass
class RenderedUnit:
    fragments: StyleAndTextTuples
    line_count: int
    _lines: tuple[StyleAndTextTuples, ...] | None = None

    def get_line(self, index: int) -> StyleAndTextTuples:
        if self._lines is None:
            self._lines = tuple(list(line) for line in split_lines(self.fragments))
        return self._lines[index]


@dataclass(frozen=True)
class TranscriptDocument:
    units: tuple[RenderedUnit, ...]
    starts: tuple[int, ...]
    line_count: int

    def get_line(self, index: int) -> StyleAndTextTuples:
        if index < 0 or index >= self.line_count:
            raise IndexError(index)
        unit_index = bisect_right(self.starts, index) - 1
        return self.units[unit_index].get_line(index - self.starts[unit_index])


@dataclass(frozen=True)
class TranscriptUnit:
    key: Any
    fingerprint: Any
    build: Callable[[], Any]
    # Keeps the keyed object alive while its render is cached, so a freed block's
    # id() can never be recycled into a stale cache hit.
    owner: Any = None


def _render_unit(renderable: Any, width: int) -> RenderedUnit:
    text = _capture(renderable, width)
    fragments = to_formatted_text(ANSI(text))
    return RenderedUnit(fragments, text.count("\n") + 1)


def transcript_units(state: CliState, timezone: ZoneInfo | None = None, width: int = 80) -> list[TranscriptUnit]:
    """Structural pass over the timeline. Cheap: builds no Rich renderables."""
    timezone = timezone or ZoneInfo(DEFAULT_UI_TIMEZONE)
    units: list[TranscriptUnit] = [
        TranscriptUnit("header", (), lambda: Group(*_header())),
    ]
    if state.todos:
        todos = [dict(item) for item in state.todos]
        units.append(TranscriptUnit(
            "todos",
            tuple((item.get("content"), item.get("status")) for item in todos),
            lambda: _todos(todos),
        ))
    index = 0
    blocks = state.blocks
    while index < len(blocks):
        block = blocks[index]
        if isinstance(block, MessageBlock):
            units.append(_message_unit(block, state.thinking_collapsed))
            index += 1
            continue
        if isinstance(block, TurnSummaryBlock):
            units.append(TranscriptUnit(
                key=("turn_summary", id(block)),
                fingerprint=timezone.key,
                build=lambda block=block: _turn_summary(block, timezone),
                owner=block,
            ))
            index += 1
            continue
        if not state.tools_expanded and _is_explore(block):
            group = [block]
            index += 1
            while index < len(blocks):
                next_block = blocks[index]
                if not _is_explore(next_block):
                    break
                group.append(next_block)
                index += 1
            units.append(_explore_unit(group, width))
            continue
        units.append(_tool_unit(block, state.tools_expanded, width))
        index += 1
    return units


def _message_unit(block: MessageBlock, thinking_collapsed: bool) -> TranscriptUnit:
    return TranscriptUnit(
        key=("message", id(block)),
        fingerprint=(block.revision, thinking_collapsed),
        build=lambda: Group(*_message(block, thinking_collapsed)),
        owner=block,
    )


def _tool_unit(block: ToolBlock, expanded: bool, width: int) -> TranscriptUnit:
    return TranscriptUnit(
        key=("tool", id(block), expanded),
        fingerprint=(block.revision, expanded, _live_tick(block)),
        build=lambda: _tool(block, expanded, width),
        owner=block,
    )


def _explore_unit(blocks: list[ToolBlock], width: int) -> TranscriptUnit:
    members = tuple(blocks)
    tick = next((item for item in (_live_tick(block) for block in members) if item is not None), None)
    return TranscriptUnit(
        key=("explore", id(members[0])),
        fingerprint=(tuple(item.revision for item in members), tick),
        build=lambda: _explore_group(list(members), width),
        owner=members,
    )


def _live_tick(block: ToolBlock) -> int | None:
    """Spinner frame for in-flight tools, None once they are frozen."""
    return _spinner_tick() if block.status == "running" and block.name not in EXPLORE_TOOLS else None


def _same_owner(cached: Any, current: Any) -> bool:
    """Identity comparison; explore groups own a tuple rebuilt on every pass."""
    if isinstance(cached, tuple) or isinstance(current, tuple):
        return (
            isinstance(cached, tuple)
            and isinstance(current, tuple)
            and len(cached) == len(current)
            and all(left is right for left, right in zip(cached, current))
        )
    return cached is current


class TranscriptRenderer:
    """Per-unit render cache: frozen history is replayed, only changes re-render."""

    def __init__(self, timezone: ZoneInfo | None = None) -> None:
        self._cache: dict[Any, tuple[Any, Any, RenderedUnit]] = {}
        self._documents: dict[bool, tuple[Any, tuple[Any, ...], TranscriptDocument]] = {}
        self._width = 0
        self._timezone = timezone or ZoneInfo(DEFAULT_UI_TIMEZONE)

    def render_document(self, state: CliState, width: int) -> TranscriptDocument:
        if width != self._width:
            self.clear()
            self._width = width
        units = transcript_units(state, self._timezone, width)
        live_ids = {id(block) for block in state.blocks}
        live = {"header", "todos"}
        live.update(key for key in self._cache if isinstance(key, tuple) and len(key) > 1 and key[1] in live_ids)
        for key in [key for key in self._cache if key not in live]:
            del self._cache[key]
        signature = tuple((unit.key, unit.fingerprint) for unit in units)
        mode = state.tools_expanded
        document_entry = self._documents.get(mode)
        if document_entry is not None and document_entry[0] == signature:
            return document_entry[2]
        rendered_units: list[RenderedUnit] = []
        starts: list[int] = []
        line_count = 0
        for unit in units:
            entry = self._cache.get(unit.key)
            if (
                entry is None
                or not _same_owner(entry[0], unit.owner)
                or entry[1] != unit.fingerprint
            ):
                rendered = _render_unit(unit.build(), width)
                self._cache[unit.key] = (unit.owner, unit.fingerprint, rendered)
            else:
                rendered = entry[2]
            starts.append(line_count)
            line_count += rendered.line_count
            rendered_units.append(rendered)
        document = TranscriptDocument(tuple(rendered_units), tuple(starts), line_count)
        self._documents[mode] = (signature, tuple(unit.owner for unit in units), document)
        return document

    def render(self, state: CliState, width: int) -> tuple[FormattedText, int]:
        document = self.render_document(state, width)
        fragments: StyleAndTextTuples = []
        for index in range(document.line_count):
            if index:
                fragments.append(("", "\n"))
            fragments.extend(document.get_line(index))
        return FormattedText(fragments), document.line_count

    def clear(self) -> None:
        self._cache.clear()
        self._documents.clear()


def render_interaction(controller: Any, width: int) -> str:
    if controller is None:
        return ""
    return _capture(controller.render(), width)


def render_review(calls: list[tuple[str, dict[str, Any]]], width: int) -> str:
    parts: list[Any] = [
        Text("Review", style="bold bright_cyan"),
        Text("Esc or Ctrl+R close · this is not an approval", style="dim"),
    ]
    for name, args in calls:
        diff = mutation_diff(name, args)
        if not diff:
            continue
        parts.append(Text(""))
        if name == "write_file":
            added = _written_lines(diff)
            parts.append(Text(_tool_summary(name, args), style="bold"))
            parts.append(Text(_added_line_label(len(added)), style="green"))
            parts.append(Syntax("\n".join(added), "diff", theme="ansi_dark", word_wrap=True))
        else:
            parts.append(Syntax(diff, "diff", theme="ansi_dark", word_wrap=True))
    if len(parts) == 2:
        parts.append(Text("Nothing to review.", style="dim"))
    return _capture(Group(*parts), width)


def mutation_diff(name: str, args: dict[str, Any]) -> str:
    if name == "edit_file":
        return _edit_diff(args)
    if name == "write_file":
        content = args.get("content")
        if not isinstance(content, str):
            return ""
        path = str(args.get("file_path") or args.get("path") or "file")
        return "\n".join(unified_diff(
            [],
            content.splitlines(),
            fromfile="/dev/null",
            tofile=_diff_name("b", path),
            lineterm="",
        ))
    if name == "delete":
        path = str(args.get("file_path") or args.get("path") or "file")
        return f"delete {path}"
    return ""


def _message(block: MessageBlock, thinking_collapsed: bool) -> list[Any]:
    items: list[Any] = []
    if block.kind == "user":
        style = "white on #404040" if block.pending else "white on #303030"
        label = f"⟳ {block.content}" if block.pending else block.content
        message = Table.grid(expand=True, padding=0)
        message.add_column(width=2, no_wrap=True)
        message.add_column(ratio=1)
        message.add_row(Text("› "), Markdown(label))
        content: list[Any] = [message]
        for index, ref in enumerate(block.attachments, 1):
            content.append(Text(
                f"▣ Image #{index}  {ref.filename} · {_format_bytes(ref.size)}",
                style="bright_cyan",
            ))
        items.append(Padding(Group(*content), (1, 1, 1, 1), style=style))
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


def _turn_summary(block: TurnSummaryBlock, timezone: ZoneInfo) -> Any:
    elapsed = max(0, int(block.elapsed_seconds))
    hours, remaining = divmod(elapsed, 3600)
    minutes, seconds = divmod(remaining, 60)
    if hours:
        duration = f"{hours}h {minutes}m {seconds}s"
    elif minutes:
        duration = f"{minutes}m {seconds}s"
    else:
        duration = f"{seconds}s"
    finished = block.finished_at.astimezone(timezone).strftime("%H:%M")
    return Padding(Text(f"Worked for {duration} · {finished}", style="dim"), (0, 1, 1, 1))


def _header() -> list[Any]:
    title = Text.assemble(("DeepAgent", "bold bright_cyan"), ("  terminal coding agent", "dim"))
    hints = Text(
        "Esc interrupt · F2 pending · Ctrl+O tools · Ctrl+R review · / commands",
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
    return Padding(Group(*lines), (0, 1, 1, 1))


def _is_explore(block: Any) -> TypeGuard[ToolBlock]:
    return isinstance(block, ToolBlock) and block.name in EXPLORE_TOOLS


def _explore_group(blocks: list[ToolBlock], width: int = 80) -> Any:
    running = any(block.status == "running" for block in blocks)
    waiting = any(block.status == "waiting" for block in blocks)
    interrupted = sum(block.status == "interrupted" for block in blocks)
    suffix = " — waiting for input" if waiting and not running else ""
    failed = [block for block in blocks if block.is_error]
    color = "#888888" if failed or interrupted else "green" if running else "yellow" if waiting else "green"
    count = len(blocks)
    title = Text.assemble(
        (" ● ", color),
        (f"Explored {count} {'item' if count == 1 else 'items'}", "bold"),
        (f" · {len(failed)} failed", "dim") if failed else ("", ""),
        (f" · {interrupted} interrupted", "dim") if interrupted else ("", ""),
        (suffix, "dim"),
    )
    title.truncate(max(1, width - 1), overflow="ellipsis")
    rows: list[Text] = []
    hidden = count - EXPLORE_PREVIEW_LIMIT
    if hidden > 0:
        row = Text(f"  ├ … {hidden} more", style="dim")
        row.truncate(max(1, width - 1), overflow="ellipsis")
        rows.append(row)
    preview = blocks[-EXPLORE_PREVIEW_LIMIT:]
    for index, block in enumerate(preview):
        branch = "└" if index == len(preview) - 1 else "├"
        row = Text.assemble((f"  {branch} ", "dim"), (_explore_summary(block), ""))
        row.truncate(max(1, width - 1), overflow="ellipsis")
        rows.append(row)
    if failed:
        omitted = max(0, len(failed) - EXPLORE_FAILURE_PREVIEW_LIMIT)
        if omitted:
            row = Text(f"  ├ … {omitted} more failed", style="dim")
            row.truncate(max(1, width - 1), overflow="ellipsis")
            rows.append(row)
        for block in failed[-EXPLORE_FAILURE_PREVIEW_LIMIT:]:
            reason = _failure_summary(block.output)
            row = Text.assemble(
                (f"  ├ Failed (exit {_failure_code(block)}) ", "dim"),
                (_explore_summary(block), ""),
                (f": {reason}" if reason else "", "dim"),
            )
            row.truncate(max(1, width - 1), overflow="ellipsis")
            rows.append(row)
    hint = Text("  Ctrl+O to expand", style="dim")
    hint.truncate(max(1, width - 1), overflow="ellipsis")
    rows.append(hint)
    # Avoid full-width Padding here: a terminal auto-wrap can leave stale text
    # over the editor when this summary changes.
    return Group(Text(""), title, *rows)


def _explore_summary(block: ToolBlock) -> str:
    args = block.arguments
    if block.name == "read_file":
        return "Read " + _tool_summary(block.name, args).removeprefix("read ")
    if block.name in {"grep", "glob"}:
        verb = "Search" if block.name == "grep" else "Glob"
        pattern = args.get("pattern") or args.get("query")
        return f"{verb} {json.dumps(str(pattern), ensure_ascii=False)}" if pattern else verb
    if block.name == "ls":
        path = args.get("path") or args.get("file_path")
        return f"List {PurePosixPath(str(path))}" if path else "List"
    return _tool_summary(block.name, args)


def _tool(block: ToolBlock, expanded: bool, width: int = 80) -> Any:
    if block.is_error:
        summary = str(block.arguments.get("command") or "execute") if block.name == "execute" else _tool_summary(block.name, block.arguments)
        title = Text.assemble(
            ("● ", "#888888"),
            (f"Failed (exit {_failure_code(block)}) ", "bold"),
            (summary, ""),
        )
        console = Console(width=max(20, width))
        body: list[Any] = [Text("")]
        body.extend(_wrap_failure_line(title, console, width))
        lines = _failure_lines(block, expanded)
        if lines:
            body.extend(_wrap_failure_line(Text("  └ output", style="dim"), console, width))
            for line in lines:
                body.extend(_wrap_failure_line(Text(f"    {line}", style="dim"), console, width))
        return Group(*body)
    symbol = _spinner() if block.status == "running" and block.name not in EXPLORE_TOOLS else "●"
    suffix = (
        " — waiting for input" if block.status == "waiting"
        else " — interrupted (completion unconfirmed)" if block.status == "interrupted"
        else ""
    )
    color = "#888888" if block.status == "interrupted" else "yellow" if block.status == "waiting" else "green"
    title = Text.assemble((f"{symbol} ", color), (_tool_summary(block.name, block.arguments), "bold"), (suffix, "dim"))
    body: list[Any] = [title]
    if block.name in MUTATION_TOOLS:
        diff = mutation_diff(block.name, block.arguments)
        if diff:
            if block.name == "write_file":
                added = _written_lines(diff)
                body.append(Text(f"  {_added_line_label(len(added))}", style="green"))
                body.append(Text(""))
                body.append(Syntax("\n".join(added[:WRITE_PREVIEW_LINES]), "diff", theme="ansi_dark", word_wrap=True))
                hidden = len(added) - WRITE_PREVIEW_LINES
                if hidden > 0:
                    body.append(Text(f"  … {hidden} lines hidden · Ctrl+R to review", style="dim"))
            else:
                preview = _preview_diff(diff)
                body.append(Syntax(preview, "diff", theme="ansi_dark", word_wrap=True))
        return Padding(Group(*body), (1, 1, 0, 1), expand=False)
    output = block.output.strip()
    if block.name == "web_search" and block.status == "completed" and not expanded and output.startswith("Web search results for:"):
        count_line = next((line for line in output.splitlines() if line.endswith(" results") and line.split(" ", 1)[0].isdigit()), "")
        if count_line:
            body.append(Text(f"  … {count_line} · Ctrl+O to expand", style="dim"))
            return Padding(Group(*body), (1, 1, 0, 1), expand=False)
    if block.name == "execute":
        if output:
            lines = output.splitlines()
            if block.status != "running" and not expanded and len(lines) > EXECUTE_TAIL_LINES:
                skipped = len(lines) - EXECUTE_TAIL_LINES
                lines = [f"… {skipped} output lines hidden · Ctrl+O to expand", *lines[-EXECUTE_TAIL_LINES:]]
            body.append(Text("\n".join(f"  {line}" for line in lines), style="dim" if not block.is_error else "red"))
        elif block.status == "completed":
            body.append(Text("  (no output)", style="dim"))
        return Padding(Group(*body), (1, 1, 0, 1), expand=False)
    if output:
        lines = output.splitlines()
        limit = 40 if expanded else 8
        if len(lines) > limit:
            skipped = len(lines) - limit
            lines = [f"… {skipped} output lines hidden · Ctrl+O to expand", *lines[-limit:]]
        body.append(Text("\n".join(f"  {line}" for line in lines), style="dim" if not block.is_error else "red"))
    return Padding(Group(*body), (1, 1, 0, 1), expand=False)


def _failure_code(block: ToolBlock) -> int:
    return block.exit_code if isinstance(block.exit_code, int) and not isinstance(block.exit_code, bool) else 1


def _wrap_failure_line(line: Text, console: Console, width: int) -> list[Text]:
    """Leave a spare terminal column while preserving long commands and output."""
    return list(line.wrap(console, max(1, width - 2), overflow="fold"))


def _failure_summary(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return ""
    if lines[0].startswith("参数校验失败"):
        return next((line.removeprefix("- ") for line in lines if line.startswith("- ")), lines[0])
    return next((line for line in lines if not line.startswith("Exit code:")), "")


def _failure_lines(block: ToolBlock, expanded: bool) -> list[str]:
    lines = block.output.strip().splitlines()
    if expanded:
        return lines
    if lines and lines[0].startswith("参数校验失败"):
        details = [line.strip().removeprefix("- ") for line in lines if line.startswith("- ")]
        shown = details[:4] or lines[:1]
        hidden = len(lines) - len(shown)
    else:
        content = [line for line in lines if line.strip() and not line.strip().startswith("Exit code:")]
        limit = 6 if block.name == "execute" else 4
        shown = content[-limit:] if block.name == "execute" else content[:limit]
        hidden = len(content) - len(shown)
    if hidden > 0:
        shown.append(f"… {hidden} output lines hidden · Ctrl+O to expand")
    return shown


def _preview_diff(diff: str) -> str:
    lines = diff.splitlines()
    if len(lines) <= DIFF_PREVIEW_LINES:
        return diff
    hidden = len(lines) - DIFF_PREVIEW_LINES
    return "\n".join([
        *lines[:DIFF_PREVIEW_LINES],
        f"… truncated ({hidden} more lines) · Ctrl+R to review …",
    ])


def _written_lines(diff: str) -> list[str]:
    """Extract added content from the synthetic new-file diff for write_file."""
    lines = diff.splitlines()
    if len(lines) < 3 or lines[0] != "--- /dev/null" or not lines[1].startswith("+++ "):
        return []
    return [line for line in lines[2:] if line.startswith("+")]


def _added_line_label(count: int) -> str:
    return f"+{count} {'line' if count == 1 else 'lines'}"


def _tool_summary(name: str, args: dict[str, Any]) -> str:
    display = {"read_file": "read", "write_file": "write", "edit_file": "edit"}.get(name, name)
    if name == "execute":
        command = str(args.get("command") or "…")
        return f"execute {command}"
    if name == "web_search":
        query = str(args.get("query") or "…").replace("\n", " ")
        return f'web_search "{query[:80]}"'
    if name == "request_human_input":
        question = str(args.get("question") or "input required").replace("\n", " ")
        return f"ask {question[:100]}"
    path = args.get("file_path") or args.get("path")
    if path:
        label = str(PurePosixPath(str(path)))
        if name == "write_file" and label.startswith("/workspace/"):
            label = label.lstrip("/")
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


def _diff_name(side: str, path: str) -> str:
    if path.startswith("/"):
        return f"{side}{path}"
    return f"{side}/{path}"


def _edit_diff(args: dict[str, Any]) -> str:
    old = args.get("old_string") or args.get("old_text")
    new = args.get("new_string") or args.get("new_text")
    if not isinstance(old, str) or not isinstance(new, str):
        return ""
    path = str(args.get("file_path") or args.get("path") or "file")
    return "\n".join(unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile=_diff_name("a", path),
        tofile=_diff_name("b", path),
        lineterm="",
    ))


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _spinner_tick() -> int:
    return int(time.monotonic() * 10) % len(_SPINNER_FRAMES)


def _spinner() -> str:
    return _SPINNER_FRAMES[_spinner_tick()]


def _format_bytes(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MiB"
    if size >= 1024:
        return f"{size / 1024:.0f} KiB"
    return f"{size} B"


def _capture(renderable: Any, width: int) -> str:
    stream = StringIO()
    # No soft_wrap: Rich must word-wrap at exactly `width` so one emitted line is
    # one screen row. ScrollbarMargin divides displayed rows by logical lines, so
    # letting the viewport re-wrap would inflate the thumb and skew its position.
    console = Console(
        file=stream,
        force_terminal=True,
        color_system="truecolor",
        width=max(20, width),
    )
    console.print(renderable)
    return stream.getvalue().rstrip("\n")
