"""Edit execution reports the real file line through structured metadata."""
from __future__ import annotations

import asyncio
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage

from agent.bootstrap.factory import create_agent
from agent.cli.display import UiDisplayLimits
from agent.cli.previews import build_tool_preview
from agent.cli.state import CliState, ToolBlock
from agent.cli.rendering import render_transcript
from agent.execution.filesystem import LineNumberedFilesystemBackend
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


def _middleware(root: Path) -> WorkspaceFilesystemMiddleware:
    return WorkspaceFilesystemMiddleware(
        backend=LineNumberedFilesystemBackend(root_dir=root, virtual_mode=True, max_file_size_mb=10),
        tools=["read", "edit"],
    )


def test_edit_backend_reports_match_line_from_the_same_read(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("one\ntwo\nthree\nfour\n")
    backend = LineNumberedFilesystemBackend(root_dir=tmp_path, virtual_mode=True, max_file_size_mb=10)
    result = backend.edit("/app.py", "three", "THREE")
    assert result.error is None
    assert result.occurrences == 1
    assert result.edit_start_line == 3
    assert path.read_text() == "one\ntwo\nTHREE\nfour\n"

    path.write_text("one\ntwo\nthree\nfour\n")
    async_result = asyncio.run(backend.aedit("/app.py", "four", "FOUR"))
    assert async_result.edit_start_line == 4


def test_edit_tool_message_carries_actual_start_line_sync_and_async(tmp_path: Path) -> None:
    path = tmp_path / "app.py"

    def handler(_request: _Request) -> ToolMessage:
        result = backend.edit("/app.py", "three", "THREE")
        assert result.error is None
        return ToolMessage(content="ok", tool_call_id="sync", name="edit")

    async def async_handler(_request: _Request) -> ToolMessage:
        result = await backend.aedit("/app.py", "three", "THREE")
        assert result.error is None
        return ToolMessage(content="ok", tool_call_id="async", name="edit")

    backend = LineNumberedFilesystemBackend(root_dir=tmp_path, virtual_mode=True, max_file_size_mb=10)
    middleware = _middleware(tmp_path)
    path.write_text("one\ntwo\nthree\nfour\n")
    sync_result = middleware.wrap_tool_call(
        _Request("edit", {"file_path": "/app.py", "old_string": "three", "new_string": "THREE"}),
        handler,
    )
    assert sync_result.artifact == {"edit_start_line": 3, "edit_occurrences": 1}

    path.write_text("one\ntwo\nthree\nfour\n")
    async_result = asyncio.run(middleware.awrap_tool_call(
        _Request("edit", {"file_path": "/app.py", "old_string": "three", "new_string": "THREE"}),
        async_handler,
    ))
    assert async_result.artifact == {"edit_start_line": 3, "edit_occurrences": 1}


def test_edit_start_line_artifact_drives_actual_diff_numbers(tmp_path: Path) -> None:
    block = ToolBlock(
        "edit-real", "edit",
        {
            "file_path": "/workspace/app.py",
            "old_string": "a\nb\nc\nd\ne\nf",
            "new_string": "a\nB\nc\nd\ne\nF",
        },
        artifact={"edit_start_line": 24, "edit_occurrences": 1},
        status="completed",
    )
    preview = build_tool_preview(block)
    body = [line.text for line in preview.file_mutation.sections[0].lines]
    assert body == ["  25+ B", "  25- b", "  29+ F", "  29- f"]
    rendered = _plain(render_transcript(CliState(blocks=[block]), 80))
    assert "25+ B" in rendered and "29- f" in rendered
    assert "└" in rendered


def test_legacy_edit_without_metadata_keeps_snippet_numbers() -> None:
    block = ToolBlock(
        "edit-legacy", "edit",
        {
            "file_path": "/workspace/app.py",
            "old_string": "a\nb\nc\nd\ne\nf",
            "new_string": "a\nB\nc\nd\ne\nF",
        },
        status="completed",
    )
    preview = build_tool_preview(block)
    body = [line.text for line in preview.file_mutation.sections[0].lines]
    assert body == ["   2+ B", "   2- b", "   6+ F", "   6- f"]


def test_grouped_edits_sort_by_actual_file_position() -> None:
    later = ToolBlock(
        "later", "edit",
        {"file_path": "/workspace/app.py", "old_string": "z", "new_string": "late"},
        artifact={"edit_start_line": 100}, status="completed",
    )
    earlier = ToolBlock(
        "earlier", "edit",
        {"file_path": "/workspace/app.py", "old_string": "y", "new_string": "early"},
        artifact={"edit_start_line": 24}, status="completed",
    )
    rendered = _plain(render_transcript(
        CliState(blocks=[later, earlier]), 100, limits=UiDisplayLimits(head_lines=8),
    ))
    assert rendered.index("24+ early") < rendered.index("100+ late")
    assert rendered.index("24- y") < rendered.index("100- z")
    assert rendered.count("Edited /workspace/app.py (+2 -2)") == 1
    assert "snippet line numbers" not in rendered


def test_edit_tool_completion_artifact_reaches_the_tui_card(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("one\ntwo\nthree\nfour\n")
    backend = LineNumberedFilesystemBackend(root_dir=tmp_path, virtual_mode=True, max_file_size_mb=10)
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
        assert completed[0].artifact == {"edit_start_line": 3, "edit_occurrences": 1}
    finally:
        runner.close()
