"""Tool preview IR: tool display semantics live here, rendering stays generic.

ToolBlock → tool-specific Preview Builder → ToolPreview → Generic Renderer.

A ToolPreview describes *what* a tool card shows: verb, target operand,
content lines, hint action. The renderer owns *how* it looks: spinner, status
colors, terminal width, folding limits. ToolPreview is a UI-derived object and
is never persisted; sessions keep the raw ToolMessage artifact instead.
"""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import PurePosixPath
from typing import Any, Callable, Literal

from agent.cli.state import ToolBlock

PreviewStyle = Literal["plain", "dim", "add", "delete", "error", "url"]
PreviewGroup = Literal["explore", "mutation", "command", "web", "other"]
PreviewAction = Literal["none", "expand", "review"]

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


@dataclass(frozen=True)
class DiffPreview:
    """Mutation domain model shared by the compact card and the full review."""

    kind: Literal["write", "edit", "delete"]
    path: str
    added: int | None = None
    deleted: int | None = None
    lines: tuple[PreviewLine, ...] = ()
    full_diff: str = ""


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
            kind="write",
            path=_mutation_path(name, args),
            added=len(content_lines),
            lines=tuple(PreviewLine(line, "add") for line in content_lines),
            full_diff=raw,
        )
    if name == "edit_file":
        old = args.get("old_string") or args.get("old_text")
        new = args.get("new_string") or args.get("new_text")
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
        # Compact preview hides unified diff headers; only real changes and
        # their context lines remain.
        changed = [
            line for line in raw.splitlines()
            if not line.startswith(("---", "+++", "@@"))
        ]
        added = sum(1 for line in changed if line.startswith("+"))
        deleted = sum(1 for line in changed if line.startswith("-"))
        lines = tuple(
            PreviewLine(
                line,
                "add" if line.startswith("+") else "delete" if line.startswith("-") else "dim",
            )
            for line in changed
        )
        return DiffPreview(
            kind="edit",
            path=_mutation_path(name, args),
            added=added,
            deleted=deleted,
            lines=lines,
            full_diff=raw,
        )
    if name == "delete":
        return DiffPreview(
            kind="delete",
            path=_mutation_path(name, args),
            full_diff=f"delete {_mutation_path(name, args)}",
        )
    return None


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
    diff = build_diff_preview("write_file", block.arguments)
    if diff is None:
        return ToolPreview(
            kind="write", verb="write", target=_operand(block.arguments),
            group="mutation",
        )
    return ToolPreview(
        kind="write", verb="write", target=diff.path,
        label=added_line_label(diff.added or 0),
        lines=diff.lines, group="mutation", action="review",
    )


def build_edit_preview(block: ToolBlock) -> ToolPreview:
    diff = build_diff_preview("edit_file", block.arguments)
    verb = "Edited" if block.status == "completed" else "edit"
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
        lines=diff.lines, group="mutation", action="review",
    )


def build_delete_preview(block: ToolBlock) -> ToolPreview:
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
