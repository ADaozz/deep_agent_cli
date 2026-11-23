from __future__ import annotations

import time
import re
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Sequence
from copy import copy
from dataclasses import dataclass, field
from io import StringIO
from typing import Any, TypeGuard
from zoneinfo import ZoneInfo

from prompt_toolkit import ANSI
from prompt_toolkit.formatted_text import FormattedText, StyleAndTextTuples, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.markdown import Markdown
from rich.padding import Padding
from rich.rule import Rule
from rich.segment import Segment
from rich.style import Style
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
    format_numbered_change,
    normalize_file_mutation,
    tool_group,
)
from agent.cli.highlighting import (
    HighlightMarkdown,
    code_renderer,
    parse_numbered_change,
)
from agent.cli.state import CliState, MessageBlock, SubtaskView, ToolBlock, TurnSummaryBlock
from agent.subagents.notifications import START_TOOL
from agent.cli.skills import skill_message_display
from agent.cli.display import DEFAULT_UI_DISPLAY_LIMITS, DEFAULT_UI_TIMEZONE, UiDisplayLimits
from agent.version import package_version


# Diff palette: exact hex so truecolor terminals keep the intended shades.
DIFF_ADD_BG = "#005f00"
DIFF_DELETE_BG = "#5f0000"
DIFF_ADD_MARKER = "#00af00"
DIFF_DELETE_MARKER = "#af0000"
DIFF_ADD_STAT = "#13a10e"
DIFF_DELETE_STAT = "#c50f1f"
DIFF_TRAIL_BG = "#0c0c0c"
# Shared style for tree leaders (Ran └ output, explore ├/└, multi-file └ path, …).
TREE_BRANCH_STYLE = "dim"

# PreviewLine style -> Rich style mapping for line-oriented mutation bodies.
_PREVIEW_LINE_STYLES: dict[str, str] = {
    "plain": "",
    "dim": "dim",
    "hunk": "bold cyan",
    "add": f"white on {DIFF_ADD_BG}",
    "delete": f"white on {DIFF_DELETE_BG}",
    "error": "red",
    "url": "bright_cyan",
}


def _tree_branch(
    label: str, *, glyph: str = "└", indent: str = "  ", label_style: str | None = None,
) -> Text:
    """Shared `└` / `├` row. Default matches Ran: whole line dim; pass label_style to keep label undimmed."""
    leader = f"{indent}{glyph} "
    if label_style is None:
        return Text(f"{leader}{label}", style=TREE_BRANCH_STYLE)
    return Text.assemble((leader, TREE_BRANCH_STYLE), (label, label_style))


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
    user_preview: str = ""

    def get_line(self, index: int) -> StyleAndTextTuples:
        if self._lines is None:
            self._lines = tuple(list(line) for line in split_lines(self.fragments))
        return self._lines[index]


@dataclass(frozen=True)
class TranscriptTurn:
    message_id: str
    preview: str
    start: int
    end: int  # Exclusive end of the bubble, before its trailing spacer.


@dataclass(frozen=True)
class TranscriptDocument:
    units: tuple[RenderedUnit, ...]
    starts: tuple[int, ...]
    line_count: int
    turns: tuple[TranscriptTurn, ...] = ()

    def turn_at(self, row: int) -> int:
        return bisect_right(self.turns, row, key=lambda turn: turn.start) - 1

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


def rendered_unit_from_ansi(text: str) -> RenderedUnit:
    """Build a unit whose line_count matches prompt_toolkit's ANSI split.

    ``text.count("\\n") + 1`` can disagree with ``split_lines(ANSI(text))`` when a
    newline lands inside a CSI sequence (incomplete codes, or Rich wrapping a
    literal escape mid-sequence). The transcript control then asks for a row the
    unit does not have and redraw crashes with ``tuple index out of range``.
    """
    fragments = to_formatted_text(ANSI(text))
    lines = tuple(list(line) for line in split_lines(fragments))
    return RenderedUnit(fragments, len(lines), lines)


def _render_unit(renderable: Any, width: int) -> RenderedUnit:
    return rendered_unit_from_ansi(_capture(renderable, width))


@dataclass
class TranscriptUnitMemo:
    """The previous structural pass, reused up to the first block that changed.

    Rebuilding a descriptor per block on every frame allocates enough
    short-lived objects in long sessions to trigger full GC passes.
    """

    settings: tuple[Any, ...] = ()
    header: TranscriptUnit | None = None
    blocks: list[Any] = field(default_factory=list)
    revisions: list[int] = field(default_factory=list)
    units: list[TranscriptUnit] = field(default_factory=list)
    # Exclusive end index into ``blocks`` of each unit, ascending.
    ends: list[int] = field(default_factory=list)

    def first_changed(self, blocks: list[Any]) -> int:
        """Index of the first block that is new, mutated or still animating."""
        limit = min(len(blocks), len(self.blocks))
        for index in range(limit):
            block = blocks[index]
            if (
                block is not self.blocks[index]
                or getattr(block, "revision", 0) != self.revisions[index]
                or (isinstance(block, ToolBlock) and block.status == "running")
            ):
                return index
        return limit


def transcript_units(
    state: CliState, timezone: ZoneInfo | None = None, width: int = 80,
    *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS,
    memo: TranscriptUnitMemo | None = None,
) -> list[TranscriptUnit]:
    """Structural pass over the timeline. Cheap: builds no Rich renderables."""
    timezone = timezone or ZoneInfo(DEFAULT_UI_TIMEZONE)
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
    if memo is None:
        units, _ends = _units_from(blocks, 0, state, timezone, width, limits)
        return [TranscriptUnit("header", (), lambda: Group(*_header())), *units]
    settings = (
        width, timezone.key, limits, state.thinking_collapsed, state.messages_expanded,
        state.tools_expanded, state.tasks_expanded,
    )
    kept = 0
    if memo.settings == settings:
        changed = memo.first_changed(blocks)
        # A unit ending right at a changed block may absorb it into its group.
        kept = bisect_left(memo.ends, changed) if changed < len(blocks) else bisect_right(memo.ends, changed)
    else:
        memo.settings = settings
    if memo.header is None:
        memo.header = TranscriptUnit("header", (), lambda: Group(*_header()))
    start = memo.ends[kept - 1] if kept else 0
    units, ends = _units_from(blocks, start, state, timezone, width, limits)
    del memo.units[kept:], memo.ends[kept:]
    memo.units.extend(units)
    memo.ends.extend(ends)
    memo.blocks = blocks
    memo.revisions = [getattr(block, "revision", 0) for block in blocks]
    return [memo.header, *memo.units]


def _units_from(
    blocks: list[Any], index: int, state: CliState, timezone: ZoneInfo, width: int, limits: UiDisplayLimits,
) -> tuple[list[TranscriptUnit], list[int]]:
    units: list[TranscriptUnit] = []
    ends: list[int] = []
    while index < len(blocks):
        block = blocks[index]
        previous = blocks[index - 1] if index else None
        # The user bubble already carries one blank row below it; the next unit
        # drops its own top margin so exactly one blank line separates them.
        after_user = isinstance(previous, MessageBlock) and previous.kind == "user"
        unit, index = _next_unit(blocks, index, block, after_user, state, timezone, width, limits)
        units.append(unit)
        ends.append(index)
    return units, ends


def _next_unit(
    blocks: list[Any], index: int, block: Any, after_user: bool,
    state: CliState, timezone: ZoneInfo, width: int, limits: UiDisplayLimits,
) -> tuple[TranscriptUnit, int]:
    """The unit starting at ``index`` and the index just past it."""
    if isinstance(block, MessageBlock):
        return _message_unit(
            block, state.thinking_collapsed, limits, state.messages_expanded, after_user=after_user,
        ), index + 1
    if isinstance(block, TurnSummaryBlock):
        return TranscriptUnit(
            key=("turn_summary", id(block)),
            fingerprint=timezone.key,
            build=lambda block=block: _turn_summary(block, timezone),
            owner=block,
        ), index + 1
    if _created_file_path(block) is not None:
        group = [block]
        while index + len(group) < len(blocks):
            candidate = blocks[index + len(group)]
            if _created_file_path(candidate) is None:
                break
            group.append(candidate)
        if len(group) > 1:
            return _create_group_unit(
                group, state.tools_expanded, width, limits=limits, after_user=after_user,
            ), index + len(group)
    if not state.tools_expanded and _is_explore(block):
        group = [block]
        index += 1
        while index < len(blocks):
            next_block = blocks[index]
            if not _is_explore(next_block):
                break
            group.append(next_block)
            index += 1
        return _explore_unit(group, width, limits=limits, after_user=after_user), index
    if _is_mergeable_modify(block):
        group = [block]
        while index + len(group) < len(blocks):
            candidate = blocks[index + len(group)]
            if not _is_mergeable_modify(candidate):
                break
            group.append(candidate)
        if len(group) > 1:
            return _modify_group_unit(group, width, limits=limits, after_user=after_user), index + len(group)
    if _is_mergeable_overwrite(block):
        group = [block]
        while index + len(group) < len(blocks):
            candidate = blocks[index + len(group)]
            if not _is_mergeable_overwrite(candidate):
                break
            group.append(candidate)
        if len(group) > 1:
            return _modify_group_unit(group, width, limits=limits, after_user=after_user), index + len(group)
    return _tool_unit(
        block, state.tools_expanded, width, limits=limits, after_user=after_user,
        tasks_expanded=state.tasks_expanded,
    ), index + 1


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
    block: ToolBlock, expanded: bool, width: int, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS,
    after_user: bool = False, tasks_expanded: bool = False,
) -> TranscriptUnit:
    return TranscriptUnit(
        key=("tool", id(block), expanded, tasks_expanded),
        fingerprint=(block.revision, expanded, tasks_expanded, _live_tick(block), limits, after_user),
        build=lambda: _tool(
            block, expanded, width, limits=limits, after_user=after_user, tasks_expanded=tasks_expanded,
        ),
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


def _is_mergeable_modify(block: Any) -> bool:
    mutation = normalize_file_mutation(block) if isinstance(block, ToolBlock) else None
    return bool(mutation and mutation.operation == "modify" and mutation.diff)


def _is_mergeable_overwrite(block: Any) -> bool:
    mutation = normalize_file_mutation(block) if isinstance(block, ToolBlock) else None
    return bool(mutation and mutation.operation == "overwrite")


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
    preview = file_tool_preview(mutation)
    return render_tool_preview(blocks[-1], preview, expanded=False, width=width, limits=limits, after_user=after_user)


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

    def __init__(self, timezone: ZoneInfo | None = None, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS, show_header: bool = True) -> None:
        self._show_header = show_header
        self._cache: dict[Any, tuple[Any, Any, RenderedUnit]] = {}
        self._units: dict[tuple[bool, bool], TranscriptUnitMemo] = {}
        self._documents: dict[tuple[bool, bool], tuple[tuple[TranscriptUnit, ...], TranscriptDocument]] = {}
        self._width = 0
        self._timezone = timezone or ZoneInfo(DEFAULT_UI_TIMEZONE)
        self._limits = limits

    def render_document(self, state: CliState, width: int) -> TranscriptDocument:
        if width != self._width:
            self.clear()
            self._width = width
        mode = (state.tools_expanded, state.tasks_expanded)
        memo = self._units.setdefault(mode, TranscriptUnitMemo())
        units = transcript_units(state, self._timezone, width, limits=self._limits, memo=memo)
        if not self._show_header:
            units = units[1:]
        live_ids = {id(block) for block in state.blocks}
        live = {"header"}
        live.update(key for key in self._cache if isinstance(key, tuple) and len(key) > 1 and key[1] in live_ids)
        for key in [key for key in self._cache if key not in live]:
            del self._cache[key]
        # Units before the first changed block are the same objects as last frame.
        signature = tuple(units)
        document_entry = self._documents.get(mode)
        if document_entry is not None and len(document_entry[0]) == len(signature) and all(
            previous is current for previous, current in zip(document_entry[0], signature)
        ):
            return document_entry[1]
        rendered_units: list[RenderedUnit] = []
        starts: list[int] = []
        turns: list[TranscriptTurn] = []
        previous = None
        line_count = 0
        for unit in units:
            block = unit.owner
            entry = self._cache.get(unit.key)
            if (
                entry is None
                or not _same_owner(entry[0], unit.owner)
                or entry[1] != unit.fingerprint
            ):
                rendered = _render_unit(unit.build(), width)
                if isinstance(block, MessageBlock) and block.kind == "user":
                    displayed, _ = skill_message_display(block.content)
                    rendered.user_preview = " ".join(displayed.split()) or ", ".join(
                        ref.filename for ref in block.attachments
                    )
                self._cache[unit.key] = (unit.owner, unit.fingerprint, rendered)
            else:
                rendered = entry[2]
            if isinstance(block, MessageBlock) and block.kind == "user" and not block.pending:
                after_user = isinstance(previous, MessageBlock) and previous.kind == "user"
                start = line_count + int(not after_user)
                turns.append(TranscriptTurn(block.id, rendered.user_preview, start, line_count + rendered.line_count - 1))
            previous = block
            starts.append(line_count)
            line_count += rendered.line_count
            rendered_units.append(rendered)
        document = TranscriptDocument(tuple(rendered_units), tuple(starts), line_count, tuple(turns))
        # Units hold their owners, so cached block ids cannot be recycled.
        self._documents[mode] = (signature, document)
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
        self._units.clear()
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
        highlighter = code_renderer.diff_from_unified(diff.full_diff, path=diff.path)
        parts.append(Text(""))
        if mutation.operation == "overwrite":
            parts.append(Text(f"write {diff.path}", style="bold"))
            parts.append(Text(added_line_label(diff.added or 0), style=DIFF_ADD_STAT))
            parts.append(Group(*(
                row
                for number, line in enumerate(diff.lines, 1)
                for row in _diff_row(
                    format_numbered_change(number, line.text), "add", width, highlighter=highlighter,
                )
            )))
        elif mutation.operation == "delete":
            parts.append(Text(f"delete {diff.path}", style="dim"))
        else:
            parts.append(Text(f"edit {diff.path} · snippet line numbers", style="dim"))
            parts.append(Group(*_numbered_diff(diff.full_diff, width, highlighter=highlighter)))
    if len(parts) == 2:
        parts.append(Text("Nothing to review.", style="dim"))
    return _capture(Group(*parts), width)


def _diff_row(
    content: str,
    kind: str,
    width: int,
    *,
    highlighter=None,
) -> list[Text]:
    """Wrap a diff line; add/delete highlight from line number through last visible char."""

    available = max(1, width - 1)  # Spare terminal column avoids auto-wrap.
    console = Console(width=max(20, width))
    if kind not in {"add", "delete"}:
        return list(Text(content, style=_PREVIEW_LINE_STYLES[kind]).wrap(console, available, overflow="fold"))

    bg = DIFF_ADD_BG if kind == "add" else DIFF_DELETE_BG
    marker_fg = DIFF_ADD_MARKER if kind == "add" else DIFF_DELETE_MARKER
    parsed = parse_numbered_change(content) if highlighter is not None else None
    if parsed is None or highlighter is None:
        text = Text(content)
        end = len(content.rstrip())
        matched = re.match(r"^ *([0-9]+)([-+])", content)
        if matched and matched.start(1) < end:
            text.stylize(f"white on {bg}", matched.start(1), end)
            text.stylize(f"{marker_fg} on {bg}", matched.start(2), matched.end(2))
        elif end:
            text.stylize(f"white on {bg}", 0, end)
    else:
        indent, number, diff_kind, body = parsed
        mark = "-" if diff_kind == "delete" else "+"
        text = Text()
        text.append(indent)
        text.append(str(number), Style(color="white", bgcolor=bg))
        text.append(mark, Style(color=marker_fg, bgcolor=bg))
        # format_numbered_change: space before body only when body is non-empty.
        if body:
            text.append(" ", Style(color="white", bgcolor=bg))
            styled = highlighter.style_body(diff_kind, number, body)
            styled.stylize(Style(bgcolor=bg), 0, len(styled))
            text.append_text(styled)
    rows = list(text.wrap(console, available, overflow="fold"))
    for row in rows:
        pad = max(0, available - row.cell_len)
        if pad:
            row.append(" " * pad, f"on {DIFF_TRAIL_BG}")
    return rows


def _numbered_diff(
    raw: str,
    width: int,
    *,
    highlighter=None,
    path: str | None = None,
) -> list[Text]:
    """Render source line numbers from unified hunk coordinates.

    Consecutive -/+ rows are flushed in unified card order: line number
    ascending, then add before delete (same as compact mutation previews).
    """
    active = highlighter or code_renderer.diff_from_unified(raw, path=path)
    result: list[Text] = []
    pending: list[tuple[str, str, int]] = []
    old_number = new_number = 0

    def flush_changes() -> None:
        nonlocal pending
        if not pending:
            return
        pending.sort(key=lambda item: (item[2], 0 if item[1] == "add" else 1))
        for content, kind, _number in pending:
            result.extend(_diff_row(content, kind, width, highlighter=active))
        pending = []

    for line in raw.splitlines():
        if line.startswith("@@"):
            flush_changes()
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match:
                old_number, new_number = map(int, match.groups())
            result.extend(_diff_row(line, "hunk", width))
        elif line.startswith(("---", "+++")):
            flush_changes()
            result.extend(_diff_row(line, "dim", width))
        elif line.startswith("-"):
            pending.append((format_numbered_change(old_number, line), "delete", old_number))
            old_number += 1
        elif line.startswith("+"):
            pending.append((format_numbered_change(new_number, line), "add", new_number))
            new_number += 1
        elif line.startswith(" "):
            flush_changes()
            result.extend(_diff_row(f"{new_number:>4} {line}", "dim", width))
            old_number += 1
            new_number += 1
        else:
            flush_changes()
            result.extend(_diff_row(line, "dim", width))
    flush_changes()
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


class _SkillMessageMarkdown(HighlightMarkdown):
    """保留用户正文的 Markdown 格式，在折行前为技能引用添加块样式。"""

    def __init__(self, text: str, labels: tuple[str, ...]) -> None:
        super().__init__(text, renderer=code_renderer)
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
    return _SkillMessageMarkdown(displayed, labels) if labels else code_renderer.markdown(text)


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
    if not messages:
        return ""
    heading = Text("Messages to be submitted after next tool call (press esc to restore to input, enter twice to interrupt and send immediately)", style="dim")
    rows = [heading]
    for message in messages:
        lines = message.splitlines() or [""]
        rows.append(Text("  ↳ " + lines[0]))
        rows.extend(Text("    " + line) for line in lines[1:])
    return _capture(Padding(Group(*rows), (0, 1)), width)


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
            items.append(Padding(
                code_renderer.markdown(block.content),
                (1 if block.thinking else top, 1, 0, 1),
            ))
    elif block.kind == "error":
        items.append(Padding(Text(f"Error: {block.content}", style="red"), (top, 1, 0, 1)))
    else:
        style = "white" if block.command_title or block.styled_spans or block.status_marker else "yellow"
        text = Text(block.content, style=style)
        for start, end, style in block.styled_spans:
            text.stylize(style, start, end)
        if block.status_marker:
            symbol, color = _tool_status_marker(block.status_marker)
            text = Text.assemble((f"{symbol} ", color), text)
        if block.command_title:
            panel = Group(Text(block.command_title, style="magenta"), Text(""), Padding(text, (0, 0, 0, 2)))
            items.append(Padding(panel, (top, 1, 0, 0)))
        else:
            items.append(Padding(text, (top, 1, 0, block.left_padding)))
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
        row = _tree_branch(f"… {hidden} more", glyph="├")
        row.truncate(max(1, width - 1), overflow="ellipsis")
        rows.append(row)
    shown = previews[-limits.explore_preview_items:]
    for index, preview in enumerate(shown):
        glyph = "└" if index == len(shown) - 1 else "├"
        row = _tree_branch(_preview_label(preview), glyph=glyph, label_style="")
        row.truncate(max(1, width - 1), overflow="ellipsis")
        rows.append(row)
    if failed:
        omitted = max(0, len(failed) - limits.explore_failure_items)
        if omitted:
            row = _tree_branch(f"… {omitted} more failed", glyph="├")
            row.truncate(max(1, width - 1), overflow="ellipsis")
            rows.append(row)
        for block, preview in failed[-limits.explore_failure_items:]:
            reason = preview.detail
            row = Text.assemble(
                (f"  ├ Failed (exit {_failure_code(block)}) ", TREE_BRANCH_STYLE),
                (_preview_label(preview), ""),
                (f": {reason}" if reason else "", TREE_BRANCH_STYLE),
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
    block: ToolBlock, expanded: bool, width: int = 80, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS,
    after_user: bool = False, tasks_expanded: bool = False,
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
        block, build_tool_preview(block, limits=limits), expanded=expanded, width=width, limits=limits,
        after_user=after_user, tasks_expanded=tasks_expanded,
    )


def render_tool_preview(
    block: ToolBlock, preview: ToolPreview, *, expanded: bool, width: int = 80,
    limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS, after_user: bool = False,
    tasks_expanded: bool = False,
) -> Any:
    """Generic tool card: status, spinner, colors, folding — no tool semantics."""
    if preview.command_execution is not None:
        return _command_card(block.output, preview.command_execution, expanded, width, limits=limits, after_user=after_user)
    if preview.kind == "failure":
        return _failure_card(block, preview, expanded, width, after_user=after_user)
    console = Console(width=max(20, width))
    if preview.kind == "task":
        # Same title a nested row uses. Depth only adds the tree prefix.
        title = _subagent_title(
            block.status, preview.target, is_error=block.is_error,
            background=block.name == START_TOOL,
        )
    else:
        symbol, color = _tool_status_marker(block.status, block.is_error)
        suffix = (
            " — waiting for input" if block.status == "waiting"
            else " — interrupted (completion unconfirmed)" if block.status == "interrupted"
            else " — cancelled" if block.status == "cancelled"
            else ""
        )
        verb = preview.verb
        if block.status == "running":
            verb = {"edit": "Editing", "write": "Writing", "delete": "Deleting"}.get(block.name, verb)
        summary = f"{verb} {preview.target}".strip()
        title = Text.assemble((f"{symbol} ", color), (summary, "bold"))
        mutation = preview.file_mutation
        counts = preview.detail if preview.detail.startswith("(+") and preview.detail.endswith(")") else ""
        if mutation is not None and mutation.diff is not None and counts:
            title.append(" (")
            title.append(f"+{mutation.diff.added or 0}", style=DIFF_ADD_STAT)
            title.append(" ")
            title.append(f"-{mutation.diff.deleted or 0}", style=DIFF_DELETE_STAT)
            title.append(")")
        elif preview.group == "mutation" and preview.detail:
            title.append(f" {preview.detail}", style="bold")
        title.append(suffix, style="dim")
    # Same wrap/indent as execute and failure cards: first visual row keeps the
    # status marker; wrapped rows indent under the title text.
    body: list[Any] = list(_wrap_card_line(title, console, width))
    if preview.kind == "task":
        output = block.output.strip()
        if output:
            body.extend(_subagent_result(
                output, console, width, expanded=tasks_expanded, limit=limits.tail_lines,
                error=block.is_error, text_at=2, spine="",
            ))
        body.extend(_subagent_tree(
            block.subtasks, console=console, width=width,
            expanded=tasks_expanded, limit=limits.tail_lines,
        ))
    elif preview.group == "mutation":
        body.extend(_mutation_body(preview, width))
    elif preview.lines:
        style = "dim" if not block.is_error else "red"
        for line in preview.lines:
            body.extend(_wrap_card_line(Text(f"  {line.text}", style=style), console, width))
    elif preview.group == "web" and not expanded and preview.detail:
        body.extend(_wrap_card_line(
            Text(f"  … {preview.detail} · {EXPAND_HINT}", style="dim"), console, width,
        ))
    else:
        output = block.output.strip()
        if output:
            error = block.is_error
            syntax = preview.syntax if preview.kind == "read" and not error else None
            body.extend(_output_tail(
                output.splitlines(), console, width,
                expanded=expanded, limit=limits.tail_lines, indent=2,
                style="dim" if not error else "red", earlier=False,
                filename=preview.target if syntax else None,
                language=syntax,
            ))
    return Padding(Group(*body), (0 if after_user else 1, 0, 0, 0), expand=False)


TASK_EXPAND_HINT = "Ctrl+A to expand"


def _subagent_title(status: str, description: str, *, is_error: bool = False, background: bool = False) -> Text:
    """The one SubAgent title. Every depth uses it; only the tree prefix changes."""
    failed = status not in {"cancelled", "interrupted", "waiting"} and (
        is_error or status in {"failed", "error"}
    )
    symbol, color = _tool_status_marker("error" if failed else status, failed)
    label = "Running SubAgent" if status == "running" else "SubAgent"
    summary = f"{label} · {description or 'Delegated task'}"
    if background:
        summary += " · background"
    title = Text.assemble((f"{symbol} ", color), (summary, "bold"))
    suffix = (
        " — waiting for input" if status == "waiting"
        else " — interrupted (completion unconfirmed)" if status == "interrupted"
        else " — cancelled" if status == "cancelled"
        else " — failed" if failed
        else ""
    )
    title.append(suffix, style="dim")
    return title


def _wrap_tree_line(line: Text, console: Console, width: int, *, text_at: int, spine: str) -> list[Text]:
    """Wrap a row and keep `│` in the branch column on every continuation."""
    available = max(1, width - 2)
    text_at = min(max(0, text_at), available - 1)
    prefix, content = line[:text_at], line[text_at:]
    wrapped = list(content.wrap(console, max(1, available - text_at), overflow="fold")) or [Text("")]
    rows: list[Text] = []
    gap = " " * max(0, text_at - len(spine))
    for index, part in enumerate(wrapped):
        if index == 0:
            rows.append(Text.assemble(prefix, part))
        elif spine:
            rows.append(Text.assemble(Text(spine, style=TREE_BRANCH_STYLE), Text(gap), part))
        else:
            rows.append(Text.assemble(Text(" " * text_at), part))
    return rows


def _subagent_result(
    text: str, console: Console, width: int, *, expanded: bool, limit: int,
    error: bool, text_at: int, spine: str,
) -> list[Text]:
    """Fold one task's own result the way other tool output folds, under its title."""
    style = "red" if error else "dim"
    gap = " " * max(0, text_at - len(spine))
    rows: list[Text] = []
    for line in text.splitlines() or [""]:
        assembled = Text.assemble(
            Text(spine, style=TREE_BRANCH_STYLE) if spine else Text(""),
            Text(f"{gap}{line}", style=style),
        )
        rows.extend(_wrap_tree_line(assembled, console, width, text_at=text_at, spine=spine))
    if expanded or len(rows) <= limit:
        return rows
    # Collapsed means the hint only. The tail used to stay visible under it.
    notice = Text.assemble(
        Text(spine, style=TREE_BRANCH_STYLE) if spine else Text(""),
        Text(f"{gap}… {len(rows)} output lines hidden · {TASK_EXPAND_HINT}", style="dim"),
    )
    return _wrap_tree_line(notice, console, width, text_at=text_at, spine=spine)


def _subagent_tree(
    nodes: Sequence[SubtaskView], *, console: Console, width: int, expanded: bool, limit: int, pad: str = "",
) -> list[Text]:
    """Nest the same SubAgent title and result. `pad` is only the ancestor spine."""
    lines: list[Text] = []
    last = len(nodes) - 1
    for index, node in enumerate(nodes):
        last_sibling = index == last
        branch = f"  {pad}{'└─' if last_sibling else '├─'} "
        # Under ├─ the bar continues through this row's result. └─ does not.
        spine = "" if last_sibling else f"  {pad}│"
        failed = node.status in {"failed", "error"}
        title = Text.assemble((branch, TREE_BRANCH_STYLE), _subagent_title(node.status, node.description, is_error=failed))
        text_at = len(branch) + 2
        lines.extend(_wrap_tree_line(title, console, width, text_at=text_at, spine=spine))
        body = (node.result or node.reason).strip()
        if body:
            lines.extend(_subagent_result(
                body, console, width, expanded=expanded, limit=limit,
                error=failed, text_at=text_at, spine=spine,
            ))
        if node.children:
            child_pad = pad + ("   " if last_sibling else "│  ")
            lines.extend(_subagent_tree(
                node.children, console=console, width=width, expanded=expanded, limit=limit, pad=child_pad,
            ))
    return lines


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
    command_lines = _wrap_card_line(title, console, width)
    if not expanded and len(command_lines) > limits.head_lines:
        hidden = len(command_lines) - limits.head_lines
        body.extend(command_lines[:limits.head_lines])
        notice = Text(f"  … {hidden} more command lines hidden · {EXPAND_HINT}{suffix}", style="dim")
        body.extend(_wrap_card_line(notice, console, width))
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
    if not lines and status not in {"running", "waiting", "interrupted"}:
        lines = ["(no output)"]
    if lines:
        body.extend(_wrap_card_line(_tree_branch("output"), console, width))
    body.extend(_output_tail(lines, console, width, expanded=expanded, limit=limits.tail_lines))

    if preview.truncated:
        if preview.log_error:
            notice = f"Full output could not be saved: {preview.log_error}"
        elif preview.log_path:
            notice = f"Output truncated · full output: {preview.log_path}"
        else:
            notice = "Output truncated"
        body.extend(_wrap_card_line(Text(f"  {notice}", style="dim"), console, width))
    return Padding(Group(*body), (0 if after_user else 1, 0, 0, 0), expand=False)


def _file_branch_row(content: str) -> Text | None:
    """Multi-file `└ path (+n -m)` using the shared tree branch style."""
    match = re.match(r"^└ (.+?)(?: \(\+(\d+) -(\d+)\))?$", content)
    if match is None:
        return None
    text = _tree_branch(match.group(1), indent="", label_style="")
    if match.group(2) is not None:
        text.append(" (")
        text.append(f"+{match.group(2)}", style=DIFF_ADD_STAT)
        text.append(" ")
        text.append(f"-{match.group(3)}", style=DIFF_DELETE_STAT)
        text.append(")")
    return text


def _mutation_highlighters(preview: ToolPreview):
    """Primary highlighter plus per-path map for multi-file └ sections."""
    mutation = preview.file_mutation
    by_path = {}
    if mutation is not None and mutation.diff is not None and mutation.diff.full_diff:
        primary = code_renderer.diff_from_unified(
            mutation.diff.full_diff, path=mutation.diff.path, language=preview.syntax,
        )
        by_path[mutation.path] = primary
        return primary, by_path
    path = mutation.path if mutation is not None else preview.target
    primary = code_renderer.diff_for_path(path, language=preview.syntax)
    if path:
        by_path[path] = primary
    return primary, by_path


def _mutation_body(preview: ToolPreview, width: int = 80) -> list[Any]:
    body: list[Any] = []
    console = Console(width=max(20, width))
    primary, by_path = _mutation_highlighters(preview)
    current = primary
    for line in preview.lines:
        if not line.text:
            body.append(Text(""))
        elif line.style in {"add", "delete"}:
            body.extend(_diff_row(f"  {line.text}", line.style, width, highlighter=current))
        else:
            header = _file_branch_row(line.text)
            if header is not None:
                match = re.match(r"^└ (.+?)(?: \(\+(\d+) -(\d+)\))?$", line.text)
                if match is not None:
                    path = match.group(1)
                    current = by_path.get(path) or code_renderer.diff_for_path(path)
                    by_path[path] = current
                text = Text.assemble(("  ", ""), header)
            else:
                text = Text(f"  {line.text}", style=_PREVIEW_LINE_STYLES[line.style])
            body.extend(text.wrap(console, max(1, width - 2), overflow="fold"))
    return body


def _failure_card(block: ToolBlock, preview: ToolPreview, expanded: bool, width: int, *, after_user: bool = False) -> Any:
    symbol, color = _tool_status_marker("error")
    title = Text.assemble(
        (f"{symbol} ", color),
        (f"Failed (exit {_failure_code(block)}) ", "bold"),
        (preview.target, ""),
    )
    console = Console(width=max(20, width))
    body: list[Any] = [] if after_user else [Text("")]
    body.extend(_wrap_card_line(title, console, width))
    if expanded:
        lines = block.output.strip().splitlines()
    else:
        lines = [line.text for line in preview.lines]
    if lines:
        body.extend(_wrap_card_line(_tree_branch("output"), console, width))
        for line in lines:
            body.extend(_wrap_card_line(Text(f"    {line}", style="dim"), console, width, indent=4))
    return Group(*body)


def _failure_code(block: ToolBlock) -> int:
    return block.exit_code if isinstance(block.exit_code, int) and not isinstance(block.exit_code, bool) else 1


def _output_tail(
    lines: list[str], console: Console, width: int, *, expanded: bool, limit: int,
    indent: int = 4, style: str = "dim", earlier: bool = True,
    filename: str | None = None, language: str | None = None,
) -> list[Text]:
    """Apply the output budget after terminal wrapping, preserving the full output."""
    use_syntax = language is not None or (
        filename is not None and code_renderer.syntax_for_path(filename) is not None
    )
    if use_syntax and style != "red":
        blob = "\n".join(lines)
        highlighted = code_renderer.render_code(blob, filename=filename, language=language)
        styled_lines = list(highlighted.split("\n"))
        # Pad if split under-counts blank trailing lines.
        while len(styled_lines) < len(lines):
            styled_lines.append(Text())
        rows: list[Text] = []
        for styled in styled_lines[:len(lines)]:
            line = Text(f"{' ' * indent}")
            line.append_text(styled)
            rows.extend(_wrap_card_line(line, console, width, indent=indent))
    else:
        rows = [
            row for line in lines
            for row in _wrap_card_line(Text(f"{' ' * indent}{line}", style=style), console, width, indent=indent)
        ]
    if expanded or len(rows) <= limit:
        return rows
    hidden = len(rows) - limit
    notice = f"… {hidden} {'earlier ' if earlier else ''}output lines hidden · {EXPAND_HINT}"
    return [
        *_wrap_card_line(Text(f"{' ' * indent}{notice}", style=style), console, width, indent=indent),
        *rows[-limit:],
    ]


def _wrap_card_line(line: Text, console: Console, width: int, *, indent: int = 2) -> list[Text]:
    """Keep the first prefix and indent continuations within the terminal margin.

    Shared by every tool card so titles, commands, and body lines wrap alike.
    """
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
    dot = "•"
    if status == "running":
        return _spinner(), "green"
    if status == "waiting":
        return dot, "yellow"
    if status in {"error", "interrupted", "cancelled"} or is_error:
        return dot, "#888888"
    return dot, "green"


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
        # Rich ignores an explicit width on TERM=dumb/unknown unless height is also set.
        height=25,
    )
    console.print(renderable)
    return stream.getvalue().rstrip("\n")
