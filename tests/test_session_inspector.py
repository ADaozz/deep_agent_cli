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


def test_merge_includes_live_only_calls_and_distinguishes_reused_ids():
    from agent.companion.session_inspector import call_arguments, call_result, observation_views
    from agent.companion.timeline import Timeline

    timeline = Timeline("session", 20)
    first = ToolObservation(
        "session", "turn-a", "run-a", "main", "model", "default", "execute", "same", 1,
        finished_at=2, outcome="success", message_id="answer-a",
    )
    second = ToolObservation(
        "session", "turn-b", "run-b", "main", "model", "default", "execute", "same", 3,
        message_id="answer-b",
    )
    timeline.completed(first)
    timeline.started(second)
    cache = {
        ("run-a", "same"): call_result(call_arguments({"command": "first"}, ()), "done", {}, ()),
        ("run-b", "same"): call_arguments({"command": "second"}, ()),
    }
    saved = [TranscriptBlock(kind="tool", tool_call_id="same", name="execute",
                             message_id="answer-a", status="completed")]
    views = observation_views(timeline, cache=cache)
    payload = inspect_session_payload(saved, observations=views)
    assert [(r["run_id"], r["status"]) for r in payload["tools"]] == [
        ("run-a", "success"), ("run-b", "running"),
    ]
    lookup = dict(view="tool_detail", tool_call_id="same", observations=views)
    assert inspect_session_payload(saved, **lookup)["error"]["code"] == "ambiguous_tool_call_id"
    second_detail = inspect_session_payload(saved, run_id="run-b", **lookup)
    assert second_detail["arguments"] == {"command": "second"}
    assert second_detail["result"]["output_excerpt"] == ""
    first_detail = inspect_session_payload(saved, run_id="run-a", **lookup)
    assert first_detail["result"]["output_excerpt"] == "done"


def test_restored_start_is_unknown_until_checkpoint_provides_a_result():
    from dataclasses import asdict
    from agent.companion.timeline import Timeline

    timeline = Timeline("session", 20)
    start = ToolObservation(
        "session", "turn", "run", "main", "model", "default", "execute", "id", 2,
        message_id="answer",
    )
    timeline.restore([("tool", 2, asdict(start))])
    tools = inspect_session_payload([], timeline)["tools"]
    assert [(row["status"], row["source"]) for row in tools] == [("unknown", "timeline")]
    block = TranscriptBlock(kind="tool", tool_call_id="id", name="execute", message_id="answer",
                            content="AssertionError: wrong result", status="error", exit_code=1)
    detail = inspect_session_payload([block], timeline, view="tool_detail", tool_call_id="id")
    assert detail["status"] == "failed"
    assert detail["error_category"] == "assertion"
    assert detail["run_id"] == "run"
    assert detail["source"] == "checkpoint"
