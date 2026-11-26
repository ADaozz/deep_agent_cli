"""Edit execution reports every real match line through structured metadata."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from deepagents.backends import FilesystemBackend
from langchain_core.messages import AIMessage, ToolMessage

from agent.bootstrap.factory import create_agent
from agent.cli.display import UiDisplayLimits
from agent.cli.previews import build_tool_preview
from agent.cli.state import CliState, ToolBlock
from agent.cli.rendering import render_transcript
from agent.execution.filesystem import LineNumberedFilesystemBackend, _match_start_lines
from agent.execution.mutations import EditPositionCapture, reset_edit_capture, set_edit_capture
from agent.middleware.workspace_filesystem import WorkspaceFilesystemMiddleware
from agent.runtime.runner import AgentRunner
from tests.cli_support import plain as _plain
from tests.conftest import scripted_model


class _Request:
    """Minimal ToolCallRequest surface used by the filesystem hook."""

    def __init__(self, name: str, args: dict) -> None:
        self.tool_call = {"name": name, "args": args}
        self.state: dict = {}

    def override(self, **kwargs) -> "_Request":
        updated = _Request(self.tool_call["name"], self.tool_call["args"])
        updated.tool_call = kwargs.get("tool_call", self.tool_call)
        updated.state = kwargs.get("state", self.state)
        return updated


def _backend(root: Path) -> LineNumberedFilesystemBackend:
    return LineNumberedFilesystemBackend(root_dir=root, virtual_mode=True, max_file_size_mb=10)


def _middleware(root: Path) -> WorkspaceFilesystemMiddleware:
    return WorkspaceFilesystemMiddleware(
        backend=_backend(root),
        tools=["read", "edit"],
    )


def _body(block: ToolBlock) -> list[str]:
    preview = build_tool_preview(block)
    return [line.text for line in preview.file_mutation.sections[0].lines]


def _capture_edit(backend: LineNumberedFilesystemBackend, *args, **kwargs):
    capture = EditPositionCapture()
    token = set_edit_capture(capture)
    try:
        result = backend.edit(*args, **kwargs)
    finally:
        reset_edit_capture(token)
    return result, capture


async def _capture_aedit(backend: LineNumberedFilesystemBackend, *args, **kwargs):
    capture = EditPositionCapture()
    token = set_edit_capture(capture)
    try:
        result = await backend.aedit(*args, **kwargs)
    finally:
        reset_edit_capture(token)
    return result, capture


def test_edit_backend_reports_match_line_from_the_same_read(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("one\ntwo\nthree\nfour\n")
    backend = _backend(tmp_path)
    result, capture = _capture_edit(backend, "/app.py", "three", "THREE")
    assert result.error is None
    assert result.occurrences == 1
    assert capture.start_lines == [3]
    assert capture.occurrences == 1
    assert path.read_text() == "one\ntwo\nTHREE\nfour\n"

    path.write_text("one\ntwo\nthree\nfour\n")
    async_result, async_capture = asyncio.run(_capture_aedit(backend, "/app.py", "four", "FOUR"))
    assert async_result.error is None
    assert async_capture.start_lines == [4]
    assert async_capture.occurrences == 1


def test_replace_all_reports_every_match_line(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("alpha\nold\ngamma\nold\nomega\n")
    result, capture = _capture_edit(_backend(tmp_path), "/app.py", "old", "new", replace_all=True)
    assert result.error is None
    assert result.occurrences == 2
    assert capture.start_lines == [2, 4]
    assert capture.occurrences == 2
    assert path.read_text() == "alpha\nnew\ngamma\nnew\nomega\n"


def test_edit_backend_matches_upstream_filesystem_semantics(tmp_path: Path) -> None:
    cases = [
        ("one\ntwo\nthree\n", "three", "THREE", False),
        ("a\na\n", "a", "b", False),
        ("a\na\n", "a", "b", True),
        ("a\n", "missing", "b", False),
        ("abc", "", "z", False),
        ("x\ny\n", "y\n", "z\n", False),
        ("x\r\ny\r\n", "y", "z", False),
    ]
    for index, (content, old, new, replace_all) in enumerate(cases):
        ours_root = tmp_path / f"ours-{index}"
        theirs_root = tmp_path / f"theirs-{index}"
        ours_root.mkdir()
        theirs_root.mkdir()
        (ours_root / "f.txt").write_text(content)
        (theirs_root / "f.txt").write_text(content)
        ours = _backend(ours_root).edit("/f.txt", old, new, replace_all)
        theirs = FilesystemBackend(
            root_dir=theirs_root, virtual_mode=True, max_file_size_mb=10,
        ).edit("/f.txt", old, new, replace_all)
        assert (ours.error, ours.path, ours.occurrences) == (
            theirs.error, theirs.path, theirs.occurrences
        ), (content, old, new, replace_all)
        assert (ours_root / "f.txt").read_bytes() == (theirs_root / "f.txt").read_bytes()


def test_edit_tool_message_carries_actual_start_lines_sync_and_async(tmp_path: Path) -> None:
    path = tmp_path / "app.py"

    def handler(_request: _Request) -> ToolMessage:
        result = backend.edit("/app.py", "old", "new", replace_all=True)
        assert result.error is None
        return ToolMessage(content="ok", tool_call_id="sync", name="edit")

    async def async_handler(_request: _Request) -> ToolMessage:
        result = await backend.aedit("/app.py", "old", "new", True)
        assert result.error is None
        return ToolMessage(content="ok", tool_call_id="async", name="edit")

    backend = _backend(tmp_path)
    middleware = _middleware(tmp_path)
    content = "one\nold\nthree\nold\n"
    expected = {"edit_start_lines": [2, 4], "edit_occurrences": 2}
    path.write_text(content)
    sync_result = middleware.wrap_tool_call(
        _Request("edit", {"file_path": "/app.py", "old_string": "old", "new_string": "new"}),
        handler,
    )
    assert sync_result.artifact == expected

    path.write_text(content)
    async_result = asyncio.run(middleware.awrap_tool_call(
        _Request("edit", {"file_path": "/app.py", "old_string": "old", "new_string": "new"}),
        async_handler,
    ))
    assert async_result.artifact == expected


def test_edit_start_lines_artifact_drives_actual_diff_numbers() -> None:
    block = ToolBlock(
        "edit-real", "edit",
        {
            "file_path": "/workspace/app.py",
            "old_string": "a\nb\nc\nd\ne\nf",
            "new_string": "a\nB\nc\nd\ne\nF",
        },
        artifact={"edit_start_lines": [24], "edit_occurrences": 1},
        status="completed",
    )
    body = _body(block)
    assert body == ["  25+ B", "  25- b", "  29+ F", "  29- f"]
    rendered = _plain(render_transcript(CliState(blocks=[block]), 80))
    assert "25+ B" in rendered and "29- f" in rendered
    assert "└  25+ B" in rendered


def test_replace_all_preview_shows_every_match_with_shifted_numbers() -> None:
    block = ToolBlock(
        "edit-all", "edit",
        {
            "file_path": "/workspace/app.py",
            "old_string": "old",
            "new_string": "old\nnew",
        },
        artifact={"edit_start_lines": [2, 4], "edit_occurrences": 2},
        status="completed",
    )
    preview = build_tool_preview(block)
    body = [line.text for line in preview.file_mutation.sections[0].lines]
    # Both matches are shown; the second added line moves down by the +1 delta
    # of the first replacement instead of repeating the pre-edit number.
    assert body == ["   3+ new", "   6+ new"]
    assert preview.file_mutation.diff.added == 2
    assert "Edited /workspace/app.py (+2 -0)" in _plain(
        render_transcript(CliState(blocks=[block]), 100)
    )


def test_replace_all_multi_line_matches_cover_all_positions() -> None:
    block = ToolBlock(
        "edit-all-lines", "edit",
        {
            "file_path": "/workspace/app.py",
            "old_string": "a\nb",
            "new_string": "a\nb\nc",
        },
        artifact={"edit_start_lines": [3, 10], "edit_occurrences": 2},
        status="completed",
    )
    body = _body(block)
    assert body == ["   5+ c", "  13+ c"]
    assert build_tool_preview(block).detail == "(+2 -0)"


def test_replace_all_preview_aggregates_with_other_edits(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("x\nold\nmiddle\nold\nend\n")
    result, capture = _capture_edit(_backend(tmp_path), "/app.py", "old", "older", replace_all=True)
    assert capture.start_lines == [2, 4]
    blocks = [
        ToolBlock(
            "edit-all", "edit",
            {"file_path": "/app.py", "old_string": "old", "new_string": "older"},
            artifact={
                "edit_start_lines": list(capture.start_lines),
                "edit_occurrences": result.occurrences,
            },
            status="completed",
        ),
        ToolBlock(
            "edit-tail", "edit",
            {"file_path": "/app.py", "old_string": "end", "new_string": "END"},
            artifact={"edit_start_lines": [5], "edit_occurrences": 1},
            status="completed",
        ),
    ]
    rendered = _plain(render_transcript(
        CliState(blocks=blocks), 100, limits=UiDisplayLimits(head_lines=16),
    ))
    assert rendered.count("Edited /app.py") == 1
    assert "Edited /app.py (+3 -3)" in rendered
    assert rendered.index("2+ older") < rendered.index("2- old")
    assert rendered.index("2- old") < rendered.index("4+ older")
    assert rendered.index("4+ older") < rendered.index("4- old")
    assert rendered.index("4- old") < rendered.index("5+ END")
    assert rendered.index("5+ END") < rendered.index("5- end")


def test_edit_without_valid_position_metadata_shows_summary_only() -> None:
    missing = ToolBlock(
        "edit-missing", "edit",
        {
            "file_path": "/workspace/app.py",
            "old_string": "a\nb\nc\nd\ne\nf",
            "new_string": "a\nB\nc\nd\ne\nF",
        },
        status="completed",
    )
    mismatch = ToolBlock(
        "edit-mismatch", "edit",
        {"file_path": "/workspace/app.py", "old_string": "old", "new_string": "new"},
        artifact={"edit_start_lines": [3], "edit_occurrences": 2},
        status="completed",
    )
    legacy_scalar = ToolBlock(
        "edit-scalar", "edit",
        {"file_path": "/workspace/app.py", "old_string": "old", "new_string": "new"},
        artifact={"edit_start_line": 7, "edit_occurrences": 1},
        status="completed",
    )
    waiting = ToolBlock(
        "edit-waiting", "edit",
        {"file_path": "/workspace/app.py", "old_string": "old", "new_string": "new"},
        status="waiting",
    )
    for block in (missing, mismatch, legacy_scalar):
        preview = build_tool_preview(block)
        assert preview.file_mutation is not None
        assert preview.file_mutation.diff is None
        assert preview.lines == ()
        rendered = _plain(render_transcript(CliState(blocks=[block]), 100))
        assert "• Edited /workspace/app.py" in rendered
        assert "?" not in rendered
        assert "(+" not in rendered
    waiting_rendered = _plain(render_transcript(CliState(blocks=[waiting]), 100))
    assert "• Editing /workspace/app.py — waiting for input" in waiting_rendered
    assert "?" not in waiting_rendered
    assert "(+" not in waiting_rendered


def test_match_start_lines_handles_many_and_multiline_matches_linearly() -> None:
    # 5,000 multiline matches across 10,000 lines — verifies incremental line counting.
    content = "old\nkeep\n" * 5000
    assert _match_start_lines(content, "old\nkeep") == [2 * i + 1 for i in range(5000)]
    assert _match_start_lines("a\nb\na\nb\n", "a\nb\n") == [1, 3]


def test_grouped_edits_sort_by_actual_file_position() -> None:
    later = ToolBlock(
        "later", "edit",
        {"file_path": "/workspace/app.py", "old_string": "z", "new_string": "late"},
        artifact={"edit_start_lines": [100], "edit_occurrences": 1}, status="completed",
    )
    earlier = ToolBlock(
        "earlier", "edit",
        {"file_path": "/workspace/app.py", "old_string": "y", "new_string": "early"},
        artifact={"edit_start_lines": [24], "edit_occurrences": 1}, status="completed",
    )
    rendered = _plain(render_transcript(
        CliState(blocks=[later, earlier]), 100, limits=UiDisplayLimits(head_lines=8),
    ))
    assert rendered.index("24+ early") < rendered.index("100+ late")
    assert rendered.index("24- y") < rendered.index("100- z")
    assert rendered.count("Edited /workspace/app.py (+2 -2)") == 1
    assert "snippet line numbers" not in rendered


@pytest.mark.parametrize("expanded", [False, True])
def test_grouped_edits_ignore_unpositioned_diff_rows_and_keep_known_rows(expanded: bool) -> None:
    known = ToolBlock(
        "known", "edit",
        {"file_path": "/workspace/app.py", "old_string": "k", "new_string": "K"},
        artifact={"edit_start_lines": [5], "edit_occurrences": 1}, status="completed",
    )
    unpositioned = ToolBlock(
        "unpositioned", "edit",
        {"file_path": "/workspace/app.py", "old_string": "l", "new_string": "L"},
        status="completed",
    )
    rendered = _plain(render_transcript(
        CliState(blocks=[unpositioned, known], diffs_expanded=expanded), 100,
    ))
    assert rendered.count("Edited /workspace/app.py (+1 -1)") == 1
    assert "5+ K" in rendered
    assert "5- k" in rendered
    assert "?" not in rendered
    assert "+ L" not in rendered


def test_edit_tool_completion_artifact_reaches_the_tui_card(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("one\ntwo\nthree\nfour\n")
    backend = _backend(tmp_path)
    model = scripted_model([
        AIMessage(content="", tool_calls=[{
            "id": "edit-1", "name": "edit",
            "args": {"file_path": "/app.py", "old_string": "three", "new_string": "THREE"},
        }]),
        AIMessage(content="done"),
    ])
    runner = AgentRunner(
        prepared=create_agent(model=model, backend=backend, skills=[]), thread_id="edit-artifact",
    )
    events = []
    try:
        assert runner.invoke("edit", on_event=events.append).status == "waiting_confirmation"
        assert runner.approve_tool("edit-1", on_event=events.append).status == "completed"
        completed = [
            event for event in events
            if event.type == "tool_completed" and event.tool_call_id == "edit-1"
        ]
        assert len(completed) == 1
        assert completed[0].artifact == {"edit_start_lines": [3], "edit_occurrences": 1}
    finally:
        runner.close()


def test_replace_all_with_removed_lines_shifts_later_match_up() -> None:
    block = ToolBlock(
        "edit-shrink", "edit",
        {
            "file_path": "/workspace/app.py",
            "old_string": "a\nb",
            "new_string": "x",
        },
        artifact={"edit_start_lines": [3, 10], "edit_occurrences": 2},
        status="completed",
    )
    preview = build_tool_preview(block, limits=UiDisplayLimits(head_lines=8))
    assert [line.text for line in preview.file_mutation.sections[0].lines] == [
        "   3+ x", "   3- a", "   4- b",
        "   9+ x", "  10- a", "  11- b",
    ]
    assert preview.detail == "(+2 -4)"


@pytest.mark.parametrize("path", ["/../escape.txt", "../../escape.txt"])
def test_edit_path_isolation_matches_upstream(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError, match="Path traversal"):
        _backend(tmp_path).edit(path, "a", "b")
    with pytest.raises(ValueError, match="Path traversal"):
        FilesystemBackend(root_dir=tmp_path, virtual_mode=True, max_file_size_mb=10).edit(path, "a", "b")


def test_failed_edit_carries_no_position_metadata(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("one\n")
    (tmp_path / "sub").mkdir()
    backend = _backend(tmp_path)
    middleware = _middleware(tmp_path)

    def handler(_request: _Request) -> ToolMessage:
        result = backend.edit("/app.py", "missing", "x")
        assert result.error is not None and "String not found" in result.error
        assert backend.edit("/sub", "one", "two").error is not None
        return ToolMessage(content=result.error, tool_call_id="failed", name="edit", status="error")

    result = middleware.wrap_tool_call(
        _Request("edit", {"file_path": "/app.py", "old_string": "missing", "new_string": "x"}),
        handler,
    )
    assert result.artifact is None
    assert path.read_text() == "one\n"
