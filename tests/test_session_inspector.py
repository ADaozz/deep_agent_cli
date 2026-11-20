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


def test_json_budget_retains_recent_rows_and_marks_truncation():
    from agent.companion.session_inspector import RESPONSE_LIMIT, format_inspect_response

    blocks = [
        TranscriptBlock(kind="assistant", content=f"{i} " + "测" * 420)
        for i in range(25)
    ]
    data = format_inspect_response(inspect_session_payload(blocks, view="recent_messages", limit=20))
    assert len(data.encode()) <= RESPONSE_LIMIT
    payload = json.loads(data)
    assert payload["truncated"]
    assert payload["messages"][-1]["content"].startswith("24 ")
    assert payload["messages"][-1]["truncated"]


def test_same_parent_message_coalesces_resume_completion_with_restored_start():
    from dataclasses import asdict
    from agent.companion.session_inspector import call_result, CallDetails, observation_views
    from agent.companion.timeline import Timeline

    timeline = Timeline("session", 20)
    old = ToolObservation("session", "one", "first", "main", "model", "default", "execute", "id", 1,
                          message_id="parent")
    resume = ToolObservation("session", "two", "second", "main", "model", "default", "execute", "id", 5,
                             finished_at=5, outcome="success", message_id="parent", resumed_from_checkpoint=True)
    timeline.restore([("tool", 1, asdict(old)), ("tool", 5, asdict(resume))])
    cache = {("second", "id"): call_result(CallDetails(), "resolved", {}, ())}
    payload = inspect_session_payload([], observations=observation_views(timeline, cache=cache),
                                      view="tool_detail", run_id="first", tool_call_id="id")
    assert (payload["status"], payload["started_at"], payload["source"]) == ("success", 1, "live")
    assert payload["result"]["output_excerpt"] == "resolved"


def test_independent_runs_are_distinct_even_if_the_model_reuses_message_ids():
    from agent.companion.timeline import Timeline

    timeline = Timeline("session", 20)
    for i, run in enumerate(("one", "two"), 1):
        timeline.completed(ToolObservation(
            "session", run, run, "main", "model", "default", "execute", "same", i,
            finished_at=i + .5, outcome="success", message_id="duplicate",
        ))
    assert [row["run_id"] for row in inspect_session_payload([], timeline)["tools"]] == ["one", "two"]


def test_redaction_preserves_diagnostic_paths_but_hides_credentials():
    from agent.companion.session_inspector import call_arguments, call_result

    details = call_arguments({
        "command": "pytest tests/test_example.py --api-key opaque-cli-secret",
        "params": {"authorization": "opaque-object-secret"},
    }, ())
    details = call_result(details, "Authorization: Bearer opaque-output-secret\n"
                          "tests/test_example.py:4: failed\n"
                          "-----BEGIN PRIVATE KEY-----\nopaque-private-key", {}, ())
    assert "tests/test_example.py" in details.arguments["command"]
    assert "tests/test_example.py:4" in details.output_excerpt
    assert "opaque-" not in json.dumps(details.arguments) + details.output_excerpt


def test_waiting_and_cancelled_checkpoint_statuses_are_preserved():
    blocks = [
        TranscriptBlock(kind="tool", tool_call_id="waiting", name="execute", status="waiting"),
        TranscriptBlock(kind="tool", tool_call_id="cancelled", name="execute", status="cancelled", is_error=True),
        TranscriptBlock(kind="tool", tool_call_id="terminated", name="execute",
                        artifact={"termination_reason": "cancelled"}),
    ]
    rows = inspect_session_payload(blocks)["tools"]
    assert [row["status"] for row in rows] == ["waiting", "cancelled", "cancelled"]


def test_live_result_overrides_stale_checkpoint_running_status():
    from agent.companion.session_inspector import call_arguments, call_result, observation_views
    from agent.companion.timeline import Timeline

    timeline = Timeline("s", 20)
    call = ToolObservation("s", "t", "r", "main", "m", "default", "execute", "id", 1,
                           finished_at=2, outcome="success", message_id="parent", exit_code=0)
    timeline.completed(call)
    blocks = [TranscriptBlock(kind="tool", tool_call_id="id", name="execute",
                              status="running", message_id="parent")]
    details = call_result(call_arguments({"command": "pytest -q"}, ()), "12 passed", {}, ())
    observations = observation_views(timeline, cache={("r", "id"): details})
    payload = inspect_session_payload(blocks, observations=observations, view="tool_detail", tool_call_id="id")
    assert payload["status"] == "success"
    assert payload["result"]["exit_code"] == 0
    assert payload["arguments"] == {"command": "pytest -q"}
    assert payload["result"]["output_excerpt"] == "12 passed"
    assert inspect_session_payload(blocks, timeline)["tools"][0]["status"] == "success"
