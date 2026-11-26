"""Tool preview IR: tool display semantics live here, rendering stays generic.

ToolBlock → tool-specific Preview Builder → ToolPreview → Generic Renderer.

A ToolPreview describes *what* a tool card shows: verb, target operand,
content lines, hint action. The renderer owns *how* it looks: spinner, status
colors, terminal width, folding limits. ToolPreview is a UI-derived object and
is never persisted; sessions keep the raw ToolMessage artifact instead.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from difflib import unified_diff
from pathlib import PurePosixPath
from typing import Any, Literal

from agent.cli.state import ToolBlock
from agent.cli.highlighting import code_renderer
from agent.subagents.notifications import is_background_launch, is_task_tool
from agent.execution.mutations import FileMutationOperation
from agent.cli.display import DEFAULT_UI_DISPLAY_LIMITS, UiDisplayLimits
from agent.tools.catalog import is_mutation_tool as catalog_is_mutation_tool
from agent.tools.catalog import tool_preview_group


PreviewStyle = Literal["plain", "dim", "add", "delete", "error", "url"]
PreviewGroup = Literal["explore", "mutation", "command", "web", "other"]
PreviewAction = Literal["none", "expand"]
CommandExecutionStatus = Literal[
    "running", "succeeded", "failed", "timeout", "cancelled",
    "spawn_error", "interrupted", "waiting",
]

# Hint vocabulary. File changes, tool output, and SubAgent results all expand
# in place. Ctrl+R and Ctrl+O share the file-body path; Ctrl+A is the task path.
EXPAND_HINT = "Ctrl+O to expand"
REVIEW_HINT = "Ctrl+R to review"
TASK_EXPAND_HINT = "Ctrl+A to expand"


def hidden_output_notice(hidden: int, *, hint: str = EXPAND_HINT, earlier: bool = False) -> str:
    """The one fold sentence. Callers decide whether the tail stays under it."""
    when = "earlier " if earlier else ""
    return f"… {hidden} {when}output lines hidden · {hint}"

@dataclass(frozen=True)
class PreviewLine:
    text: str
    style: PreviewStyle = "plain"


@dataclass(frozen=True)
class ToolPreview:
    kind: str
    verb: str
    target: str = ""
    # Full heading after the status marker. The renderer does not invent a verb.
    title: str = ""
    # Human-input waiting already says Asking; do not append another status suffix.
    omit_status_suffix: bool = False
    # Title suffix (edit: "(+2 -2)").
    detail: str = ""
    # One-line label for compact lists (explore group: 'Read file.py').
    summary: str = ""
    lines: tuple[PreviewLine, ...] = ()
    group: PreviewGroup = "other"
    action: PreviewAction = "none"
    syntax: str | None = None
    file_mutation: FileMutationPreview | None = None
    command_execution: CommandExecutionPreview | None = None
    # Extra lines above folded output. Empty for every non-MCP tool.
    collapsed_lines: tuple[PreviewLine, ...] = ()
    expanded_lines: tuple[PreviewLine, ...] = ()
    # Sanitized body. None keeps ToolBlock.output.
    display_output: str | None = None


@dataclass(frozen=True)
class CommandExecutionPreview:
    """Command result semantics; live output remains in ToolBlock.output."""

    command: str
    status: CommandExecutionStatus
    exit_code: int | None = None
    termination_reason: str | None = None
    truncated: bool = False
    log_path: str | None = None
    log_error: str | None = None
    execution_id: str | None = None
    interactive: bool = False


@dataclass(frozen=True)
class _ChangedLine:
    """One real -/+ line of a unified diff at its actual 1-based file line."""

    text: str
    kind: Literal["add", "delete"]
    number: int


@dataclass(frozen=True)
class DiffPreview:
    """Mutation domain model shared by the compact card and the full review."""

    path: str
    added: int | None = None
    deleted: int | None = None
    full_diff: str = ""
    # Every changed line, parsed once here so cards do not re-parse per view.
    changed: tuple[_ChangedLine, ...] = ()


@dataclass(frozen=True)
class FileSection:
    """One path inside a file card. The renderer draws the tree."""

    path: str
    added: int = 0
    deleted: int = 0
    # Body rows, including the gutter ⋮ when this section dropped lines.
    lines: tuple[PreviewLine, ...] = ()
    # Changed lines this section did not show.
    hidden: int = 0
    # True when this section has a real diff (false for empty overwrite or edit without position metadata).
    has_diff: bool = False


@dataclass(frozen=True)
class FileMutationPreview:
    """A file change independent of the tool that produced it."""

    path: str
    operation: FileMutationOperation
    diff: DiffPreview | None = None
    # Footer notice outside the tree, after all section rows.
    footer: tuple[PreviewLine, ...] = ()
    # One section per path, including single-file cards. The renderer owns tree
    # chrome so PreviewLine.text never stores a connection glyph.
    sections: tuple[FileSection, ...] = ()

    @property
    def paths(self) -> tuple[str, ...]:
        """Ordered paths represented by this card, derived from ``sections``."""
        return tuple(section.path for section in self.sections) if self.sections else (self.path,)

    @property
    def compact_lines(self) -> tuple[PreviewLine, ...]:
        """Flat view for compact cards: single-file body, then the shared footer."""
        body = self.sections[0].lines if len(self.sections) == 1 else ()
        return (*body, *self.footer)


def _changed_line_sort_key(line: _ChangedLine) -> tuple[int, int]:
    """Sort by actual file line ascending, add before delete."""
    return (line.number, 0 if line.kind == "add" else 1)


PreviewBuilder = Callable[[ToolBlock], ToolPreview]


def _operand(args: dict[str, Any]) -> str:
    """file_path/path label (with offset range), else preferred scalar args."""
    path = args.get("file_path") or args.get("path")
    if path:
        label = str(PurePosixPath(str(path)))
        offset = args.get("offset")
        limit = args.get("limit")
        if offset is not None or limit is not None:
            start = int(offset or 1)
            label += f":{start}-{start + int(limit) - 1}" if limit else f":{start}"
        return label
    values: list[str] = []
    for key in ("query", "pattern", "to", "subject"):
        value = args.get(key)
        if isinstance(value, (str, int, float, bool)) and str(value):
            values.append(shlex.quote(str(value))[:80])
        if len(values) == 2:
            break
    return " ".join(values)


def _mutation_path(name: str, args: dict[str, Any]) -> str:
    path = args.get("file_path")
    if not path:
        return "file"
    label = str(PurePosixPath(str(path)))
    if name == "write" and label.startswith("/"):
        label = label.lstrip("/")
    return label


def _diff_name(side: str, path: str) -> str:
    if path.startswith("/"):
        return f"{side}{path}"
    return f"{side}/{path}"


def _changed_lines(raw: str) -> list[_ChangedLine]:
    """Real -/+ lines with their file line and diff order; context is dropped."""
    changed: list[_ChangedLine] = []
    seen_hunk = False
    old_number = new_number = 0
    for line in raw.splitlines():
        if line.startswith("@@"):
            seen_hunk = True
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match:
                old_number, new_number = map(int, match.groups())
            continue
        if not seen_hunk:
            continue  # --- / +++ header lines before the first hunk
        if line.startswith("+"):
            changed.append(_ChangedLine(line, "add", new_number))
            new_number += 1
        elif line.startswith("-"):
            changed.append(_ChangedLine(line, "delete", old_number))
            old_number += 1
        elif line.startswith(" "):
            old_number += 1
            new_number += 1
    return changed


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(,\d+)? \+(\d+)(,\d+)? @@(.*)$")


def _shift_hunk_numbers(raw: str, *, old_offset: int, new_offset: int) -> str:
    """Move snippet-relative hunk numbers to the actual file position.

    ``old_offset`` shifts the pre-edit side, ``new_offset`` the post-edit side;
    a later match in the same edit moves by the line delta of earlier matches.
    """
    if not old_offset and not new_offset:
        return raw

    def replace(match: re.Match[str]) -> str:
        old = int(match.group(1))
        new = int(match.group(3))
        old = old + old_offset if old > 0 else old
        new = new + new_offset if new > 0 else new
        return f"@@ -{old}{match.group(2) or ''} +{new}{match.group(4) or ''} @@{match.group(5)}"

    return "\n".join(
        _HUNK_HEADER.sub(replace, line) if line.startswith("@@") else line
        for line in raw.splitlines()
    )


def _select_changed_lines(
    changed: list[_ChangedLine], *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS, full: bool = False,
) -> list[_ChangedLine]:
    """Sort every change by file position, then apply the head-lines budget.

    With a budget of at least two, show both - and + when both sides exist.
    Display order is line number ascending, add before delete.
    ``full`` returns every changed line in that order.
    """
    ordered = sorted(changed, key=_changed_line_sort_key)
    if full or len(ordered) <= limits.head_lines:
        return ordered
    selected = list(ordered[:limits.head_lines])
    if limits.head_lines >= 2 and len({line.kind for line in ordered}) >= 2:
        if "add" not in {line.kind for line in selected}:
            replacement = next(line for line in ordered if line.kind == "add")
            for index in range(len(selected) - 1, -1, -1):
                if selected[index].kind == "delete":
                    selected[index] = replacement
                    break
        elif "delete" not in {line.kind for line in selected}:
            replacement = next(line for line in ordered if line.kind == "delete")
            for index in range(len(selected) - 1, -1, -1):
                if selected[index].kind == "add":
                    selected[index] = replacement
                    break
    selected.sort(key=_changed_line_sort_key)
    return selected


# Minimum width of the diff line-number slot. Expands with a leading space once
# real line numbers reach 4+ digits so numbers never collide with └ / │.
NUMBER_GUTTER = 4


def _number_gutter(changed: Sequence[_ChangedLine]) -> int:
    """Unified line-number slot width for all rows and ⋮ markers in a file card."""
    digits = max((len(str(line.number)) for line in changed), default=0)
    return max(NUMBER_GUTTER, digits + 1)


def format_numbered_change(number: int, unified_line: str, *, gutter: int = NUMBER_GUTTER) -> str:
    """Render `  12- content`: marker glued to the line number, space before body."""
    label = str(number)
    prefix, content = unified_line[:1], unified_line[1:]
    if prefix not in "+-":
        return f"{label:>{gutter}} {unified_line}"
    body = f"{prefix} {content}" if content else prefix
    return f"{label:>{gutter}}{body}"


def _edit_diff(path: str, old: str, new: str, start_lines: tuple[int, ...]) -> str:
    """One snippet diff, repeated per match at its actual (and shifted) position.

    Old-side numbers are the pre-edit lines; new-side numbers account for the
    line delta of earlier matches, so later additions land on real lines.
    """
    old_lines, new_lines = old.splitlines(), new.splitlines()
    snippet = "\n".join(unified_diff(
        old_lines,
        new_lines,
        fromfile=_diff_name("a", path),
        tofile=_diff_name("b", path),
        lineterm="",
    ))
    if not snippet or not start_lines:
        return snippet
    blocks: list[str] = []
    shift = 0
    for start in start_lines:
        blocks.append(_shift_hunk_numbers(
            snippet, old_offset=start - 1, new_offset=start - 1 + shift,
        ))
        shift += new.count("\n") - old.count("\n")
    merged = blocks[0]
    for block in blocks[1:]:
        lines = block.splitlines()
        first_hunk = next((i for i, line in enumerate(lines) if line.startswith("@@")), len(lines))
        merged = f"{merged}\n" + "\n".join(lines[first_hunk:])
    return merged


def build_diff_preview(
    name: str, args: dict[str, Any], *, start_lines: tuple[int, ...] = (),
) -> DiffPreview | None:
    """Parse mutation arguments into the shared DiffPreview domain model.

    For ``edit``, ``start_lines`` must hold the actual 1-based file line of
    every match from ``artifact.edit_start_lines``. When valid position
    metadata is absent, returns ``None`` so callers show only the Edit summary
    without guessing or fabricating line numbers.
    """
    if name == "write":
        content = args.get("content")
        if not isinstance(content, str):
            return None
        raw = "\n".join(unified_diff(
            [],
            content.splitlines(),
            fromfile="/dev/null",
            tofile=_diff_name("b", str(args.get("file_path") or "file")),
            lineterm="",
        ))
        if not raw:
            return None
        changed = _changed_lines(raw)
        return DiffPreview(
            path=_mutation_path(name, args),
            added=sum(1 for line in changed if line.kind == "add"),
            deleted=0,
            full_diff=raw,
            changed=tuple(changed),
        )
    if name == "edit":
        if not start_lines:
            return None
        old = args.get("old_string")
        new = args.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        path = str(args.get("file_path") or "file")
        raw = _edit_diff(path, old, new, start_lines)
        if not raw:
            return None
        changed = _changed_lines(raw)
        return DiffPreview(
            path=_mutation_path(name, args),
            added=sum(1 for line in changed if line.kind == "add"),
            deleted=sum(1 for line in changed if line.kind == "delete"),
            full_diff=raw,
            changed=tuple(changed),
        )
    if name == "delete":
        return DiffPreview(
            path=_mutation_path(name, args),
            full_diff=f"delete {_mutation_path(name, args)}",
        )
    return None


def _edit_start_lines(artifact: Any) -> tuple[int, ...]:
    """Actual match start lines captured by edit execution in current format."""
    if not isinstance(artifact, Mapping):
        return ()
    values = artifact.get("edit_start_lines")
    if not isinstance(values, (list, tuple)) or not values:
        return ()
    starts = tuple(
        value for value in values
        if not isinstance(value, bool) and isinstance(value, int) and value >= 1
    )
    occurrences = artifact.get("edit_occurrences")
    if isinstance(occurrences, bool) or not isinstance(occurrences, int):
        occurrences = None
    if len(starts) != len(values) or (occurrences is not None and occurrences != len(starts)):
        return ()
    return starts


def _hidden_changed_footer(hidden: int) -> PreviewLine:
    return PreviewLine(f"… {hidden} changed lines hidden · {REVIEW_HINT}", "dim")


def _omitted_marker(*, gutter: int = NUMBER_GUTTER) -> PreviewLine:
    """⋮ right-aligned in the unified line-number slot, never a tree glyph."""
    return PreviewLine(f"{'⋮':>{gutter}}", "dim")


def normalize_file_mutation(
    block: ToolBlock, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS, full: bool = False,
) -> FileMutationPreview | None:
    """Map a successful tool result to file semantics; never infer a missing write operation."""
    if block.status != "completed" or block.is_error:
        return None
    if block.name == "edit":
        operation: FileMutationOperation = "modify"
        diff = build_diff_preview(
            block.name, block.arguments, start_lines=_edit_start_lines(block.artifact),
        )
        path = diff.path if diff else _operand(block.arguments)
    elif block.name == "write":
        operation = block.artifact.get("operation") if isinstance(block.artifact, dict) else None
        if operation not in {"create", "overwrite"}:
            return None
        diff = build_diff_preview(block.name, block.arguments)
        path = _operand(block.arguments)
    elif block.name == "delete":
        return FileMutationPreview(path=_operand(block.arguments), operation="delete")
    else:
        return None
    return _file_preview(path, operation, diff, limits=limits, expanded=full)


def _mutation_count_detail(diff: DiffPreview | None, operation: FileMutationOperation) -> str:
    if diff is None:
        return ""
    added, deleted = diff.added or 0, diff.deleted or 0
    if operation == "create" and added == 0 and deleted == 0:
        return ""
    if operation in {"create", "modify", "overwrite"}:
        return f"(+{added} -{deleted})"
    return ""


def file_tool_preview(mutation: FileMutationPreview) -> ToolPreview:
    verb = {"create": "Created", "modify": "Edited", "overwrite": "Wrote", "delete": "Deleted"}[mutation.operation]
    multi = len(mutation.paths) > 1
    target = f"{len(mutation.paths)} files" if multi else mutation.path
    return ToolPreview(
        kind="file_mutation", verb=verb, target=target,
        title=f"{verb} {target}".strip(),
        detail=_mutation_count_detail(mutation.diff, mutation.operation),
        lines=mutation.compact_lines,
        group="mutation", action="expand", file_mutation=mutation,
        syntax=None if multi else code_renderer.syntax_for_path(mutation.path),
    )


def _changed_rows(
    diffs: list[DiffPreview],
    *,
    limits: UiDisplayLimits,
    expanded: bool,
    gutter: int,
) -> tuple[tuple[PreviewLine, ...], int]:
    """One path's body: every changed line sorted, then the existing budget.

    Sorting happens before the head-lines budget; the +/- floor and hidden
    count are unchanged. Skipped spans leave one ⋮ right-aligned in ``gutter``.
    """
    changed = [line for diff in diffs for line in diff.changed]
    selected = _select_changed_lines(changed, limits=limits, full=expanded)
    rows = [
        PreviewLine(format_numbered_change(line.number, line.text, gutter=gutter), line.kind)
        for line in selected
    ]
    hidden = len(changed) - len(selected)
    if hidden and not expanded:
        rows.append(_omitted_marker(gutter=gutter))
    return tuple(rows), hidden


def _section(
    path: str,
    diffs: list[DiffPreview],
    *,
    limits: UiDisplayLimits,
    expanded: bool,
    gutter: int,
) -> FileSection:
    """One path's section, shared by single- and multi-file cards."""
    rows, hidden = _changed_rows(diffs, limits=limits, expanded=expanded, gutter=gutter)
    return FileSection(
        path=path,
        added=sum(diff.added or 0 for diff in diffs),
        deleted=sum(diff.deleted or 0 for diff in diffs),
        lines=rows,
        hidden=hidden,
        has_diff=bool(diffs),
    )


def _hidden_footer(sections: Sequence[FileSection], *, expanded: bool) -> tuple[PreviewLine, ...]:
    hidden = sum(section.hidden for section in sections)
    return () if expanded or not hidden else (_hidden_changed_footer(hidden),)


def _file_preview(
    path: str,
    operation: FileMutationOperation,
    diff: DiffPreview | None,
    *,
    limits: UiDisplayLimits,
    expanded: bool,
) -> FileMutationPreview:
    """Single-path card: one section plus the shared footer."""
    diffs = [diff] if diff is not None else []
    gutter = _number_gutter(diff.changed if diff is not None else ())
    section = _section(path, diffs, limits=limits, expanded=expanded, gutter=gutter)
    return FileMutationPreview(
        path=path, operation=operation, diff=diff,
        footer=_hidden_footer((section,), expanded=expanded),
        sections=(section,),
    )


def _preview_body(diff: DiffPreview, *, limits: UiDisplayLimits, full: bool) -> tuple[PreviewLine, ...]:
    """Flat body for cards without a mutation model, e.g. a running write."""
    return _file_preview(diff.path, "overwrite", diff, limits=limits, expanded=full).compact_lines


def _path_diffs(
    mutations: list[FileMutationPreview], operation: FileMutationOperation,
) -> list[DiffPreview]:
    """Repeated edits keep every diff; a write selects the final mutation first."""
    if operation == "modify":
        return [mutation.diff for mutation in mutations if mutation.diff is not None]
    if not mutations:
        return []
    final_diff = mutations[-1].diff
    return [final_diff] if final_diff is not None else []


def aggregate_file_mutations(
    mutations: list[FileMutationPreview], *, expanded: bool = False, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS,
) -> FileMutationPreview:
    """Select rows for a consecutive semantic mutation group.

    ``expanded`` shows every changed line in the existing card: no per-file
    ellipsis and no hidden-line footer. Paths in the group are always listed.
    Same-path edits share one ordered body in both single- and multi-file cards.
    """
    first = mutations[0]
    if first.operation not in {"create", "modify", "overwrite"}:
        raise ValueError("Only create, modify, and overwrite mutations can be grouped")

    ordered_paths: list[str] = []
    by_path: dict[str, list[FileMutationPreview]] = {}
    for mutation in mutations:
        if mutation.path not in by_path:
            ordered_paths.append(mutation.path)
            by_path[mutation.path] = []
        by_path[mutation.path].append(mutation)
    path_diffs = {path: _path_diffs(by_path[path], first.operation) for path in ordered_paths}
    all_changed = [
        line for diffs in path_diffs.values() for diff in diffs for line in diff.changed
    ]
    gutter = _number_gutter(all_changed)
    sections = tuple(
        _section(path, path_diffs[path], limits=limits, expanded=expanded, gutter=gutter)
        for path in ordered_paths
    )
    has_any_diff = any(section.has_diff for section in sections)
    total_diff = (
        DiffPreview(
            path=first.path,
            added=sum(section.added for section in sections),
            deleted=sum(section.deleted for section in sections),
        )
        if has_any_diff else None
    )
    return FileMutationPreview(
        path=first.path, operation=first.operation,
        diff=total_diff,
        footer=_hidden_footer(sections, expanded=expanded),
        sections=sections,
    )


def build_read_preview(block: ToolBlock) -> ToolPreview:
    target = _operand(block.arguments)
    path = block.arguments.get("file_path") or block.arguments.get("path")
    return ToolPreview(
        kind="read", verb="read", target=target, summary=f"Read {target}",
        group="explore", action="expand",
        syntax=code_renderer.syntax_for_path(str(path) if path else target),
    )


def build_ls_preview(block: ToolBlock) -> ToolPreview:
    path = block.arguments.get("path")
    return ToolPreview(
        kind="ls", verb="ls", target=_operand(block.arguments),
        summary=f"List {PurePosixPath(str(path))}" if path else "List",
        group="explore", action="expand",
    )


def build_write_preview(
    block: ToolBlock, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS, full: bool = False,
) -> ToolPreview:
    mutation = normalize_file_mutation(block, limits=limits, full=full)
    if mutation is not None:
        return file_tool_preview(mutation)
    diff = build_diff_preview("write", block.arguments)
    if diff is None:
        return ToolPreview(
            kind="write", verb="write", target=_operand(block.arguments),
            group="mutation",
        )
    lines = _preview_body(diff, limits=limits, full=full)
    return ToolPreview(
        kind="write", verb="write", target=diff.path,
        detail=_mutation_count_detail(diff, "overwrite"),
        lines=lines, group="mutation", action="expand",
        syntax=code_renderer.syntax_for_path(diff.path),
    )


def build_edit_preview(
    block: ToolBlock, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS, full: bool = False,
) -> ToolPreview:
    mutation = normalize_file_mutation(block, limits=limits, full=full)
    if mutation is not None:
        return file_tool_preview(mutation)
    return ToolPreview(
        kind="edit", verb="edit", target=_operand(block.arguments),
        group="mutation",
    )


def build_delete_preview(block: ToolBlock) -> ToolPreview:
    mutation = normalize_file_mutation(block)
    if mutation is not None:
        return file_tool_preview(mutation)
    return ToolPreview(
        kind="delete", verb="delete", target=_operand(block.arguments),
        group="mutation", action="expand",
    )


def normalize_command_execution(block: ToolBlock) -> CommandExecutionPreview:
    """Interpret execute metadata once, without reading output text."""
    artifact = block.artifact if isinstance(block.artifact, dict) else {}
    raw_code = artifact.get("exit_code")
    exit_code = block.exit_code
    if exit_code is None and isinstance(raw_code, int) and not isinstance(raw_code, bool):
        exit_code = raw_code
    reason = artifact.get("termination_reason")
    reason = reason if isinstance(reason, str) else None
    if block.status == "waiting":
        status: CommandExecutionStatus = "waiting"
    elif block.status == "interrupted":
        status = "interrupted"
    elif block.status == "running" or artifact.get("running") is True:
        status = "running"
    elif reason == "cancelled":
        status = "cancelled"
    elif reason == "timeout":
        status = "timeout"
    elif reason == "spawn_error":
        status = "spawn_error"
    elif exit_code not in (None, 0) or block.is_error or block.status == "error":
        status = "failed"
    else:
        status = "succeeded"
    agent_path = artifact.get("agent_log_path")
    host_path = artifact.get("host_log_path")
    log_path = agent_path or host_path
    log_error = artifact.get("log_error")
    execution_id = artifact.get("execution_id")
    args = block.arguments if isinstance(block.arguments, dict) else {}
    interactive = bool(args.get("interactive") or args.get("tty")) or (
        block.name == "write_stdin"
    )
    command = str(args.get("command") or args.get("execution_id") or "…")
    return CommandExecutionPreview(
        command=command,
        status=status,
        exit_code=exit_code,
        termination_reason=reason,
        truncated=artifact.get("truncated") is True,
        log_path=str(log_path) if log_path else None,
        log_error=str(log_error) if log_error else None,
        execution_id=str(execution_id) if execution_id else None,
        interactive=interactive,
    )


def _phrase(block: ToolBlock, *, success: bool, target: str | None = None) -> str:
    """Present participle, or the past form after a successful result.

    ``target`` is the operand the builder already chose, so the title keeps
    that path. Unknown names stay the registered name. A finished write
    without ``artifact.operation`` stays the neutral ``write`` label.
    """
    name = block.name
    operand = target if target else _operand(block.arguments)

    def labeled(verb: str, value: str = operand) -> str:
        return f"{verb} {value}".strip()

    if name == "ls":
        return labeled("Listed" if success else "Listing")
    if name == "read":
        return labeled("Read" if success else "Reading")
    if name == "write":
        return labeled("write" if success else "Writing")
    if name == "edit":
        return labeled("Edited" if success else "Editing")
    if name == "delete":
        return labeled("Deleted" if success else "Deleting")
    if name == "web_search":
        quoted = operand or f'"{str(block.arguments.get("query") or "…").replace(chr(10), " ")[:80]}"'
        return labeled("Searched Web" if success else "Searching Web", quoted)
    if name == "web_fetch":
        url = operand or str(block.arguments.get("url") or "…").replace("\n", " ")[:100]
        return labeled("Fetched" if success else "Fetching", url)
    if name == "request_human_input":
        question = operand or str(block.arguments.get("question") or "input required").replace("\n", " ")[:100]
        return labeled("Asked" if success else "Asking", question)
    if name == "mcp_list":
        return "Listed MCP Servers" if success else "Listing MCP Servers"
    if name == "mcp_add":
        server = str(block.arguments.get("name") or "").strip()
        return labeled("Added" if success else "Adding", f"MCP Server {server}".strip())
    if name == "mcp_remove":
        server = str(block.arguments.get("name") or "").strip()
        return labeled("Removed" if success else "Removing", f"MCP Server {server}".strip())
    if name == "compact_conversation":
        return "Compacted conversation" if success else "Compacting conversation"
    if name == "read_task":
        run_id = str(block.arguments.get("run_id") or "").strip()
        return labeled("Read Task" if success else "Reading Task", run_id)
    if name == "cancel_task":
        run_id = str(block.arguments.get("run_id") or "").strip()
        return labeled("Requested Task Cancellation" if success else "Cancelling Task", run_id)
    from agent.mcp.names import is_external_mcp_tool

    if is_external_mcp_tool(name):
        return _mcp_titles(block)
    return labeled(name) or name


def _failure_heading(phrase: str, *, exit_code: object = None) -> str:
    """One Failed prefix. An exit code is included only when the tool reported one."""
    code = exit_code if type(exit_code) is int else None
    head = f"Failed (exit {code})" if code is not None else "Failed"
    text = str(phrase or "").strip()
    return f"{head} {text}".strip() if text else head


def _command_title(execution: CommandExecutionPreview) -> str:
    command = execution.command
    status = execution.status
    suffix = ""
    if execution.execution_id and status in {"running", "waiting", "interrupted"}:
        suffix = f" · {execution.execution_id}"
        if execution.interactive:
            suffix += " · interactive"
    if status in {"running", "waiting", "interrupted"}:
        return f"Running {command}{suffix}"
    if status == "succeeded":
        return f"Ran {command}"
    if status == "failed":
        return _failure_heading(command, exit_code=execution.exit_code)
    if status == "timeout":
        return f"Timed out {command}"
    if status == "cancelled":
        return f"Cancelled {command}"
    if status == "spawn_error":
        return f"Failed to start {command}"
    return f"Running {command}{suffix}"


def _finalize_title(block: ToolBlock, preview: ToolPreview) -> ToolPreview:
    """Fill a missing title. A title already chosen by the builder is kept."""
    if preview.title:
        return preview
    success = block.status == "completed" and not block.is_error
    return replace(
        preview,
        title=_phrase(block, success=success, target=preview.target),
        omit_status_suffix=block.name == "request_human_input" and block.status == "waiting",
    )


def build_execute_preview(block: ToolBlock) -> ToolPreview:
    execution = normalize_command_execution(block)
    return ToolPreview(
        kind="execute", verb="execute", target=execution.command,
        title=_command_title(execution),
        summary=f"execute {execution.command}",
        group="command", action="expand", command_execution=execution,
    )


def _format_seconds(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return f"{value:g}s"
    text = str(value).strip()
    return f"{text}s" if text else ""


def build_web_search_preview(block: ToolBlock) -> ToolPreview:
    query = str(block.arguments.get("query") or "…").replace("\n", " ")
    detail = ""
    artifact = block.artifact if isinstance(block.artifact, dict) else None
    if block.status == "completed":
        if artifact is not None:
            stats: list[str] = []
            results = artifact.get("results")
            if isinstance(results, list):
                stats.append(f"{len(results)} results")
            seconds = _format_seconds(artifact.get("response_time"))
            if seconds:
                stats.append(seconds)
            detail = " · ".join(stats)
    return ToolPreview(
        kind="web_search", verb="web_search", target=f'"{query[:80]}"',
        detail=detail, group="web", action="expand",
    )


def build_web_fetch_preview(block: ToolBlock) -> ToolPreview:
    url = str(block.arguments.get("url") or "…").replace("\n", " ")
    artifact = block.artifact if isinstance(block.artifact, dict) else {}
    title = str(artifact.get("title") or "").replace("\n", " ")
    return ToolPreview(
        kind="web_fetch", verb="web_fetch", target=url[:100],
        detail=title[:100], group="web", action="expand",
    )


def build_human_input_preview(block: ToolBlock) -> ToolPreview:
    question = str(block.arguments.get("question") or "input required").replace("\n", " ")
    return ToolPreview(
        kind="human_input", verb="ask", target=question[:100],
        summary=f"ask {question[:100]}", action="expand",
    )


_MCP_SUMMARY_LIMIT = 120
_MCP_SUMMARY_ARGS = 4


def _mcp_clean(value: str) -> str:
    from agent.cli.interactions import mcp_display_text

    return " ".join(mcp_display_text(value).split())


def resolve_mcp_identity(
    name: str,
    *,
    artifact: Any = None,
    lookup: Callable[[str], tuple[str, str] | None] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve real MCP (serverInfo.name, remote tool name) without parsing ``mcp__...``."""
    from agent.mcp.names import is_external_mcp_tool

    if not isinstance(name, str) or not is_external_mcp_tool(name):
        return None, None
    raw_server: Any = None
    raw_tool: Any = None
    if isinstance(artifact, Mapping):
        raw_server = artifact.get("mcp_server_name")
        raw_tool = artifact.get("mcp_tool_name")
    if (not raw_tool or not raw_server) and callable(lookup):
        resolved = lookup(name)
        if resolved is not None:
            l_server, l_tool = resolved
            if not raw_server and l_server:
                raw_server = l_server
            if not raw_tool and l_tool:
                raw_tool = l_tool
    server = _mcp_clean(raw_server) if isinstance(raw_server, str) and raw_server.strip() else None
    tool = _mcp_clean(raw_tool) if isinstance(raw_tool, str) and raw_tool.strip() else None
    return server, tool


def format_mcp_display_name(
    name: str,
    *,
    artifact: Any = None,
    lookup: Callable[[str], tuple[str, str] | None] | None = None,
    include_server: bool = True,
    prefix: str | None = "MCP",
) -> str:
    """Single MCP display formatter shared by tool cards, approval panels, and Tool Health."""
    if not isinstance(name, str):
        return ""
    server, tool = resolve_mcp_identity(name, artifact=artifact, lookup=lookup)
    if not tool:
        return name
    parts: list[str] = []
    if prefix:
        parts.append(prefix)
    if include_server and server:
        parts.append(server)
    parts.append(tool)
    return " · ".join(parts)


def mcp_preview_title(name: str, artifact: Any) -> str:
    """Card title for external MCP tools. Missing remote tool name keeps the registered name."""
    return format_mcp_display_name(name, artifact=artifact, include_server=True, prefix="MCP")


def _mcp_titles(block: ToolBlock) -> str:
    return mcp_preview_title(block.name, block.artifact)


def _mcp_argument_summary(args: Any) -> str:
    """One collapsed line, bounded by the existing argument preview."""
    from agent.cli.interactions import mcp_arguments_preview

    chosen: list[str] = []
    extra = False
    for line in mcp_arguments_preview(args, expanded=False).splitlines():
        if "content omitted" in line or not line.startswith("  ") or line.startswith("    "):
            continue
        compact = " ".join(line.split())
        if not compact:
            continue
        if len(chosen) >= _MCP_SUMMARY_ARGS:
            extra = True
            break
        chosen.append(compact)
    if extra:
        chosen.append("…")
    text = " ".join(chosen)
    if len(text) > _MCP_SUMMARY_LIMIT:
        return text[: _MCP_SUMMARY_LIMIT - 1].rstrip() + "…"
    return text


def build_mcp_preview(block: ToolBlock) -> ToolPreview:
    """Same card as every other tool. Only the title and argument lines differ."""
    from agent.cli.interactions import mcp_arguments_preview, mcp_display_text

    target = _mcp_titles(block)
    summary = _mcp_argument_summary(block.arguments)
    structured = mcp_arguments_preview(block.arguments, expanded=True)
    expanded = [PreviewLine("Host (outside sandbox)", "dim")]
    if structured:
        expanded.extend(PreviewLine(line, "dim") for line in structured.splitlines())
    return ToolPreview(
        kind="mcp",
        verb="MCP",
        target=target,
        title=target,
        summary=target,
        collapsed_lines=(PreviewLine(summary, "dim"),) if summary else (),
        expanded_lines=tuple(expanded),
        display_output=mcp_display_text(block.output),
        group="other",
        action="expand",
    )


def build_generic_preview(block: ToolBlock) -> ToolPreview:
    display = block.name
    target = _operand(block.arguments)
    return ToolPreview(
        kind=block.name, verb=display, target=target,
        summary=f"{display} {target}".strip() or display,
        action="expand",
    )


def build_task_preview(block: ToolBlock) -> ToolPreview:
    """Single-line task preview; display width truncation lives in _subagent_title()."""
    description = str(block.arguments.get("description") or "Delegated task")
    compact = " ".join(description.split()) or "Delegated task"
    detail = "background" if is_background_launch(block.name, block.arguments) else ""
    return ToolPreview(
        kind="task", verb="SubAgent", target=description,
        detail=detail,
        summary=f"SubAgent · {compact}", action="expand",
    )


_PREVIEW_BUILDERS: dict[str, PreviewBuilder] = {
    "start_task": build_task_preview,
    "ls": build_ls_preview,
    "read": build_read_preview,

    "delete": build_delete_preview,

    "execute": build_execute_preview,
    "write_stdin": build_execute_preview,

    "web_search": build_web_search_preview,
    "web_fetch": build_web_fetch_preview,

    "request_human_input": build_human_input_preview,
}

def tool_group(name: str) -> PreviewGroup:
    """Classification lookup for cheap structural passes (no Rich involved)."""
    return tool_preview_group(name)  # type: ignore[return-value]


def is_mutation_tool(name: str) -> bool:
    return catalog_is_mutation_tool(name)


def _failure_summary(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return ""
    if lines[0].startswith("Argument validation failed"):
        return next((line.removeprefix("- ") for line in lines if line.startswith("- ")), lines[0])
    return next((line for line in lines if not line.startswith("Exit code:")), "")


def _failure_lines(
    block: ToolBlock, group: PreviewGroup, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS,
    output: str | None = None,
) -> list[str]:
    lines = (block.output if output is None else output).strip().splitlines()
    if lines and lines[0].startswith("Argument validation failed"):
        details = [line.strip().removeprefix("- ") for line in lines if line.startswith("- ")]
        shown = details[:limits.head_lines] or lines[:1]
        hidden = len(lines) - len(shown)
    else:
        content = [line for line in lines if line.strip() and not line.strip().startswith("Exit code:")]
        limit = limits.tail_lines if group == "command" else limits.head_lines
        shown = content[-limit:] if group == "command" else content[:limit]
        hidden = len(content) - len(shown)
    if hidden > 0:
        shown.append(hidden_output_notice(hidden))
    return shown


def _build_base_preview(block: ToolBlock, *, limits: UiDisplayLimits, full: bool = False) -> ToolPreview:
    from agent.mcp.names import is_external_mcp_tool

    if block.name == "write":
        return build_write_preview(block, limits=limits, full=full)
    if block.name == "edit":
        return build_edit_preview(block, limits=limits, full=full)
    if is_external_mcp_tool(block.name):
        return build_mcp_preview(block)
    builder = _PREVIEW_BUILDERS.get(block.name, build_generic_preview)
    return builder(block)


def build_failure_preview(block: ToolBlock, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS) -> ToolPreview:
    """Uniform preview for failed tools: real exit code + compact error output."""
    base = _build_base_preview(block, limits=limits)
    group = tool_group(block.name)
    phrase = _phrase(block, success=False, target=base.target)
    output = block.output if base.display_output is None else base.display_output
    return ToolPreview(
        kind="failure",
        verb="Failed",
        target=phrase,
        title=_failure_heading(phrase, exit_code=block.exit_code),
        detail=_failure_summary(output),
        summary=base.summary,
        lines=tuple(PreviewLine(line, "dim") for line in _failure_lines(block, group, limits=limits, output=output)),
        group=group,
        action="expand",
        collapsed_lines=base.collapsed_lines,
        expanded_lines=base.expanded_lines,
        display_output=base.display_output,
    )


def build_tool_preview(
    block: ToolBlock, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS, full: bool = False,
) -> ToolPreview:
    if is_task_tool(block.name):
        return _finalize_title(block, build_task_preview(block))
    if block.name == "execute":
        return build_execute_preview(block)
    if block.is_error:
        return build_failure_preview(block, limits=limits)
    return _finalize_title(block, _build_base_preview(block, limits=limits, full=full))
