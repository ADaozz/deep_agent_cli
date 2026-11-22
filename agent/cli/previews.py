"""Tool preview IR: tool display semantics live here, rendering stays generic.

ToolBlock → tool-specific Preview Builder → ToolPreview → Generic Renderer.

A ToolPreview describes *what* a tool card shows: verb, target operand,
content lines, hint action. The renderer owns *how* it looks: spinner, status
colors, terminal width, folding limits. ToolPreview is a UI-derived object and
is never persisted; sessions keep the raw ToolMessage artifact instead.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import PurePosixPath
from typing import Any, Callable, Literal

from agent.cli.state import ToolBlock
from agent.cli.highlighting import code_renderer
from agent.subagents.notifications import START_TOOL
from agent.execution.mutations import FileMutationOperation
from agent.cli.display import DEFAULT_UI_DISPLAY_LIMITS, UiDisplayLimits
from agent.tools.catalog import is_mutation_tool as catalog_is_mutation_tool
from agent.tools.catalog import tool_preview_group


PreviewStyle = Literal["plain", "dim", "add", "delete", "error", "url"]
PreviewGroup = Literal["explore", "mutation", "command", "web", "other"]
PreviewAction = Literal["none", "expand", "review"]
CommandExecutionStatus = Literal[
    "running", "succeeded", "failed", "timeout", "cancelled",
    "spawn_error", "interrupted", "waiting",
]

# Hint vocabulary selected by ToolPreview.action: review -> Ctrl+R,
# expand -> Ctrl+O. Shared by builders and the renderer so no branch
# hand-writes its own wording.
EXPAND_HINT = "Ctrl+O to expand"
REVIEW_HINT = "Ctrl+R to review"

@dataclass(frozen=True)
class PreviewLine:
    text: str
    style: PreviewStyle = "plain"


@dataclass(frozen=True)
class ToolPreview:
    kind: str
    verb: str
    target: str = ""
    # Title suffix (edit: "(+2 -2)").
    detail: str = ""
    # Highlighted count line above the content (write: "+302 lines").
    label: str = ""
    # One-line label for compact lists (explore group: 'Read file.py').
    summary: str = ""
    lines: tuple[PreviewLine, ...] = ()
    group: PreviewGroup = "other"
    action: PreviewAction = "none"
    syntax: str | None = None
    file_mutation: FileMutationPreview | None = None
    command_execution: CommandExecutionPreview | None = None


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


@dataclass(frozen=True)
class DiffPreview:
    """Mutation domain model shared by the compact card and the full review."""

    path: str
    added: int | None = None
    deleted: int | None = None
    lines: tuple[PreviewLine, ...] = ()
    full_diff: str = ""


@dataclass(frozen=True)
class FileMutationPreview:
    """A file change independent of the tool that produced it."""

    path: str
    operation: FileMutationOperation
    diff: DiffPreview | None = None
    compact_lines: tuple[PreviewLine, ...] = ()
    label: str = ""
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ChangedLine:
    """One real -/+ line of a unified diff, with its hunk and diff order."""

    text: str
    kind: Literal["add", "delete"]
    hunk: int
    order: int
    number: int


def _changed_line_sort_key(line: _ChangedLine) -> tuple[int, int]:
    """Unified diff display order: line number ascending, then add before delete."""
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


def added_line_label(count: int) -> str:
    return f"+{count} {'line' if count == 1 else 'lines'}"


def _changed_lines(raw: str) -> list[_ChangedLine]:
    """Real -/+ lines with hunk index and diff order; context is dropped."""
    changed: list[_ChangedLine] = []
    hunk = -1
    seen_hunk = False
    old_number = new_number = 0
    for line in raw.splitlines():
        if line.startswith("@@"):
            seen_hunk = True
            hunk += 1
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if match:
                old_number, new_number = map(int, match.groups())
            continue
        if not seen_hunk:
            continue  # --- / +++ header lines before the first hunk
        if line.startswith("+"):
            changed.append(_ChangedLine(line, "add", hunk, len(changed), new_number))
            new_number += 1
        elif line.startswith("-"):
            changed.append(_ChangedLine(line, "delete", hunk, len(changed), old_number))
            old_number += 1
        elif line.startswith(" "):
            old_number += 1
            new_number += 1
    return changed


def _select_changed_lines(changed: list[_ChangedLine], *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS) -> list[_ChangedLine]:
    """Pick the compact-preview lines: first N, then a +/- floor, then re-sort.

    With a budget of at least two, show both - and + when both sides exist.
    Display order is snippet line number ascending, add before delete.
    """
    if len(changed) <= limits.head_lines:
        return sorted(changed, key=_changed_line_sort_key)
    selected = list(changed[:limits.head_lines])
    if limits.head_lines >= 2 and len({line.kind for line in changed}) >= 2:
        if "add" not in {line.kind for line in selected}:
            replacement = next(line for line in changed if line.kind == "add")
            for index in range(len(selected) - 1, -1, -1):
                if selected[index].kind == "delete":
                    selected[index] = replacement
                    break
        elif "delete" not in {line.kind for line in selected}:
            replacement = next(line for line in changed if line.kind == "delete")
            for index in range(len(selected) - 1, -1, -1):
                if selected[index].kind == "add":
                    selected[index] = replacement
                    break
    selected.sort(key=_changed_line_sort_key)
    return selected


def format_numbered_change(number: int, unified_line: str, *, indent: str = "") -> str:
    """Render `  12- content`: marker glued to the line number, space before body."""
    prefix, content = unified_line[:1], unified_line[1:]
    if prefix not in "+-":
        return f"{indent}{number:>4} {unified_line}"
    body = f"{prefix} {content}" if content else prefix
    return f"{indent}{number:>4}{body}"


def _edit_display_lines(
    changed: list[_ChangedLine],
    *,
    limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS,
    indent: str = "",
    with_hidden_footer: bool = True,
) -> tuple[tuple[PreviewLine, ...], int]:
    """Compact edit body: changed lines only, ⋮ at skipped spans, hidden count.

    A ⋮ marks a snippet-number jump or hunk boundary after number-then-/+
    sorting; it never counts against the budget or the hidden total.
    """
    selected = _select_changed_lines(changed, limits=limits)
    if not selected:
        return (), len(changed)
    display: list[PreviewLine] = []
    previous: _ChangedLine | None = None
    for line in selected:
        if previous is not None and (line.hunk != previous.hunk or line.number > previous.number + 1):
            display.append(PreviewLine(f"{indent}  ⋮", "dim"))
        display.append(PreviewLine(
            format_numbered_change(line.number, line.text, indent=indent),
            "add" if line.kind == "add" else "delete",
        ))
        previous = line
    hidden = len(changed) - len(selected)
    if with_hidden_footer and hidden > 0:
        display.append(PreviewLine(f"… {hidden} changed lines hidden · {REVIEW_HINT}", "dim"))
    return tuple(display), hidden


def build_diff_preview(name: str, args: dict[str, Any]) -> DiffPreview | None:
    """Parse mutation arguments into the shared DiffPreview domain model."""
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
        content_lines = [f"+{line}" for line in content.splitlines()]
        return DiffPreview(
            path=_mutation_path(name, args),
            added=len(content_lines),
            deleted=0,
            lines=tuple(PreviewLine(line, "add") for line in content_lines),
            full_diff=raw,
        )
    if name == "edit":
        old = args.get("old_string")
        new = args.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        path = str(args.get("file_path") or "file")
        raw = "\n".join(unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=_diff_name("a", path),
            tofile=_diff_name("b", path),
            lineterm="",
        ))
        if not raw:
            return None
        changed = _changed_lines(raw)
        added = sum(1 for line in changed if line.kind == "add")
        deleted = sum(1 for line in changed if line.kind == "delete")
        lines = tuple(
            PreviewLine(line.text, "add" if line.kind == "add" else "delete")
            for line in changed
        )
        return DiffPreview(
            path=_mutation_path(name, args),
            added=added,
            deleted=deleted,
            lines=lines,
            full_diff=raw,
        )
    if name == "delete":
        return DiffPreview(
            path=_mutation_path(name, args),
            full_diff=f"delete {_mutation_path(name, args)}",
        )
    return None


def _write_compact_lines(
    diff: DiffPreview | None,
    *,
    limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS,
    indent: str = "",
    with_hidden_footer: bool = True,
) -> tuple[tuple[PreviewLine, ...], int]:
    if diff is None:
        return (), 0
    lines = [
        PreviewLine(format_numbered_change(number, line.text, indent=indent), line.style)
        for number, line in enumerate(diff.lines[:limits.head_lines], 1)
    ]
    hidden = len(diff.lines) - len(lines)
    if with_hidden_footer and hidden:
        lines.append(PreviewLine(f"… {hidden} lines hidden · {REVIEW_HINT}", "dim"))
    return tuple(lines), hidden


def normalize_file_mutation(block: ToolBlock, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS) -> FileMutationPreview | None:
    """Map a successful tool result to file semantics; never infer a missing write operation."""
    if block.status != "completed" or block.is_error:
        return None
    if block.name == "edit":
        diff = build_diff_preview(block.name, block.arguments)
        lines, _ = (
            _edit_display_lines(_changed_lines(diff.full_diff), limits=limits)
            if diff else ((), 0)
        )
        return FileMutationPreview(
            path=diff.path if diff else _operand(block.arguments),
            operation="modify", diff=diff, compact_lines=lines,
        )
    if block.name == "write":
        operation = block.artifact.get("operation") if isinstance(block.artifact, dict) else None
        if operation not in {"create", "overwrite"}:
            return None
        diff = build_diff_preview(block.name, block.arguments)
        lines, _ = _write_compact_lines(diff, limits=limits) if diff else ((), 0)
        return FileMutationPreview(
            path=_operand(block.arguments), operation=operation, diff=diff, compact_lines=lines,
        )
    if block.name == "delete":
        return FileMutationPreview(path=_operand(block.arguments), operation="delete")
    return None


def build_review_mutation(name: str, args: dict[str, Any]) -> FileMutationPreview | None:
    """Build proposed diff content when a review has arguments but no tool result."""
    diff = build_diff_preview(name, args)
    if diff is None:
        return None
    operation: FileMutationOperation = {
        "edit": "modify", "write": "overwrite", "delete": "delete",
    }[name]
    return FileMutationPreview(path=diff.path, operation=operation, diff=diff)


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
        detail=_mutation_count_detail(mutation.diff, mutation.operation),
        lines=mutation.compact_lines,
        group="mutation", action="review", file_mutation=mutation,
        syntax=None if multi else code_renderer.syntax_for_path(mutation.path),
    )


def _same_path_modify_rows(
    mutations: list[FileMutationPreview],
    *,
    limits: UiDisplayLimits,
    indent: str = "",
    with_hidden_footer: bool = True,
) -> tuple[list[PreviewLine], int, int, int]:
    """Merge consecutive edits to one path: Edit N headers + shared head_lines budget."""
    source: list[tuple[int, PreviewLine]] = []
    for index, mutation in enumerate(mutations):
        if mutation.diff and mutation.diff.full_diff:
            for changed in sorted(_changed_lines(mutation.diff.full_diff), key=_changed_line_sort_key):
                source.append((index, PreviewLine(
                    format_numbered_change(changed.number, changed.text, indent=indent),
                    "add" if changed.kind == "add" else "delete",
                )))
            continue
        for line in mutation.compact_lines:
            if line.style in {"add", "delete"}:
                source.append((index, PreviewLine(f"{indent}{line.text.lstrip()}", line.style)))
    selected = list(enumerate(source[:limits.head_lines]))
    if len(source) > limits.head_lines:
        kinds = {line.style for _, line in source}
        shown = {line.style for _, (_, line) in selected}
        if limits.head_lines >= 2 and len(kinds) == 2 and len(shown) == 1:
            replacement = next((order, item) for order, item in enumerate(source) if item[1].style not in shown)
            selected[-1] = replacement
            selected.sort(key=lambda item: item[0])
    rows: list[PreviewLine] = []
    previous_order = previous_edit = None
    for order, (edit_index, line) in selected:
        if len(mutations) > 1 and edit_index != previous_edit:
            rows.append(PreviewLine(f"{indent}Edit {edit_index + 1} · snippet line numbers", "dim"))
        elif previous_order is not None and order != previous_order + 1:
            rows.append(PreviewLine(f"{indent}  ⋮", "dim"))
        rows.append(line)
        previous_order, previous_edit = order, edit_index
    added = sum(mutation.diff.added or 0 for mutation in mutations if mutation.diff)
    deleted = sum(mutation.diff.deleted or 0 for mutation in mutations if mutation.diff)
    hidden = max(0, len(source) - len(selected))
    if with_hidden_footer and hidden:
        rows.append(PreviewLine(f"… {hidden} changed lines hidden · {REVIEW_HINT}", "dim"))
    return rows, added, deleted, hidden


def _path_write_rows(
    mutation: FileMutationPreview,
    *,
    limits: UiDisplayLimits,
    indent: str = "  ",
) -> tuple[list[PreviewLine], int, int, int]:
    added = mutation.diff.added or 0 if mutation.diff else 0
    deleted = mutation.diff.deleted or 0 if mutation.diff else 0
    lines, hidden = _write_compact_lines(
        mutation.diff, limits=limits, indent=indent, with_hidden_footer=False,
    )
    return list(lines), added, deleted, hidden


def aggregate_file_mutations(
    mutations: list[FileMutationPreview], *, expanded: bool = False, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS,
) -> FileMutationPreview:
    """Select compact rows for a consecutive semantic mutation group."""
    del expanded  # Multi-file cards always list every path in the consecutive group.
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
    paths = tuple(ordered_paths)

    if len(paths) == 1:
        path_mutations = by_path[paths[0]]
        if first.operation == "modify":
            rows, added, deleted, _ = _same_path_modify_rows(path_mutations, limits=limits)
            return FileMutationPreview(
                path=paths[0], operation="modify",
                diff=DiffPreview(path=paths[0], added=added, deleted=deleted),
                compact_lines=tuple(rows), paths=paths,
            )
        latest = path_mutations[-1]
        return FileMutationPreview(
            path=latest.path, operation=latest.operation, diff=latest.diff,
            compact_lines=latest.compact_lines, paths=paths,
        )

    rows: list[PreviewLine] = []
    total_added = total_deleted = total_hidden = 0
    for path in paths:
        path_mutations = by_path[path]
        if first.operation == "modify":
            file_rows, added, deleted, hidden = _same_path_modify_rows(
                path_mutations, limits=limits, indent="  ", with_hidden_footer=False,
            )
        else:
            file_rows, added, deleted, hidden = _path_write_rows(path_mutations[-1], limits=limits)
        total_added += added
        total_deleted += deleted
        total_hidden += hidden
        count = _mutation_count_detail(
            DiffPreview(path=path, added=added, deleted=deleted), first.operation,
        )
        suffix = f" {count}" if count else ""
        rows.append(PreviewLine(f"└ {path}{suffix}"))
        rows.extend(file_rows)
    if total_hidden:
        rows.append(PreviewLine(f"… {total_hidden} changed lines hidden · {REVIEW_HINT}", "dim"))
    return FileMutationPreview(
        path=first.path, operation=first.operation,
        diff=DiffPreview(path=first.path, added=total_added, deleted=total_deleted),
        compact_lines=tuple(rows), paths=paths,
    )


def build_read_preview(block: ToolBlock) -> ToolPreview:
    target = _operand(block.arguments)
    path = block.arguments.get("file_path") or block.arguments.get("path")
    return ToolPreview(
        kind="read", verb="read", target=target, summary=f"Read {target}",
        group="explore", action="expand",
        syntax=code_renderer.syntax_for_path(str(path) if path else target),
    )


def build_grep_preview(block: ToolBlock) -> ToolPreview:
    pattern = block.arguments.get("pattern")
    label = json.dumps(str(pattern), ensure_ascii=False) if pattern else ""
    return ToolPreview(
        kind="grep", verb="grep", target=_operand(block.arguments),
        summary=f"Search {label}" if pattern else "Search",
        group="explore", action="expand",
    )


def build_glob_preview(block: ToolBlock) -> ToolPreview:
    pattern = block.arguments.get("pattern")
    label = json.dumps(str(pattern), ensure_ascii=False) if pattern else ""
    return ToolPreview(
        kind="glob", verb="glob", target=_operand(block.arguments),
        summary=f"Glob {label}" if pattern else "Glob",
        group="explore", action="expand",
    )


def build_ls_preview(block: ToolBlock) -> ToolPreview:
    path = block.arguments.get("path")
    return ToolPreview(
        kind="ls", verb="ls", target=_operand(block.arguments),
        summary=f"List {PurePosixPath(str(path))}" if path else "List",
        group="explore", action="expand",
    )


def build_write_preview(block: ToolBlock, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS) -> ToolPreview:
    mutation = normalize_file_mutation(block, limits=limits)
    if mutation is not None:
        return file_tool_preview(mutation)
    diff = build_diff_preview("write", block.arguments)
    if diff is None:
        return ToolPreview(
            kind="write", verb="write", target=_operand(block.arguments),
            group="mutation",
        )
    lines, _ = _write_compact_lines(diff, limits=limits)
    return ToolPreview(
        kind="write", verb="write", target=diff.path,
        detail=_mutation_count_detail(diff, "overwrite"),
        lines=lines, group="mutation", action="review",
        syntax=code_renderer.syntax_for_path(diff.path),
    )


def build_edit_preview(block: ToolBlock, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS) -> ToolPreview:
    mutation = normalize_file_mutation(block, limits=limits)
    if mutation is not None:
        return file_tool_preview(mutation)
    diff = build_diff_preview("edit", block.arguments)
    verb = "edit"
    if diff is None:
        return ToolPreview(
            kind="edit", verb=verb, target=_operand(block.arguments),
            group="mutation",
        )
    lines, _ = _edit_display_lines(_changed_lines(diff.full_diff), limits=limits)
    detail = ""
    if diff.added is not None and diff.deleted is not None:
        detail = f"(+{diff.added} -{diff.deleted})"
    return ToolPreview(
        kind="edit", verb=verb, target=diff.path, detail=detail,
        lines=lines, group="mutation", action="review",
        syntax=code_renderer.syntax_for_path(diff.path),
    )


def build_delete_preview(block: ToolBlock) -> ToolPreview:
    mutation = normalize_file_mutation(block)
    if mutation is not None:
        return file_tool_preview(mutation)
    diff = build_diff_preview("delete", block.arguments)
    target = diff.path if diff is not None else _operand(block.arguments)
    return ToolPreview(
        kind="delete", verb="delete", target=target,
        group="mutation", action="review",
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
    elif block.status == "running":
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
    return CommandExecutionPreview(
        command=str(block.arguments.get("command") or "…"),
        status=status,
        exit_code=exit_code,
        termination_reason=reason,
        truncated=artifact.get("truncated") is True,
        log_path=str(log_path) if log_path else None,
        log_error=str(log_error) if log_error else None,
    )


def build_execute_preview(block: ToolBlock) -> ToolPreview:
    execution = normalize_command_execution(block)
    return ToolPreview(
        kind="execute", verb="execute", target=execution.command,
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


def build_generic_preview(block: ToolBlock) -> ToolPreview:
    display = block.name
    target = _operand(block.arguments)
    return ToolPreview(
        kind=block.name, verb=display, target=target,
        summary=f"{display} {target}".strip() or display,
        action="expand",
    )


def build_task_preview(block: ToolBlock) -> ToolPreview:
    """A bounded single-line title; the actual delegation stays untouched."""
    description = " ".join(str(block.arguments.get("description") or "Delegated task").split())
    title = description if len(description) <= 120 else description[:119] + "…"
    detail = str(block.arguments.get("subagent_type") or "general-purpose")
    if block.name == START_TOOL:
        detail += " · background"
    return ToolPreview(
        kind="task", verb="SubAgent", target=title,
        detail=detail,
        summary=f"SubAgent · {title}", action="expand",
    )


_PREVIEW_BUILDERS: dict[str, PreviewBuilder] = {
    "start_task": build_task_preview,
    START_TOOL: build_task_preview,
    "ls": build_ls_preview,
    "read": build_read_preview,
    "grep": build_grep_preview,
    "glob": build_glob_preview,

    "delete": build_delete_preview,

    "execute": build_execute_preview,

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
    if lines[0].startswith("参数校验失败"):
        return next((line.removeprefix("- ") for line in lines if line.startswith("- ")), lines[0])
    return next((line for line in lines if not line.startswith("Exit code:")), "")


def _failure_lines(block: ToolBlock, group: PreviewGroup, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS) -> list[str]:
    lines = block.output.strip().splitlines()
    if lines and lines[0].startswith("参数校验失败"):
        details = [line.strip().removeprefix("- ") for line in lines if line.startswith("- ")]
        shown = details[:limits.head_lines] or lines[:1]
        hidden = len(lines) - len(shown)
    else:
        content = [line for line in lines if line.strip() and not line.strip().startswith("Exit code:")]
        limit = limits.tail_lines if group == "command" else limits.head_lines
        shown = content[-limit:] if group == "command" else content[:limit]
        hidden = len(content) - len(shown)
    if hidden > 0:
        shown.append(f"… {hidden} output lines hidden · {EXPAND_HINT}")
    return shown


def _build_base_preview(block: ToolBlock, *, limits: UiDisplayLimits) -> ToolPreview:
    if block.name == "write":
        return build_write_preview(block, limits=limits)
    if block.name == "edit":
        return build_edit_preview(block, limits=limits)
    builder = _PREVIEW_BUILDERS.get(block.name, build_generic_preview)
    return builder(block)


def build_failure_preview(block: ToolBlock, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS) -> ToolPreview:
    """Uniform preview for failed tools: real exit code + compact error output."""
    base = _build_base_preview(block, limits=limits)
    group = tool_group(block.name)
    summary = base.summary or f"{base.verb} {base.target}".strip() or base.kind
    return ToolPreview(
        kind="failure",
        verb="Failed",
        target=summary,
        detail=_failure_summary(block.output),
        summary=base.summary,
        lines=tuple(PreviewLine(line, "dim") for line in _failure_lines(block, group, limits=limits)),
        group=group,
        action="expand",
    )


def build_tool_preview(block: ToolBlock, *, limits: UiDisplayLimits = DEFAULT_UI_DISPLAY_LIMITS) -> ToolPreview:
    if block.name in {"start_task", START_TOOL}:
        return build_task_preview(block)
    if block.name == "execute":
        return build_execute_preview(block)
    if block.is_error:
        return build_failure_preview(block, limits=limits)
    return _build_base_preview(block, limits=limits)
