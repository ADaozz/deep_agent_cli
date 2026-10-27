from __future__ import annotations

import time
import re
from bisect import bisect_right
from collections.abc import Callable, Sequence
from copy import copy
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
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.markdown import Markdown
from rich.padding import Padding
from rich.rule import Rule
from rich.segment import Segment
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from agent.cli.previews import (
    CommandExecutionPreview,
    EXPAND_HINT,
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
from agent.cli.skills import skill_message_display
from agent.config import DEFAULT_UI_DISPLAY_LIMITS, DEFAULT_UI_TIMEZONE, UiDisplayLimits
from agent.version import package_version

# PreviewLine style -> Rich style mapping for line-oriented mutation bodies.
_PREVIEW_LINE_STYLES: dict[str, str] = {
    "plain": "",
    "dim": "dim",
    "hunk": "bold cyan",
    # Use exact xterm-256 colors: muted RGB shades quantize to grayscale.
    "add": "white on #005f00",
    "delete": "white on #5f0000",
    "error": "red",
    "url": "bright_cyan",
}


def render_transcript(
    state: CliState,
    width: int,
    timezone: ZoneInfo | None = None,
    *,
    limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS,
) -> str:
    return "\n".join(
        _capture(unit.build(), width) for unit in transcript_units(
            state, timezone, width, limits=limits,
        )
    )


# --- Incremental transcript rendering -------------------------------------
#
# The transcript is split into render units: the header, one message,
# one tool, or one collapsed explore group. A unit is pushed through Rich and
# parsed into prompt_toolkit fragments exactly once, then replayed from cache
# until its fingerprint changes. While streaming, only the active block is
# dirty, so frozen history never pays for Markdown parsing again.


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


def transcript_units(
    state: CliState, timezone: ZoneInfo | None = None, width: int = 80,
    *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS,
) -> list[TranscriptUnit]:
    """Structural pass over the timeline. Cheap: builds no Rich renderables."""
    timezone = timezone or ZoneInfo(DEFAULT_UI_TIMEZONE)
    units: list[TranscriptUnit] = [
        TranscriptUnit("header", (), lambda: Group(*_header())),
    ]
    index = 0
    # Streaming can create an assistant block before any visible text arrives.
    # An empty Rich Group still occupies a row, adding a second gap above the
    # editor. Keep these placeholders out of the layout until they have content.
    blocks = [
        block for block in state.blocks
        if not (
            isinstance(block, MessageBlock) and block.kind == "assistant"
            and not block.content.strip() and not block.thinking.strip()
        )
    ]
    while index < len(blocks):
        block = blocks[index]
        previous = blocks[index - 1] if index else None
        # The user bubble already carries one blank row below it; the next unit
        # drops its own top margin so exactly one blank line separates them.
        after_user = isinstance(previous, MessageBlock) and previous.kind == "user"
        if isinstance(block, MessageBlock):
            units.append(_message_unit(
                block, state.thinking_collapsed, limits, state.messages_expanded,
                after_user=after_user,
            ))
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
        if _created_file_path(block) is not None:
            group = [block]
            while index + len(group) < len(blocks):
                candidate = blocks[index + len(group)]
                if _created_file_path(candidate) is None:
                    break
                group.append(candidate)
            if len(group) > 1:
                units.append(_create_group_unit(group, state.tools_expanded, width, limits=limits, after_user=after_user))
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
            units.append(_explore_unit(group, width, limits=limits, after_user=after_user))
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
                units.append(_modify_group_unit(group, width, limits=limits, after_user=after_user))
                index += len(group)
                continue
        units.append(_tool_unit(block, state.tools_expanded, width, limits=limits, after_user=after_user))
        index += 1
    return units


def _message_unit(
    block: MessageBlock, thinking_collapsed: bool, limits: UiDisplayLimits, messages_expanded: bool = False,
    *, after_user: bool = False,
) -> TranscriptUnit:
    return TranscriptUnit(
        key=("message", id(block)),
        fingerprint=(block.revision, thinking_collapsed, limits, messages_expanded if block.kind == "user" else False, after_user),
        build=lambda: Group(*_message(
            block, thinking_collapsed, limits=limits, messages_expanded=messages_expanded, after_user=after_user,
        )),
        owner=block,
    )


def _tool_unit(
    block: ToolBlock, expanded: bool, width: int, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS, after_user: bool = False,
) -> TranscriptUnit:
    return TranscriptUnit(
        key=("tool", id(block), expanded),
        fingerprint=(block.revision, expanded, _live_tick(block), limits, after_user),
        build=lambda: _tool(block, expanded, width, limits=limits, after_user=after_user),
        owner=block,
    )


def _explore_unit(
    blocks: list[ToolBlock], width: int, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS, after_user: bool = False,
) -> TranscriptUnit:
    members = tuple(blocks)
    tick = next((item for item in (_live_tick(block) for block in members) if item is not None), None)
    return TranscriptUnit(
        key=("explore", id(members[0])),
        fingerprint=(tuple(item.revision for item in members), tick, limits, after_user),
        build=lambda: _explore_group(members, width, limits=limits, after_user=after_user),
        owner=members,
    )


def _mergeable_modify_path(block: Any) -> str | None:
    mutation = normalize_file_mutation(block) if isinstance(block, ToolBlock) else None
    return mutation.path if mutation and mutation.operation == "modify" and mutation.diff else None


def _created_file_path(block: Any) -> str | None:
    mutation = normalize_file_mutation(block) if isinstance(block, ToolBlock) else None
    return mutation.path if mutation and mutation.operation == "create" else None


def _create_group_unit(
    blocks: list[ToolBlock], expanded: bool, width: int, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS, after_user: bool = False,
) -> TranscriptUnit:
    members = tuple(blocks)
    return TranscriptUnit(
        key=("create_group", id(members[0]), expanded),
        fingerprint=tuple(block.revision for block in members) + (tuple(id(block) for block in members), expanded, limits, after_user),
        build=lambda: _create_group(members, expanded, width, limits=limits, after_user=after_user),
        owner=members,
    )


def _create_group(
    blocks: Sequence[ToolBlock], expanded: bool, width: int, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS, after_user: bool = False,
) -> Any:
    mutations = [item for block in blocks if (item := normalize_file_mutation(block, limits=limits)) is not None]
    mutation = aggregate_file_mutations(mutations, expanded=expanded, limits=limits)
    if len(mutation.paths) == 1:
        return _tool(blocks[-1], expanded, width, limits=limits, after_user=after_user)
    title = Text.assemble(("• ", "green"), (f"Created {len(mutation.paths)} files", "bold"))
    rows = [Text(line.text, style=_PREVIEW_LINE_STYLES[line.style]) for line in mutation.compact_lines]
    for row in (title, *rows):
        row.truncate(max(1, width - 1), overflow="ellipsis")
    return Group(*([] if after_user else [Text("")]), title, *rows)


def _modify_group_unit(
    blocks: list[ToolBlock], width: int, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS, after_user: bool = False,
) -> TranscriptUnit:
    members = tuple(blocks)
    return TranscriptUnit(
        key=("modify_group", id(members[0])),
        fingerprint=tuple(block.revision for block in members) + (tuple(id(block) for block in members), limits, after_user),
        build=lambda: _modify_group(members, width, limits=limits, after_user=after_user),
        owner=members,
    )


def _modify_group(
    blocks: Sequence[ToolBlock], width: int, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS, after_user: bool = False,
) -> Any:
    mutations = [item for block in blocks if (item := normalize_file_mutation(block, limits=limits)) is not None]
    mutation = aggregate_file_mutations(mutations, limits=limits)
    preview = file_tool_preview(mutation)
    return render_tool_preview(blocks[-1], preview, expanded=False, width=width, limits=limits, after_user=after_user)


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

    def __init__(self, timezone: ZoneInfo | None = None, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS) -> None:
        self._cache: dict[Any, tuple[Any, Any, RenderedUnit]] = {}
        self._documents: dict[bool, tuple[Any, tuple[Any, ...], TranscriptDocument]] = {}
        self._width = 0
        self._timezone = timezone or ZoneInfo(DEFAULT_UI_TIMEZONE)
        self._limits = limits

    def render_document(self, state: CliState, width: int) -> TranscriptDocument:
        if width != self._width:
            self.clear()
            self._width = width
        units = transcript_units(state, self._timezone, width, limits=self._limits)
        live_ids = {id(block) for block in state.blocks}
        live = {"header"}
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


def render_interaction_viewport(controller: Any, width: int, height: int, offset: int) -> tuple[str, int]:
    """Scroll only the options; headings and keyboard hints stay in place."""
    if controller is None or height <= 0:
        return "", 0
    header, options, footer = controller.render_sections()
    if not options:
        return render_interaction(controller, width), 0
    heading = _capture(Group(*header), width).splitlines()
    hints = _capture(Group(*footer), width).splitlines()
    rows = [_capture(option, width).splitlines() for option in options]
    option_rows = [line for option in rows for line in option]
    # Reserve list space even when a long question fills a small terminal.
    hint_height = min(len(hints), max(0, height - 2))
    list_minimum = min(len(option_rows), max(1, height // 2))
    heading_height = min(len(heading), max(0, height - hint_height - list_minimum))
    list_height = max(1, height - hint_height - heading_height)
    selected = min(controller.option_index, len(rows) - 1)
    start = sum(len(option) for option in rows[:selected])
    end = start + len(rows[selected])
    offset = max(0, min(offset, max(0, len(option_rows) - list_height)))
    if start < offset:
        offset = start
    elif end > offset + list_height:
        offset = min(start, end - list_height)
    if heading_height < len(heading) and heading_height > 1:
        heading = [*heading[:heading_height - 1], _capture(Text("…", style="dim"), width)]
    visible = heading[:heading_height] + option_rows[offset:offset + list_height] + hints[:hint_height]
    return "\n".join(visible[:height]), offset


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


@dataclass(frozen=True)
class _ThinkingDetails:
    """在已扣除缩进的实际宽度内折行，再截取思考正文的末尾显示行。"""

    content: str
    collapsed: bool
    tail_lines: int

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        lines = self.content.splitlines() or [self.content]
        if not self.collapsed:
            for line in lines:
                yield Text(line, style="italic #888888")
            return
        rows = []
        for line in lines:
            rows.extend(Text(line, style="italic #888888").wrap(
                console, max(1, options.max_width), overflow="fold",
            ))
        hidden = max(0, len(rows) - self.tail_lines)
        if hidden:
            yield Text(
                f"… {hidden} earlier thinking lines hidden · Ctrl+T to expand",
                style="#888888", no_wrap=True, overflow="ellipsis",
            )
        for row in rows[-self.tail_lines:]:
            row.no_wrap = True
            row.overflow = "crop"
            yield row


class _SkillMessageMarkdown(Markdown):
    """保留用户正文的 Markdown 格式，在折行前为技能引用添加块样式。"""

    def __init__(self, text: str, labels: tuple[str, ...]) -> None:
        super().__init__(text)
        self.inlines = self.inlines | {"skill"}
        pattern = re.compile("(" + "|".join(re.escape(label) for label in labels) + ")")

        def style_tokens(tokens):
            styled = []
            for token in tokens:
                if token.children:
                    token.children = style_tokens(token.children)
                if token.type != "text":
                    styled.append(token)
                    continue
                for part in pattern.split(token.content):
                    if not part:
                        continue
                    item = copy(token)
                    item.content = part
                    if part in labels:
                        item.type, item.tag = "skill_inline", "skill"
                    styled.append(item)
            return styled

        self.parsed = style_tokens(self.parsed)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        with console.use_theme(Theme({"markdown.skill": "bold #e8f4ff on #254b70"})):
            yield from super().__rich_console__(console, options)


def _user_message_content(text: str) -> Markdown:
    displayed, labels = skill_message_display(text)
    return _SkillMessageMarkdown(displayed, labels) if labels else Markdown(text)


@dataclass(frozen=True)
class _UserMessagePreview:
    content: Any
    expanded: bool

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        rows = console.render_lines(self.content, options.update(overflow="fold", no_wrap=False), pad=False)
        hidden = max(0, len(rows) - 3)
        for row in rows if self.expanded else rows[:3]:
            yield from row
            yield Segment.line()
        if hidden:
            hint = "Ctrl+B to collapse" if self.expanded else f"… Ctrl+B to expand · {hidden} more lines"
            yield Text(hint, style="dim", no_wrap=True, overflow="ellipsis")


def render_queued_messages(messages: list[str], width: int) -> str:
    return _capture(Padding(Group(*(Text(f"{index}. {text}") for index, text in enumerate(messages, 1))), (0, 1)), width)


def _message(
    block: MessageBlock, thinking_collapsed: bool, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS,
    *, messages_expanded: bool = False, after_user: bool = False,
) -> list[Any]:
    items: list[Any] = []
    top = 0 if after_user else 1
    if block.kind == "user":
        style = "white on #404040" if block.pending else "white on #303030"
        label = f"⟳ {block.content}" if block.pending else block.content
        message = Table.grid(expand=True, padding=0)
        message.add_column(width=2, no_wrap=True)
        message.add_column(ratio=1)
        message.add_row(Text("› "), _UserMessagePreview(_user_message_content(label), messages_expanded))
        content: list[Any] = [message]
        for index, ref in enumerate(block.attachments, 1):
            content.append(Text(
                f"▣ Image #{index}  {ref.filename} · {_format_bytes(ref.size)}",
                style="bright_cyan",
            ))
        box = Padding(Group(*content), (1, 1, 1, 1), style=style)
        items.append(Padding(box, (top, 0, 1, 0)))
    elif block.kind == "assistant":
        if block.thinking:
            body: list[Any] = [Text("Thinking", style="bold #888888")]
            details = _ThinkingDetails(block.thinking, thinking_collapsed, limits.tail_lines)
            body.append(Padding(details, (0, 0, 0, 2)))
            items.append(Padding(Group(*body), (top, 1, 0, 1)))
        if block.content:
            items.append(Padding(Markdown(block.content), (1 if block.thinking else top, 1, 0, 1)))
    elif block.kind == "error":
        items.append(Padding(Text(f"Error: {block.content}", style="red"), (top, 1, 0, 1)))
    else:
        text = Text(block.content, style="white" if block.styled_spans else "yellow")
        for start, end, style in block.styled_spans:
            text.stylize(style, start, end)
        items.append(Padding(text, (top, 1, 0, 1)))
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
    return Padding(Text(f"Worked for {duration} · {finished}", style="dim"), (0, 1, 0, 1))


def _header() -> list[Any]:
    title = Text.assemble(
        ("DeepAgentCLI", "bold bright_cyan"),
        (f" v{package_version()}", "dim"),
        ("  terminal coding agent", "dim"),
    )
    items: list[Any] = [title]
    items.extend([Rule(style="#303030"), Text("")])
    return items


def _is_explore(block: Any) -> TypeGuard[ToolBlock]:
    return isinstance(block, ToolBlock) and tool_group(block.name) == "explore"


def _preview_label(preview: ToolPreview) -> str:
    return preview.summary or f"{preview.verb} {preview.target}".strip() or preview.kind


def _explore_group(
    blocks: Sequence[ToolBlock], width: int = 80, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS, after_user: bool = False,
) -> Any:
    previews = [build_tool_preview(block, limits=limits) for block in blocks]
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
    verb = "Exploring" if running else "Explore" if waiting or interrupted or failed else "Explored"
    title = Text.assemble(
        (f"{symbol} ", color),
        (f"{verb} {count} {'item' if count == 1 else 'items'}", "bold"),
        (f" · {len(failed)} failed", "dim") if failed else ("", ""),
        (f" · {interrupted} interrupted", "dim") if interrupted else ("", ""),
        (suffix, "dim"),
    )
    title.truncate(max(1, width - 1), overflow="ellipsis")
    rows: list[Text] = []
    hidden = count - limits.explore_preview_items
    if hidden > 0:
        row = Text(f"  ├ … {hidden} more", style="dim")
        row.truncate(max(1, width - 1), overflow="ellipsis")
        rows.append(row)
    shown = previews[-limits.explore_preview_items:]
    for index, preview in enumerate(shown):
        branch = "└" if index == len(shown) - 1 else "├"
        row = Text.assemble((f"  {branch} ", "dim"), (_preview_label(preview), ""))
        row.truncate(max(1, width - 1), overflow="ellipsis")
        rows.append(row)
    if failed:
        omitted = max(0, len(failed) - limits.explore_failure_items)
        if omitted:
            row = Text(f"  ├ … {omitted} more failed", style="dim")
            row.truncate(max(1, width - 1), overflow="ellipsis")
            rows.append(row)
        for block, preview in failed[-limits.explore_failure_items:]:
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
    return Group(*([] if after_user else [Text("")]), title, *rows)


def _tool(
    block: ToolBlock, expanded: bool, width: int = 80, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS, after_user: bool = False,
) -> Any:
    if block.name.startswith("mcp__"):
        from agent.cli.interactions import mcp_arguments_preview, mcp_display_text
        symbol, color = _tool_status_marker(block.status, block.is_error)
        body = [
            Text(f"{symbol} {mcp_display_text(block.name)} — {block.status}", style=color),
            Text("Execution: Host (outside sandbox)", style="dim"),
            Text("Arguments:\n" + mcp_arguments_preview(block.arguments, expanded=expanded)),
        ]
        if block.output and block.status != "waiting":
            output = mcp_display_text(block.output).splitlines()
            limit = len(output) if expanded else limits.tail_lines
            if len(output) > limit:
                output = [f"… output omitted · {EXPAND_HINT}", *output[-limit:]]
            body.append(Text("\n".join(output), style="dim"))
        return Padding(Group(*body), (0 if after_user else 1, 0, 0, 0), expand=False)
    return render_tool_preview(
        block, build_tool_preview(block, limits=limits), expanded=expanded, width=width, limits=limits, after_user=after_user,
    )


def render_tool_preview(
    block: ToolBlock, preview: ToolPreview, *, expanded: bool, width: int = 80,
    limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS, after_user: bool = False,
) -> Any:
    """Generic tool card: status, spinner, colors, folding — no tool semantics."""
    if preview.command_execution is not None:
        return _command_card(block.output, preview.command_execution, expanded, width, limits=limits, after_user=after_user)
    if preview.kind == "failure":
        return _failure_card(block, preview, expanded, width, after_user=after_user)
    symbol, color = _tool_status_marker(block.status, block.is_error)
    suffix = (
        " — waiting for input" if block.status == "waiting"
        else " — interrupted (completion unconfirmed)" if block.status == "interrupted"
        else ""
    )
    verb = preview.verb
    if block.status == "running":
        verb = {"edit_file": "Editing", "write_file": "Writing", "delete": "Deleting"}.get(block.name, verb)
    summary = f"{verb} {preview.target}".strip()
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
            limit = len(lines) if expanded else limits.tail_lines
            if len(lines) > limit:
                skipped = len(lines) - limit
                lines = [f"… {skipped} output lines hidden · {EXPAND_HINT}", *lines[-limit:]]
            body.append(Text(
                "\n".join(f"  {line}" for line in lines),
                style="dim" if not block.is_error else "red",
            ))
    return Padding(Group(*body), (0 if after_user else 1, 0, 0, 0), expand=False)


def _command_card(
    output: str, preview: CommandExecutionPreview, expanded: bool, width: int, *,
    limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS, after_user: bool = False,
) -> Any:
    """Render the current output snapshot; every delta rebuilds this card."""
    status = preview.status
    verb = {
        "running": "Running", "waiting": "execute", "interrupted": "execute",
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
    command_lines = _wrap_failure_line(title, console, width)
    if not expanded and len(command_lines) > limits.head_lines:
        hidden = len(command_lines) - limits.head_lines
        body.extend(command_lines[:limits.head_lines])
        notice = Text(f"  … {hidden} more command lines hidden · {EXPAND_HINT}{suffix}", style="dim")
        body.extend(_wrap_failure_line(notice, console, width))
    else:
        body.extend(command_lines)

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
    if not expanded and len(lines) > limits.tail_lines:
        hidden = len(lines) - limits.tail_lines
        lines = [f"… {hidden} earlier output lines hidden · {EXPAND_HINT}", *lines[-limits.tail_lines:]]
    if not lines and status not in {"running", "waiting", "interrupted"}:
        lines = ["(no output)"]
    if lines:
        body.extend(_wrap_failure_line(Text("  └ output", style="dim"), console, width))
    for line in lines:
        body.extend(_wrap_failure_line(Text(f"    {line}", style="dim"), console, width, indent=4))

    if preview.truncated:
        if preview.log_error:
            notice = f"Full output could not be saved: {preview.log_error}"
        elif preview.log_path:
            notice = f"Output truncated · full output: {preview.log_path}"
        else:
            notice = "Output truncated"
        body.extend(_wrap_failure_line(Text(f"  {notice}", style="dim"), console, width))
    return Padding(Group(*body), (0 if after_user else 1, 0, 0, 0), expand=False)


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


def _failure_card(block: ToolBlock, preview: ToolPreview, expanded: bool, width: int, *, after_user: bool = False) -> Any:
    _symbol, color = _tool_status_marker("error")
    title = Text.assemble(
        ("• ", color),
        (f"Failed (exit {_failure_code(block)}) ", "bold"),
        (preview.target, ""),
    )
    console = Console(width=max(20, width))
    body: list[Any] = [] if after_user else [Text("")]
    body.extend(_wrap_failure_line(title, console, width))
    if expanded:
        lines = block.output.strip().splitlines()
    else:
        lines = [line.text for line in preview.lines]
    if lines:
        body.extend(_wrap_failure_line(Text("  └ output", style="dim"), console, width))
        for line in lines:
            body.extend(_wrap_failure_line(Text(f"    {line}", style="dim"), console, width, indent=4))
    return Group(*body)


def _failure_code(block: ToolBlock) -> int:
    return block.exit_code if isinstance(block.exit_code, int) and not isinstance(block.exit_code, bool) else 1


def _wrap_failure_line(line: Text, console: Console, width: int, *, indent: int = 2) -> list[Text]:
    """Keep the first prefix and indent continuations within the terminal margin."""
    available = max(1, width - 2)
    indent = min(indent, available - 1)
    prefix, content = line[:indent], line[indent:]
    wrapped = list(content.wrap(console, available - indent, overflow="fold"))
    return [
        Text.assemble(prefix if index == 0 else Text(" " * indent, style=line.style), part)
        for index, part in enumerate(wrapped)
    ]


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _tool_status_marker(status: str, is_error: bool = False) -> tuple[str, str]:
    """Shared status icon and color for tool cards and grouped tools."""
    if status == "running":
        return _spinner(), "green"
    if status == "waiting":
        return "•", "yellow"
    if status in {"error", "interrupted"} or is_error:
        return "•", "#888888"
    return "•", "green"


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
