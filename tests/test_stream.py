from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from agent.stream import StreamDeltaCallback, append_buf, reasoning_text, visible_text


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


def test_append_buf() -> None:
    assert append_buf("hel", "hello") == "hello"
    assert append_buf("hello", " world") == "hello world"


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
        chunk=AIMessageChunk(content="<think>先想清楚</think>再回答。"),
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
