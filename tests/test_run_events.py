"""Terminal run results stay string statuses and do not depend on the graph loop."""
from langchain_core.messages import AIMessage

from agent.llm_stream import ModelStreamCancelled
from agent.run_events import (
    cancelled_result,
    completed_result,
    failed_result,
    final_answer_committed,
    interaction_result,
    normalize_stream_error,
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


def test_unexpected_model_close_is_a_failure_and_a_requested_cancel_is_not() -> None:
    assert normalize_stream_error(ModelStreamCancelled("gone"), cancel_requested=True) is None
    failure = normalize_stream_error(ModelStreamCancelled("gone"), cancel_requested=False)
    assert isinstance(failure, RuntimeError)
    assert "unexpectedly" in str(failure)
    assert str(normalize_stream_error(RuntimeError("net"), cancel_requested=False)) == "net"
