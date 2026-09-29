from __future__ import annotations

import time
import re
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from io import StringIO
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

from agent.cli.previews import (
    CommandExecutionPreview,
    EXPAND_HINT,
    REVIEW_HINT,
    ToolPreview,
    added_line_label,
    aggregate_file_mutations,
    build_review_mutation,
    build_tool_preview,
    file_tool_preview,
    normalize_file_mutation,
    tool_group,
)
from agent.cli.state import CliState, MessageBlock, ToolBlock, TurnSummaryBlock
from agent.config import DEFAULT_UI_TIMEZONE

# Non-file output folding stays in the renderer.
EXECUTE_TAIL_LINES = 4

EXPLORE_PREVIEW_LIMIT = 5
EXPLORE_FAILURE_PREVIEW_LIMIT = 3

# PreviewLine style -> Rich style mapping for line-oriented mutation bodies.
_PREVIEW_LINE_STYLES: dict[str, str] = {
    "plain": "",
    "dim": "dim",
    "hunk": "bold cyan",
    "add": "white on #245c38",
    "delete": "white on #4a2028",
    "error": "red",
    "url": "bright_cyan",
}


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
            (
                tuple((item.get("content"), item.get("status")) for item in todos),
                _spinner_tick() if any(item.get("status") == "in_progress" for item in todos) else None,
            ),
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
        create_path = _created_file_path(block)
        if create_path is not None:
            group = [block]
            while index + len(group) < len(blocks):
                candidate = blocks[index + len(group)]
                if _created_file_path(candidate) is None:
                    break
                group.append(candidate)
            if len(group) > 1:
                units.append(_create_group_unit(group, state.tools_expanded, width))
                index += len(group)
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
        edit_path = _mergeable_modify_path(block)
        if edit_path is not None:
            group = [block]
            while index + len(group) < len(blocks):
                candidate = blocks[index + len(group)]
                if _mergeable_modify_path(candidate) != edit_path:
                    break
                group.append(candidate)
            if len(group) > 1:
                units.append(_modify_group_unit(group, width))
                index += len(group)
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


def _mergeable_modify_path(block: Any) -> str | None:
    mutation = normalize_file_mutation(block) if isinstance(block, ToolBlock) else None
    return mutation.path if mutation and mutation.operation == "modify" and mutation.diff else None


def _created_file_path(block: Any) -> str | None:
    mutation = normalize_file_mutation(block) if isinstance(block, ToolBlock) else None
    return mutation.path if mutation and mutation.operation == "create" else None


def _create_group_unit(blocks: list[ToolBlock], expanded: bool, width: int) -> TranscriptUnit:
    members = tuple(blocks)
    return TranscriptUnit(
        key=("create_group", id(members[0]), expanded),
        fingerprint=tuple(block.revision for block in members) + (tuple(id(block) for block in members), expanded),
        build=lambda: _create_group(list(members), expanded, width),
        owner=members,
    )


def _create_group(blocks: list[ToolBlock], expanded: bool, width: int) -> Any:
    mutations = [item for block in blocks if (item := normalize_file_mutation(block)) is not None]
    mutation = aggregate_file_mutations(mutations, expanded=expanded)
    if len(mutation.paths) == 1:
        return _tool(blocks[-1], expanded, width)
    title = Text.assemble(("● ", "green"), (f"Create {len(mutation.paths)} files", "bold"))
    rows = [Text(line.text, style=_PREVIEW_LINE_STYLES[line.style]) for line in mutation.compact_lines]
    for row in (title, *rows):
        row.truncate(max(1, width - 1), overflow="ellipsis")
    return Group(Text(""), title, *rows)


def _modify_group_unit(blocks: list[ToolBlock], width: int) -> TranscriptUnit:
    members = tuple(blocks)
    return TranscriptUnit(
        key=("modify_group", id(members[0])),
        fingerprint=tuple(block.revision for block in members) + (tuple(id(block) for block in members),),
        build=lambda: _modify_group(list(members), width),
        owner=members,
    )


def _modify_group(blocks: list[ToolBlock], width: int) -> Any:
    mutations = [item for block in blocks if (item := normalize_file_mutation(block)) is not None]
    mutation = aggregate_file_mutations(mutations)
    preview = file_tool_preview(mutation)
    return render_tool_preview(blocks[-1], preview, expanded=False, width=width)


def _live_tick(block: ToolBlock) -> int | None:
    """Spinner frame for in-flight tools, None once they are frozen."""
    return _spinner_tick() if block.status == "running" else None


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
        mutation = build_review_mutation(name, args)
        if mutation is None or mutation.diff is None or not mutation.diff.full_diff:
            continue
        diff = mutation.diff
        parts.append(Text(""))
        if mutation.operation == "overwrite":
            parts.append(Text(f"write {diff.path}", style="bold"))
            parts.append(Text(added_line_label(diff.added or 0), style="green"))
            parts.append(Group(*(
                row
                for number, line in enumerate(diff.lines, 1)
                for row in _diff_row(f"{number:>4} {line.text}", "add", width)
            )))
        elif mutation.operation == "delete":
            parts.append(Text(f"delete {diff.path}", style="dim"))
        else:
            parts.append(Group(*_numbered_diff(diff.full_diff, width)))
    if len(parts) == 2:
        parts.append(Text("Nothing to review.", style="dim"))
    return _capture(Group(*parts), width)


def _diff_row(content: str, kind: str, width: int) -> list[Text]:
    """Fill every visual row of a changed line, including wrapped rows."""
    style = _PREVIEW_LINE_STYLES[kind]
    available = max(1, width - 1)  # Spare terminal column avoids auto-wrap.
    console = Console(width=max(20, width))
    rows = list(Text(content, style=style).wrap(console, available, overflow="fold"))
    if kind in {"add", "delete"}:
        for row in rows:
            row.pad_right(max(0, available - row.cell_len))
    return rows


def _numbered_diff(raw: str, width: int) -> list[Text]:
    """Render source line numbers from unified hunk coordinates."""
    result: list[Text] = []
    old_number = new_number = 0
    for line in raw.splitlines():
        if line.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match:
                old_number, new_number = map(int, match.groups())
            result.extend(_diff_row(line, "hunk", width))
        elif line.startswith(("---", "+++")):
            result.extend(_diff_row(line, "dim", width))
        elif line.startswith("-"):
            result.extend(_diff_row(f"{old_number:>4} {line}", "delete", width))
            old_number += 1
        elif line.startswith("+"):
            result.extend(_diff_row(f"{new_number:>4} {line}", "add", width))
            new_number += 1
        elif line.startswith(" "):
            result.extend(_diff_row(f"{new_number:>4} {line}", "dim", width))
            old_number += 1
            new_number += 1
        else:
            result.extend(_diff_row(line, "dim", width))
    return result


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
        marker_status = {
            "in_progress": "running", "pending": "waiting", "blocked": "waiting",
            "failed": "error", "completed": "completed",
        }.get(status or "", "waiting")
        symbol, style = _tool_status_marker(marker_status)
        lines.append(Text.assemble((f"{symbol} ", style), (str(item.get("content") or ""), style)))
    return Padding(Group(*lines), (0, 1, 1, 1))


def _is_explore(block: Any) -> TypeGuard[ToolBlock]:
    return isinstance(block, ToolBlock) and tool_group(block.name) == "explore"


def _preview_label(preview: ToolPreview) -> str:
    return preview.summary or f"{preview.verb} {preview.target}".strip() or preview.kind


def _explore_group(blocks: list[ToolBlock], width: int = 80) -> Any:
    previews = [build_tool_preview(block) for block in blocks]
    running = any(block.status == "running" for block in blocks)
    waiting = any(block.status == "waiting" for block in blocks)
    interrupted = sum(block.status == "interrupted" for block in blocks)
    suffix = " — waiting for input" if waiting and not running else ""
    failed = [
        (block, preview)
        for block, preview in zip(blocks, previews)
        if block.is_error
    ]
    marker_status = "error" if failed or interrupted else "running" if running else "waiting" if waiting else "completed"
    symbol, color = _tool_status_marker(marker_status)
    count = len(blocks)
    title = Text.assemble(
        (f" {symbol} ", color),
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
    shown = previews[-EXPLORE_PREVIEW_LIMIT:]
    for index, preview in enumerate(shown):
        branch = "└" if index == len(shown) - 1 else "├"
        row = Text.assemble((f"  {branch} ", "dim"), (_preview_label(preview), ""))
        row.truncate(max(1, width - 1), overflow="ellipsis")
        rows.append(row)
    if failed:
        omitted = max(0, len(failed) - EXPLORE_FAILURE_PREVIEW_LIMIT)
        if omitted:
            row = Text(f"  ├ … {omitted} more failed", style="dim")
            row.truncate(max(1, width - 1), overflow="ellipsis")
            rows.append(row)
        for block, preview in failed[-EXPLORE_FAILURE_PREVIEW_LIMIT:]:
            reason = preview.detail
            row = Text.assemble(
                (f"  ├ Failed (exit {_failure_code(block)}) ", "dim"),
                (_preview_label(preview), ""),
                (f": {reason}" if reason else "", "dim"),
            )
            row.truncate(max(1, width - 1), overflow="ellipsis")
            rows.append(row)
    hint = Text(f"  {EXPAND_HINT}", style="dim")
    hint.truncate(max(1, width - 1), overflow="ellipsis")
    rows.append(hint)
    # Avoid full-width Padding here: a terminal auto-wrap can leave stale text
    # over the editor when this summary changes.
    return Group(Text(""), title, *rows)


def _tool(block: ToolBlock, expanded: bool, width: int = 80) -> Any:
    return render_tool_preview(
        block, build_tool_preview(block), expanded=expanded, width=width,
    )


def render_tool_preview(
    block: ToolBlock, preview: ToolPreview, *, expanded: bool, width: int = 80,
) -> Any:
    """Generic tool card: status, spinner, colors, folding — no tool semantics."""
    if preview.command_execution is not None:
        return _command_card(block.output, preview.command_execution, expanded, width)
    if preview.kind == "failure":
        return _failure_card(block, preview, expanded, width)
    symbol, color = _tool_status_marker(block.status, block.is_error)
    suffix = (
        " — waiting for input" if block.status == "waiting"
        else " — interrupted (completion unconfirmed)" if block.status == "interrupted"
        else ""
    )
    summary = f"{preview.verb} {preview.target}".strip()
    title = Text.assemble((f"{symbol} ", color), (summary, "bold"))
    mutation = preview.file_mutation
    if mutation is not None and mutation.operation == "modify" and mutation.diff is not None:
        title.append(" (")
        title.append(f"+{mutation.diff.added or 0}", style="green")
        title.append(" ")
        title.append(f"-{mutation.diff.deleted or 0}", style="red")
        title.append(")")
    elif preview.group == "mutation" and preview.detail:
        title.append(f" {preview.detail}", style="bold")
    title.append(suffix, style="dim")
    body: list[Any] = [title]
    if preview.group == "mutation":
        body.extend(_mutation_body(preview, width))
        return Padding(Group(*body), (1, 0, 0, 0), expand=False)
    elif preview.lines:
        body.append(Text(
            "\n".join(f"  {line.text}" for line in preview.lines),
            style="dim" if not block.is_error else "red",
        ))
    elif preview.group == "web" and not expanded and preview.detail:
        body.append(Text(f"  … {preview.detail} · {EXPAND_HINT}", style="dim"))
    else:
        output = block.output.strip()
        if output:
            lines = output.splitlines()
            limit = 40 if expanded else 8
            if len(lines) > limit:
                skipped = len(lines) - limit
                lines = [f"… {skipped} output lines hidden · {EXPAND_HINT}", *lines[-limit:]]
            body.append(Text(
                "\n".join(f"  {line}" for line in lines),
                style="dim" if not block.is_error else "red",
            ))
    return Padding(Group(*body), (1, 1, 0, 1), expand=False)


def _command_card(output: str, preview: CommandExecutionPreview, expanded: bool, width: int) -> Any:
    """Render the current output snapshot; every delta rebuilds this card."""
    status = preview.status
    verb = {
        "running": "execute", "waiting": "execute", "interrupted": "execute",
        "succeeded": "Ran", "failed": f"Failed (exit {preview.exit_code if preview.exit_code is not None else 1})",
        "timeout": "Timed out", "cancelled": "Cancelled", "spawn_error": "Failed to start",
    }[status]
    suffix = (
        " — waiting for input" if status == "waiting"
        else " — interrupted (completion unconfirmed)" if status == "interrupted"
        else ""
    )
    marker_status = {
        "running": "running", "waiting": "waiting", "succeeded": "completed",
    }.get(status, "error")
    symbol, color = _tool_status_marker(marker_status)
    title = Text.assemble(
        (f"{symbol} ", color), (f"{verb} {preview.command}", "bold"), (suffix, "dim"),
    )
    console = Console(width=max(20, width))
    body: list[Any] = []
    body.extend(_wrap_failure_line(title, console, width))

    display_output = output
    if preview.truncated:
        # The executor embeds a saved-log footer in its returned text. It is
        # still kept in ToolBlock.output, but the card displays one normalized
        # notice with the agent-visible path after the live output.
        display_output = re.sub(
            r"(?:\n\n|^)\[Output truncated(?::|\.)[^\]]*\]",
            "", display_output, count=1, flags=re.DOTALL,
        )
        display_output = display_output.removesuffix("\n\n[output truncated]")
    if status == "failed" and preview.exit_code is not None:
        footer = f"Exit code: {preview.exit_code}"
        display_output = "" if display_output == footer else display_output.removesuffix(f"\n\n{footer}")
    elif status == "cancelled":
        footer = "Cancelled by user."
        display_output = "" if display_output == footer else display_output.removesuffix(f"\n\n{footer}")
    elif status == "timeout":
        display_output = re.sub(
            r"(?:\n\n|^)Error: Command timed out after [0-9.]+ seconds\.$",
            "", display_output,
        )
    lines = display_output.strip().splitlines()
    if not expanded and len(lines) > EXECUTE_TAIL_LINES:
        hidden = len(lines) - EXECUTE_TAIL_LINES
        lines = [f"… {hidden} earlier output lines hidden · {EXPAND_HINT}", *lines[-EXECUTE_TAIL_LINES:]]
    if not lines and status not in {"running", "waiting", "interrupted"}:
        lines = ["(no output)"]
    if lines:
        body.extend(_wrap_failure_line(Text("  └ output", style="dim"), console, width))
    for line in lines:
        body.extend(_wrap_failure_line(Text(f"    {line}", style="dim"), console, width))

    if preview.truncated:
        if preview.log_error:
            notice = f"Full output could not be saved: {preview.log_error}"
        elif preview.log_path:
            notice = f"Output truncated · full output: {preview.log_path}"
        else:
            notice = "Output truncated"
        body.extend(_wrap_failure_line(Text(f"  {notice}", style="dim"), console, width))
    return Padding(Group(*body), (1, 0, 0, 0), expand=False)


def _mutation_body(preview: ToolPreview, width: int = 80) -> list[Any]:
    body: list[Any] = []
    if preview.label:
        body.append(Text(f"  {preview.label}", style="green"))
        body.append(Text(""))
    console = Console(width=max(20, width))
    for line in preview.lines:
        if not line.text:
            body.append(Text(""))
        elif line.style in {"add", "delete"}:
            body.extend(_diff_row(f"  {line.text}", line.style, width))
        else:
            text = Text(f"  {line.text}", style=_PREVIEW_LINE_STYLES[line.style])
            body.extend(text.wrap(console, max(1, width - 2), overflow="fold"))
    return body


def _failure_card(block: ToolBlock, preview: ToolPreview, expanded: bool, width: int) -> Any:
    _symbol, color = _tool_status_marker("error")
    title = Text.assemble(
        ("● ", color),
        (f"Failed (exit {_failure_code(block)}) ", "bold"),
        (preview.target, ""),
    )
    console = Console(width=max(20, width))
    body: list[Any] = [Text("")]
    body.extend(_wrap_failure_line(title, console, width))
    if expanded:
        lines = block.output.strip().splitlines()
    else:
        lines = [line.text for line in preview.lines]
    if lines:
        body.extend(_wrap_failure_line(Text("  └ output", style="dim"), console, width))
        for line in lines:
            body.extend(_wrap_failure_line(Text(f"    {line}", style="dim"), console, width))
    return Group(*body)


def _failure_code(block: ToolBlock) -> int:
    return block.exit_code if isinstance(block.exit_code, int) and not isinstance(block.exit_code, bool) else 1


def _wrap_failure_line(line: Text, console: Console, width: int) -> list[Text]:
    """Leave a spare terminal column while preserving long commands and output."""
    return list(line.wrap(console, max(1, width - 2), overflow="fold"))


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _tool_status_marker(status: str, is_error: bool = False) -> tuple[str, str]:
    """Shared status icon and color for tool cards and grouped tools."""
    if status == "running":
        return _spinner(), "green"
    if status == "waiting":
        return "●", "yellow"
    if status in {"error", "interrupted"} or is_error:
        return "●", "#888888"
    return "●", "green"


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
        # The TUI uses these ANSI styles for semantic diff rows and statuses;
        # an inherited NO_COLOR from the shell must not erase them.
        no_color=False,
        width=max(20, width),
    )
    console.print(renderable)
    return stream.getvalue().rstrip("\n")
