from __future__ import annotations

import json
from types import SimpleNamespace

from agent.companion.session_inspector import inspect_session_payload
from agent.companion.timeline import ToolObservation
from agent.sessions.contracts import TranscriptBlock


def test_recent_tools_merges_checkpoint_and_timeline():
    blocks = [
        TranscriptBlock(kind="tool", tool_call_id="a", name="execute", content="ok", status="completed"),
        TranscriptBlock(
            kind="tool", tool_call_id="b", name="execute", content="Error: permission denied",
            status="error", is_error=True, exit_code=1,
            arguments={"command": "pytest -q"},
        ),
    ]
    timeline = SimpleNamespace(
        failures=[{
            "tool_call_id": "b", "tool_name": "execute", "outcome": "failed",
            "exit_code": 1, "error_category": "permission", "duration_ms": 900, "finished_at": 10.0,
        }],
        recent_tool_calls=[],
        active={},
    )
    payload = inspect_session_payload(blocks, timeline, view="recent_tools", limit=10)
    assert payload["view"] == "recent_tools"
    assert [row["tool_call_id"] for row in payload["tools"]] == ["a", "b"]
    assert payload["tools"][1]["error_category"] == "permission"
    assert payload["tools"][1]["outcome"] == "failed"


def test_tool_detail_redacts_secrets_and_returns_excerpt():
    blocks = [
        TranscriptBlock(
            kind="tool",
            tool_call_id="call-1",
            name="execute",
            arguments={"command": "echo sk-personal-abc123"},
            content="secret sk-personal-abc123 leaked",
            status="completed",
        ),
    ]
    payload = inspect_session_payload(
        blocks, None, secrets=(), view="tool_detail", tool_call_id="call-1",
    )
    assert payload["tool"] == "execute"
    assert "sk-personal" not in json.dumps(payload, ensure_ascii=False)
    assert payload["result"]["status"] == "success"


def test_recent_messages_skip_thinking_only_blocks():
    blocks = [
        TranscriptBlock(kind="user", content="为什么失败？"),
        TranscriptBlock(kind="assistant", content="", thinking="hidden"),
        TranscriptBlock(kind="assistant", content="先看命令。"),
        TranscriptBlock(kind="tool", tool_call_id="t1", name="execute", status="error", is_error=True),
    ]
    payload = inspect_session_payload(blocks, view="recent_messages", limit=10)
    assert [item["role"] for item in payload["messages"]] == ["user", "assistant", "tool"]


def test_live_calls_mark_running_tools():
    blocks = [
        TranscriptBlock(kind="tool", tool_call_id="live", name="read", status="running"),
    ]
    live = {
        "live": ToolObservation(
            "s", "t", "r", "main", "model", "default", "read", "live", 1.0,
        ),
    }
    payload = inspect_session_payload(blocks, None, live, view="recent_tools")
    assert payload["tools"][0]["outcome"] == "running"
    assert payload["tools"][0]["source"] == "live"
