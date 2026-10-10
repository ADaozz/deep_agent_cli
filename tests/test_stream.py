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
