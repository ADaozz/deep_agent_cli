"""Terminal run results stay string statuses and do not depend on the graph loop."""
import pytest
from langchain_core.messages import AIMessage, ToolMessage

from agent.llm_stream import ModelStreamCancelled
from agent.run_events import (
    UnknownInterruptError,
    cancelled_result,
    classify_interrupt,
    completed_result,
    failed_result,
    final_answer_committed,
    interaction_result,
    normalize_stream_error,
    update_tool_events,
)


def test_final_answer_is_committed_only_after_a_new_answer_without_tools() -> None:
    answer = AIMessage(content="done", id="m2")
    assert final_answer_committed([answer], "m1") is True
    assert final_answer_committed([answer], "m2") is False
    calling = AIMessage(content="", id="m3", tool_calls=[{"id": "c", "name": "execute", "args": {}}])
    assert final_answer_committed([calling], "m1") is False
    assert final_answer_committed([], None) is False


def test_terminal_results_keep_the_existing_status_strings() -> None:
    assert completed_result("ok") == type(completed_result("ok"))(status="completed", output="ok")
    waiting = interaction_result("waiting_confirmation", [{"toolCallId": "c"}], {})
    assert waiting.status == "waiting_confirmation"
    assert waiting.pending_tool_calls == [{"toolCallId": "c"}]
    assert failed_result("boom").error == "boom"
    error, result = cancelled_result(model_stream_closed=False, unclosed_message="still open")
    assert error == "still open" and result.status == "cancelled" and result.error == "still open"
    error, result = cancelled_result(model_stream_closed=True, unclosed_message="still open")
    assert error == "" and result.error == ""


def test_tool_updates_start_once_and_forward_artifacts() -> None:
    seen: set[str] = set()
    calls = AIMessage(content="", id="m", tool_calls=[
        {"id": "a", "name": "execute", "args": {"command": "true"}},
        {"id": "b", "name": "write_file", "args": {"file_path": "x"}},
    ])
    events, saw_usage = update_tool_events({"agent": {"messages": [calls]}}, seen)
    assert saw_usage is False
    assert [(event.type, event.tool_call_id, event.name) for event in events] == [
        ("tool_started", "a", "execute"),
        ("tool_started", "b", "write_file"),
    ]
    assert events[0].arguments == {"command": "true"}
    replay, _ = update_tool_events({"agent": {"messages": [calls]}}, seen)
    assert replay == []
    done = ToolMessage(content="ok", tool_call_id="a", name="execute", artifact={"exit_code": 0})
    failed = ToolMessage(content="no", tool_call_id="b", name="write_file", status="error")
    events, saw_usage = update_tool_events({"tools": {"messages": [done, failed]}}, seen)
    assert saw_usage is False
    assert events[0].type == "tool_completed" and events[0].artifact == {"exit_code": 0}
    assert events[0].is_error is False
    assert events[1].is_error is True and events[1].content == "no"


def test_usage_events_track_input_tokens_only() -> None:
    seen: set[str] = set()
    plain = AIMessage(content="hi", id="m1")
    events, saw_usage = update_tool_events({"model": {"messages": [plain]}}, seen)
    assert events == [] and saw_usage is False
    measured = AIMessage(
        content="hi", id="m2", usage_metadata={"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
    )
    events, saw_usage = update_tool_events({"model": {"messages": [measured]}}, seen)
    assert saw_usage is True
    assert events[0].type == "usage"
    assert events[0].result == {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}


def test_unknown_interrupt_is_rejected() -> None:
    with pytest.raises(UnknownInterruptError, match="Unsupported interrupt type"):
        classify_interrupt([{"type": "something_new"}], object(), {})
    paused = classify_interrupt([{"type": "pause"}], object(), {})
    assert paused.kind.value == "paused"
    human = classify_interrupt([{"type": "human_input", "question": "q"}], object(), {})
    assert human.kind.value == "waiting_human"
    boundary = classify_interrupt([{"type": "runtime_config_boundary"}], object(), {})
    assert boundary.kind.value == "runtime_config_boundary"


def test_late_cancel_does_not_rewrite_a_committed_answer() -> None:
    answer = AIMessage(content="done", id="final")
    assert final_answer_committed([answer], "previous") is True
    assert normalize_stream_error(ModelStreamCancelled("late"), cancel_requested=True) is None
    assert normalize_stream_error(RuntimeError("reset"), cancel_requested=False) is not None


def test_unexpected_model_close_is_a_failure_and_a_requested_cancel_is_not() -> None:
    assert normalize_stream_error(ModelStreamCancelled("gone"), cancel_requested=True) is None
    failure = normalize_stream_error(ModelStreamCancelled("gone"), cancel_requested=False)
    assert isinstance(failure, RuntimeError)
    assert "unexpectedly" in str(failure)
    assert str(normalize_stream_error(RuntimeError("net"), cancel_requested=False)) == "net"
