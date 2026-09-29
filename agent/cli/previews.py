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
from agent.file_mutation import FileMutationOperation

PreviewStyle = Literal["plain", "dim", "add", "delete", "error", "url"]
PreviewGroup = Literal["explore", "mutation", "command", "web", "other"]
PreviewAction = Literal["none", "expand", "review"]

# Hint vocabulary selected by ToolPreview.action: review -> Ctrl+R,
# expand -> Ctrl+O. Shared by builders and the renderer so no branch
# hand-writes its own wording.
EXPAND_HINT = "Ctrl+O to expand"
REVIEW_HINT = "Ctrl+R to review"

# Maximum changed lines (- and +) shown in the edit compact preview.
# Separators, blanks and the hidden hint never count against it.
EDIT_PREVIEW_CHANGED_LINES = 8
WRITE_PREVIEW_LINES = 6
CREATE_PREVIEW_LIMIT = 5


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


PreviewBuilder = Callable[[ToolBlock], ToolPreview]


def _display_name(name: str) -> str:
    return {"read_file": "read", "write_file": "write", "edit_file": "edit"}.get(name, name)


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
    path = args.get("file_path") or args.get("path")
    if not path:
        return "file"
    label = str(PurePosixPath(str(path)))
    if name == "write_file" and label.startswith("/workspace/"):
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


def _select_changed_lines(changed: list[_ChangedLine]) -> list[_ChangedLine]:
    """Pick the compact-preview lines: first N, then a +/- floor, then re-sort.

    When both sides exist, the preview must show at least one - and one +;
    the replacement line keeps its original diff position after sorting.
    """
    if len(changed) <= EDIT_PREVIEW_CHANGED_LINES:
        return list(changed)
    selected = changed[:EDIT_PREVIEW_CHANGED_LINES]
    if len({line.kind for line in changed}) < 2:
        return selected
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
    selected.sort(key=lambda line: line.order)
    return selected


def _format_changed_text(line: str) -> str:
    prefix, content = line[:1], line[1:]
    return f"{prefix} {content}" if content else prefix


def _edit_display_lines(changed: list[_ChangedLine]) -> tuple[PreviewLine, ...]:
    """Compact edit body: changed lines only, ⋮ at skipped spans, hidden count.

    A ⋮ marks that the preview jumped over hidden changed lines or crossed a
    hunk boundary; it never counts against the budget or the hidden total.
    """
    selected = _select_changed_lines(changed)
    if not selected:
        return ()
    display: list[PreviewLine] = [PreviewLine("", "plain")]
    previous: _ChangedLine | None = None
    for line in selected:
        if previous is not None and (line.order > previous.order + 1 or line.hunk != previous.hunk):
            display.append(PreviewLine("  ⋮", "dim"))
        display.append(PreviewLine(
            f"{line.number:>4} {_format_changed_text(line.text)}",
            "add" if line.kind == "add" else "delete",
        ))
        previous = line
    hidden = len(changed) - len(selected)
    if hidden > 0:
        display.append(PreviewLine("", "plain"))
        display.append(PreviewLine(f"… {hidden} changed lines hidden · {REVIEW_HINT}", "dim"))
    return tuple(display)


def build_diff_preview(name: str, args: dict[str, Any]) -> DiffPreview | None:
    """Parse mutation arguments into the shared DiffPreview domain model."""
    if name == "write_file":
        content = args.get("content")
        if not isinstance(content, str):
            return None
        raw = "\n".join(unified_diff(
            [],
            content.splitlines(),
            fromfile="/dev/null",
            tofile=_diff_name("b", str(args.get("file_path") or args.get("path") or "file")),
            lineterm="",
        ))
        if not raw:
            return None
        content_lines = [f"+{line}" for line in content.splitlines()]
        return DiffPreview(
            path=_mutation_path(name, args),
            added=len(content_lines),
            lines=tuple(PreviewLine(line, "add") for line in content_lines),
            full_diff=raw,
        )
    if name == "edit_file":
        # Presence checks, not truthiness: an explicit "" is a legal edit side.
        old = args["old_string"] if "old_string" in args else args.get("old_text")
        new = args["new_string"] if "new_string" in args else args.get("new_text")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        path = str(args.get("file_path") or args.get("path") or "file")
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


def _write_compact_lines(diff: DiffPreview | None) -> tuple[PreviewLine, ...]:
    if diff is None:
        return ()
    lines = [
        PreviewLine(f"{number:>4} {line.text}", line.style)
        for number, line in enumerate(diff.lines[:WRITE_PREVIEW_LINES], 1)
    ]
    hidden = len(diff.lines) - len(lines)
    if hidden:
        lines.append(PreviewLine(f"… {hidden} lines hidden · {REVIEW_HINT}", "dim"))
    return tuple(lines)


def normalize_file_mutation(block: ToolBlock) -> FileMutationPreview | None:
    """Map a successful tool result to file semantics; never infer a missing write operation."""
    if block.status != "completed" or block.is_error:
        return None
    if block.name == "edit_file":
        diff = build_diff_preview(block.name, block.arguments)
        return FileMutationPreview(
            path=diff.path if diff else _operand(block.arguments),
            operation="modify", diff=diff,
            compact_lines=_edit_display_lines(_changed_lines(diff.full_diff)) if diff else (),
        )
    if block.name == "write_file":
        operation = block.artifact.get("operation") if isinstance(block.artifact, dict) else None
        if operation not in {"create", "overwrite"}:
            return None
        diff = build_diff_preview(block.name, block.arguments) if operation == "overwrite" else None
        return FileMutationPreview(
            path=_operand(block.arguments), operation=operation, diff=diff,
            compact_lines=_write_compact_lines(diff) if operation == "overwrite" else (),
            label=added_line_label(diff.added or 0) if operation == "overwrite" and diff else "",
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
        "edit_file": "modify", "write_file": "overwrite", "delete": "delete",
    }[name]
    return FileMutationPreview(path=diff.path, operation=operation, diff=diff)


def file_tool_preview(mutation: FileMutationPreview) -> ToolPreview:
    verb = {"create": "Create", "modify": "Edited", "overwrite": "Wrote", "delete": "Deleted"}[mutation.operation]
    detail = ""
    if mutation.operation == "modify" and mutation.diff:
        detail = f"(+{mutation.diff.added or 0} -{mutation.diff.deleted or 0})"
    return ToolPreview(
        kind="file_mutation", verb=verb, target=mutation.path, detail=detail,
        label=mutation.label, lines=mutation.compact_lines,
        group="mutation", action="review", file_mutation=mutation,
    )


def aggregate_file_mutations(
    mutations: list[FileMutationPreview], *, expanded: bool = False,
) -> FileMutationPreview:
    """Select compact rows for a consecutive semantic mutation group."""
    first = mutations[0]
    if first.operation == "create":
        paths = tuple(dict.fromkeys(mutation.path for mutation in mutations))
        hidden = 0 if expanded else max(0, len(paths) - CREATE_PREVIEW_LIMIT)
        rows: list[PreviewLine] = []
        if hidden:
            rows.append(PreviewLine(f"  ├ … {hidden} more", "dim"))
        shown = paths if expanded else paths[-CREATE_PREVIEW_LIMIT:]
        for index, path in enumerate(shown):
            branch = "└" if index == len(shown) - 1 else "├"
            rows.append(PreviewLine(f"  {branch} {path}"))
        if not expanded:
            rows.append(PreviewLine(f"  {EXPAND_HINT}", "dim"))
        return FileMutationPreview(
            path=first.path, operation="create", compact_lines=tuple(rows), paths=paths,
        )
    if first.operation != "modify":
        raise ValueError("Only create and modify mutations can be grouped")
    changes = [
        (index, line)
        for index, mutation in enumerate(mutations)
        for line in mutation.compact_lines
        if line.style in {"add", "delete"}
    ]
    selected = list(enumerate(changes[:EDIT_PREVIEW_CHANGED_LINES]))
    if len(changes) > EDIT_PREVIEW_CHANGED_LINES:
        kinds = {line.style for _, line in changes}
        shown = {line.style for _, (_, line) in selected}
        if len(kinds) == 2 and len(shown) == 1:
            replacement = next((order, item) for order, item in enumerate(changes) if item[1].style not in shown)
            selected[-1] = replacement
            selected.sort(key=lambda item: item[0])
    rows = [PreviewLine("")]
    previous_order = previous_edit = None
    for order, (edit_index, line) in selected:
        if previous_order is not None and (order != previous_order + 1 or edit_index != previous_edit):
            rows.append(PreviewLine("  ⋮", "dim"))
        rows.append(line)
        previous_order, previous_edit = order, edit_index
    added = sum(mutation.diff.added or 0 for mutation in mutations if mutation.diff)
    deleted = sum(mutation.diff.deleted or 0 for mutation in mutations if mutation.diff)
    hidden = added + deleted - len(selected)
    if hidden:
        rows.extend((PreviewLine(""), PreviewLine(f"… {hidden} changed lines hidden · {REVIEW_HINT}", "dim")))
    return FileMutationPreview(
        path=first.path, operation="modify",
        diff=DiffPreview(path=first.path, added=added, deleted=deleted),
        compact_lines=tuple(rows), paths=tuple(mutation.path for mutation in mutations),
    )


def build_read_preview(block: ToolBlock) -> ToolPreview:
    target = _operand(block.arguments)
    return ToolPreview(
        kind="read", verb="read", target=target, summary=f"Read {target}",
        group="explore", action="expand",
    )


def build_grep_preview(block: ToolBlock) -> ToolPreview:
    pattern = block.arguments.get("pattern") or block.arguments.get("query")
    label = json.dumps(str(pattern), ensure_ascii=False) if pattern else ""
    return ToolPreview(
        kind="grep", verb="grep", target=_operand(block.arguments),
        summary=f"Search {label}" if pattern else "Search",
        group="explore", action="expand",
    )


def build_glob_preview(block: ToolBlock) -> ToolPreview:
    pattern = block.arguments.get("pattern") or block.arguments.get("query")
    label = json.dumps(str(pattern), ensure_ascii=False) if pattern else ""
    return ToolPreview(
        kind="glob", verb="glob", target=_operand(block.arguments),
        summary=f"Glob {label}" if pattern else "Glob",
        group="explore", action="expand",
    )


def build_ls_preview(block: ToolBlock) -> ToolPreview:
    path = block.arguments.get("path") or block.arguments.get("file_path")
    return ToolPreview(
        kind="ls", verb="ls", target=_operand(block.arguments),
        summary=f"List {PurePosixPath(str(path))}" if path else "List",
        group="explore", action="expand",
    )


def build_write_preview(block: ToolBlock) -> ToolPreview:
    mutation = normalize_file_mutation(block)
    if mutation is not None:
        return file_tool_preview(mutation)
    diff = build_diff_preview("write_file", block.arguments)
    if diff is None:
        return ToolPreview(
            kind="write", verb="write", target=_operand(block.arguments),
            group="mutation",
        )
    return ToolPreview(
        kind="write", verb="write", target=diff.path,
        label=added_line_label(diff.added or 0),
        lines=_write_compact_lines(diff), group="mutation", action="review",
    )


def build_edit_preview(block: ToolBlock) -> ToolPreview:
    mutation = normalize_file_mutation(block)
    if mutation is not None:
        return file_tool_preview(mutation)
    diff = build_diff_preview("edit_file", block.arguments)
    verb = "edit"
    if diff is None:
        return ToolPreview(
            kind="edit", verb=verb, target=_operand(block.arguments),
            group="mutation",
        )
    if block.status == "completed" and diff.added is not None and diff.deleted is not None:
        detail = f"(+{diff.added} -{diff.deleted})"
    else:
        detail = ""
    return ToolPreview(
        kind="edit", verb=verb, target=diff.path, detail=detail,
        lines=_edit_display_lines(_changed_lines(diff.full_diff)),
        group="mutation", action="review",
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


def build_execute_preview(block: ToolBlock) -> ToolPreview:
    command = str(block.arguments.get("command") or "…")
    lines: tuple[PreviewLine, ...] = ()
    if block.status == "completed" and not block.output.strip():
        lines = (PreviewLine("(no output)", "dim"),)
    return ToolPreview(
        kind="execute", verb="execute", target=command,
        summary=f"execute {command}", lines=lines,
        group="command", action="expand",
    )


def _format_seconds(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return f"{value:g}s"
    text = str(value).strip()
    return f"{text}s" if text else ""


def _legacy_search_stats(output: str) -> str:
    """Fallback for old sessions / third-party tools without an artifact."""
    if not output.startswith("Web search results for:"):
        return ""
    count_line = next((
        line for line in output.splitlines()
        if line.endswith(" results") and line.split(" ", 1)[0].isdigit()
    ), "")
    return count_line


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
        else:
            detail = _legacy_search_stats(block.output)
    return ToolPreview(
        kind="web_search", verb="web_search", target=f'"{query[:80]}"',
        detail=detail, group="web", action="expand",
    )


def build_human_input_preview(block: ToolBlock) -> ToolPreview:
    question = str(block.arguments.get("question") or "input required").replace("\n", " ")
    return ToolPreview(
        kind="human_input", verb="ask", target=question[:100],
        summary=f"ask {question[:100]}", action="expand",
    )


def build_generic_preview(block: ToolBlock) -> ToolPreview:
    display = _display_name(block.name)
    target = _operand(block.arguments)
    return ToolPreview(
        kind=block.name, verb=display, target=target,
        summary=f"{display} {target}".strip() or display,
        action="expand",
    )


_PREVIEW_BUILDERS: dict[str, PreviewBuilder] = {
    "ls": build_ls_preview,
    "read_file": build_read_preview,
    "grep": build_grep_preview,
    "glob": build_glob_preview,

    "write_file": build_write_preview,
    "edit_file": build_edit_preview,
    "delete": build_delete_preview,

    "execute": build_execute_preview,

    "web_search": build_web_search_preview,

    "request_human_input": build_human_input_preview,
}

_TOOL_GROUPS: dict[str, PreviewGroup] = {
    "ls": "explore",
    "read_file": "explore",
    "grep": "explore",
    "glob": "explore",
    "write_file": "mutation",
    "edit_file": "mutation",
    "delete": "mutation",
    "execute": "command",
    "web_search": "web",
}


def tool_group(name: str) -> PreviewGroup:
    """Classification lookup for cheap structural passes (no Rich involved)."""
    return _TOOL_GROUPS.get(name, "other")


def is_mutation_tool(name: str) -> bool:
    return tool_group(name) == "mutation"


def _failure_summary(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return ""
    if lines[0].startswith("参数校验失败"):
        return next((line.removeprefix("- ") for line in lines if line.startswith("- ")), lines[0])
    return next((line for line in lines if not line.startswith("Exit code:")), "")


def _failure_lines(block: ToolBlock, group: PreviewGroup) -> list[str]:
    lines = block.output.strip().splitlines()
    if lines and lines[0].startswith("参数校验失败"):
        details = [line.strip().removeprefix("- ") for line in lines if line.startswith("- ")]
        shown = details[:4] or lines[:1]
        hidden = len(lines) - len(shown)
    else:
        content = [line for line in lines if line.strip() and not line.strip().startswith("Exit code:")]
        limit = 6 if group == "command" else 4
        shown = content[-limit:] if group == "command" else content[:limit]
        hidden = len(content) - len(shown)
    if hidden > 0:
        shown.append(f"… {hidden} output lines hidden · {EXPAND_HINT}")
    return shown


def build_failure_preview(block: ToolBlock) -> ToolPreview:
    """Uniform preview for failed tools: real exit code + compact error output."""
    builder = _PREVIEW_BUILDERS.get(block.name, build_generic_preview)
    base = builder(block)
    group = tool_group(block.name)
    if block.name == "execute":
        summary = str(block.arguments.get("command") or "execute")
    else:
        summary = base.summary or f"{base.verb} {base.target}".strip() or base.kind
    return ToolPreview(
        kind="failure",
        verb="Failed",
        target=summary,
        detail=_failure_summary(block.output),
        summary=base.summary,
        lines=tuple(PreviewLine(line, "dim") for line in _failure_lines(block, group)),
        group=group,
        action="expand",
    )


def build_tool_preview(block: ToolBlock) -> ToolPreview:
    if block.is_error:
        return build_failure_preview(block)
    builder = _PREVIEW_BUILDERS.get(block.name)
    if builder is None:
        return build_generic_preview(block)
    return builder(block)
