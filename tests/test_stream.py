import pytest

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from agent.stream import StreamDeltaCallback, reasoning_text, visible_text
from agent.session import messages_to_transcript


def test_summary_callback_reports_lifecycle_and_keeps_summary_out_of_answer() -> None:
    events = []
    callback = StreamDeltaCallback(
        lambda kind, text: events.append((kind, text)),
        on_start=lambda: events.append("assistant_started"),
        on_end=lambda *_: events.append("assistant_completed"),
        on_compaction=lambda active: events.append(("compacting", active)),
    )
    for failed in (False, True):
        callback.on_llm_start({}, run_id="summary", metadata={"lc_source": "summarization"})
        callback.on_llm_new_token("内部压缩摘要", run_id="summary")
        if failed:
            callback.on_llm_error(RuntimeError("failed"), run_id="summary")
        else:
            callback.on_llm_end({}, run_id="summary")
    assert events == [("compacting", True), ("compacting", False)] * 2
    callback.on_llm_start({}, run_id="answer")
    callback.on_llm_new_token("回复", run_id="answer")
    callback.on_llm_end({}, run_id="answer")
    assert events[-3:] == ["assistant_started", ("assistant", "回复"), "assistant_completed"]


def test_reasoning_from_kwargs_and_think_tags() -> None:
    tagged = AIMessage(content="<think>先确认范围</think>接下来提问。")
    assert "先确认范围" in reasoning_text(tagged)
    assert visible_text(tagged) == "接下来提问。"

    extra = AIMessage(content="可见回复", additional_kwargs={"reasoning_content": "内部摘要"})
    assert reasoning_text(extra) == "内部摘要"
    assert visible_text(extra) == "可见回复"


def test_reasoning_from_content_blocks() -> None:
    message = AIMessage(content=[
        {"type": "reasoning", "summary": [{"text": "正在收敛目标"}]},
        {"type": "text", "text": "需要补充信息。"},
    ])
    assert reasoning_text(message) == "正在收敛目标"
    assert visible_text(message) == "需要补充信息。"


def test_restored_assistant_uses_the_live_content_parser() -> None:
    for message in (
        AIMessage(content="<think>先想</think>答案"),
        AIMessage(content=[
            {"type": "reasoning", "summary": [{"text": "先想"}]},
            {"type": "text", "text": "答案"},
        ]),
    ):
        restored = messages_to_transcript([message])[0]
        assert (restored.content, restored.thinking) == ("答案", "先想")


def test_stream_preserves_whitespace_and_repeated_chunks() -> None:
    events: list[tuple[str, str]] = []
    callback = StreamDeltaCallback(lambda kind, text: events.append((kind, text)))
    callback.on_llm_start({})
    for part in ("ha", "ha", " ", "def f():", "\n", "    ", "return 1"):
        callback.on_llm_new_token(part, chunk=AIMessageChunk(content=part))
    assert events[-1] == ("assistant", "haha def f():\n    return 1")


def test_stream_delta_callback_reads_generation_chunk_reasoning() -> None:
    events: list[tuple[str, str]] = []
    callback = StreamDeltaCallback(lambda kind, text: events.append((kind, text)))
    callback.on_llm_start({})
    message = AIMessageChunk(content="", additional_kwargs={"reasoning_content": "先分析"})
    callback.on_llm_new_token("", chunk=ChatGenerationChunk(message=message))
    callback.on_llm_new_token("答案", chunk=ChatGenerationChunk(message=AIMessageChunk(content="答案")))
    assert events[0] == ("reasoning", "先分析")
    assert ("assistant", "答案") in events


def test_stream_delta_callback_splits_think_and_answer() -> None:
    events: list[tuple[str, str]] = []
    callback = StreamDeltaCallback(lambda kind, text: events.append((kind, text)))
    callback.on_llm_start({})
    callback.on_llm_new_token(
        "",
        chunk=AIMessageChunk(content="<think>先想"),
    )
    callback.on_llm_new_token(
        "",
        chunk=AIMessageChunk(content="清楚</think>再回答。"),
    )
    callback.on_llm_new_token(
        "",
        chunk=AIMessageChunk(content="", additional_kwargs={"reasoning_content": "内部推理"}),
    )
    kinds = [kind for kind, _ in events]
    assert "reasoning" in kinds
    assert "assistant" in kinds
    assistant = [text for kind, text in events if kind == "assistant"]
    assert assistant[-1] == "再回答。"
    reasoning = [text for kind, text in events if kind == "reasoning"]
    assert "内部推理" in reasoning[-1] or "先想" in reasoning[0]


def test_usage_only_chunk_is_forwarded_and_summary_usage_is_ignored():
    usages = []
    callback = StreamDeltaCallback(lambda *_: None, on_usage=usages.append)
    callback.on_llm_start({}, run_id="answer")
    assert usages == [{}]
    callback.on_llm_new_token("text", chunk=AIMessageChunk(content="text"), run_id="answer")
    assert usages == [{}]
    callback.on_llm_new_token("", chunk=AIMessageChunk(content="", usage_metadata={
        "input_tokens": 0, "output_tokens": 2, "total_tokens": 2,
    }), run_id="answer")
    assert usages[-1] == {"input_tokens": 0, "output_tokens": 2, "total_tokens": 2}
    callback.on_llm_start({}, run_id="summary", metadata={"lc_source": "summarization"})
    callback.on_llm_new_token("", chunk=AIMessageChunk(content="", usage_metadata={
        "input_tokens": 999, "output_tokens": 2, "total_tokens": 1001,
    }), run_id="summary")
    assert len(usages) == 2
    callback.on_llm_start({}, run_id="next")
    assert usages[-1] == {}


def test_output_metrics_include_thinking_and_exclude_ttft_and_usage_tail(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("agent.stream.monotonic", lambda: now[0])
    reports = []
    cb = StreamDeltaCallback(lambda *_: None, on_metrics=reports.append)
    cb.on_llm_start({}, invocation_params={"model": "deepseek-flash"})
    for timestamp, reasoning, answer in [(1, "think", ""), (31, " more", "begin"), (41, "", " end")]:
        now[0] = timestamp
        cb.on_llm_new_token(answer, chunk=AIMessageChunk(content=answer, additional_kwargs={"reasoning_content": reasoning}))
    now[0] = 100  # Neither the usage-only tail nor callback completion adds generation time.
    cb.on_llm_new_token("", chunk=AIMessageChunk(content="", usage_metadata={
        "input_tokens": 12450, "output_tokens": 3000, "total_tokens": 15450,
        "output_token_details": {"reasoning": 2000},
    }))
    assert reports == []
    cb.on_llm_end(None)
    report = reports[0]
    assert report["model"] == "deepseek-flash" and report["thinking"] is True
    assert report["ttft"] == 1 and report["output_duration"] == 40
    assert report["reasoning_duration"] == 30 and report["answer_duration"] == 10
    assert report["reasoning_tokens"] == 2000 and report["answer_tokens"] == 1000
    assert report["tps"] == 75 and report["answer_tps"] == 100
    assert abs(report["reasoning_tps"] - 2000 / 30) < 1e-9


def test_output_metrics_retry_resets_failed_spans_and_preserves_thinking_counts(monkeypatch):
    from agent.llm_stream import ModelStreamInterrupted, stream_retry_state

    now = [0.0]
    monkeypatch.setattr("agent.stream.monotonic", lambda: now[0])
    reports = []
    cb = StreamDeltaCallback(lambda *_: None, on_metrics=reports.append)
    cb.on_llm_start({})
    now[0] = 10
    cb.on_llm_new_token("broken", chunk=AIMessageChunk(content="broken"))
    cb.on_retry(stream_retry_state(ModelStreamInterrupted("broken"), 1))
    now[0] = 100
    cb.on_llm_new_token("new", chunk=AIMessageChunk(content="new"))
    now[0] = 102
    cb.on_llm_new_token(" answer", chunk=AIMessageChunk(content=" answer", usage_metadata={
        "input_tokens": 10, "output_tokens": 20, "total_tokens": 30,
        "output_token_details": {"reasoning": 0},
    }))
    cb.on_llm_end(None)
    report = reports[0]
    assert report["tps"] == report["answer_tps"] == 10
    assert report["ttft"] == 100 and report["thinking"] is False
    assert report["reasoning_tps"] is None
    cb.on_llm_start({})
    cb.on_llm_error(RuntimeError("cancelled"))
    assert len(reports) == 1  # Failed calls never replace the latest snapshot.


def test_output_metrics_ignore_compaction_and_nonstreaming_has_no_fake_timings(monkeypatch):
    from langchain_core.outputs import ChatGeneration, LLMResult

    reports = []
    cb = StreamDeltaCallback(lambda *_: None, on_metrics=reports.append)
    cb.on_llm_start({}, run_id="summary", metadata={"lc_source": "summarization"})
    cb.on_llm_new_token("internal", run_id="summary")
    cb.on_llm_end(None, run_id="summary")
    assert reports == []
    cb.on_llm_start({}, metadata={"ls_model_name": "another-model"})
    cb.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(
        content="reply", usage_metadata={"input_tokens": 5, "output_tokens": 10, "total_tokens": 15},
    ))]]))
    report = reports[0]
    assert report["model"] == "another-model" and report["output_tokens"] == 10
    assert report["tps"] is report["ttft"] is report["reasoning_tokens"] is report["answer_tokens"] is None


def test_tool_argument_chunks_count_as_generated_output(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("agent.stream.monotonic", lambda: now[0])
    reports = []
    cb = StreamDeltaCallback(lambda *_: None, on_metrics=reports.append)
    cb.on_llm_start({})
    now[0] = 1
    cb.on_llm_new_token("", chunk=AIMessageChunk(content=""))  # Role/heartbeat only.
    now[0] = 2
    cb.on_llm_new_token("", chunk=AIMessageChunk(content="", tool_call_chunks=[{
        "name": "execute", "args": '{"command":', "id": "call-1", "index": 0,
    }]))
    now[0] = 4
    cb.on_llm_new_token("", chunk=AIMessageChunk(content="", tool_call_chunks=[{
        "name": None, "args": '"ls"}', "id": None, "index": 0,
    }], usage_metadata={"input_tokens": 5, "output_tokens": 20, "total_tokens": 25,
                       "output_token_details": {"reasoning": 0}}))
    cb.on_llm_end(None)
    assert reports[0]["ttft"] == 2
    assert reports[0]["tps"] == reports[0]["answer_tps"] == 10


def test_metrics_capture_reasoning_effort_for_both_protocols():
    for params in [{"model": "deepseek-flash", "reasoning_effort": "low"},
                   {"model": "qwen", "reasoning": {"effort": "high"}}]:
        reports = []
        callback = StreamDeltaCallback(lambda *_: None, on_metrics=reports.append)
        callback.on_llm_start({}, invocation_params=params)
        callback.on_llm_end(None)
        assert reports[0]["reasoning_effort"] in {"low", "high"}
        assert reports[0]["model"] == params["model"]


@pytest.mark.parametrize("kind", [{}, {"full": True}])
def test_reasoning_summary_kwargs_exclude_full_output_throughput(monkeypatch, kind):
    from agent.performance import model_metric_groups

    now = [0.0]
    monkeypatch.setattr("agent.stream.monotonic", lambda: now[0])
    reports = []
    callback = StreamDeltaCallback(lambda *_: None, on_metrics=reports.append)
    callback.on_llm_start({})
    now[0] = 1
    callback.on_llm_new_token("", chunk=AIMessageChunk(
        content="", additional_kwargs={"reasoning_summary": "brief summary"},
        response_metadata={"deep_agent_reasoning_kind": kind},
    ))
    now[0] = 2
    callback.on_llm_new_token("", chunk=AIMessageChunk(
        content="", additional_kwargs={"reasoning_summary": " continued"},
    ))
    now[0] = 3
    callback.on_llm_new_token("answer", chunk=AIMessageChunk(content="answer"))
    now[0] = 5
    callback.on_llm_new_token(" more", chunk=AIMessageChunk(content=" more", usage_metadata={
        "input_tokens": 10, "output_tokens": 30, "total_tokens": 40,
        "output_token_details": {"reasoning": 20},
    }))
    callback.on_llm_end(None)
    report = reports[0]
    assert report["output_tokens"] == 30 and report["reasoning_tokens"] == 20
    assert report["tps"] is report["reasoning_tps"] is report["output_duration"] is None
    assert report["answer_tps"] == 5 and report["answer_duration"] == 2
    assert model_metric_groups(reports)[0]["sample_count"] == 0


@pytest.mark.parametrize("extra", [
    {"reasoning_summary": value} for value in ("", None, 42, {}, [])
] + [
    {key: "full reasoning", "reasoning_summary": "brief summary"}
    for key in ("reasoning_content", "reasoning", "thinking")
])
def test_summary_detection_follows_reasoning_text_precedence(monkeypatch, extra):
    now = [0.0]
    monkeypatch.setattr("agent.stream.monotonic", lambda: now[0])
    reports = []
    callback = StreamDeltaCallback(lambda *_: None, on_metrics=reports.append)
    callback.on_llm_start({})
    for timestamp in (1, 2):
        now[0] = timestamp
        callback.on_llm_new_token("", chunk=AIMessageChunk(
            content=[{"type": "reasoning", "text": "full reasoning"}],
            additional_kwargs=extra,
        ))
    now[0] = 3
    callback.on_llm_new_token("answer", chunk=AIMessageChunk(content="answer", usage_metadata={
        "input_tokens": 10, "output_tokens": 30, "total_tokens": 40,
        "output_token_details": {"reasoning": 20},
    }))
    callback.on_llm_end(None)
    report = reports[0]
    assert report["tps"] == 15 and report["reasoning_tps"] == 20
