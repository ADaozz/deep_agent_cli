import asyncio
from types import SimpleNamespace

import langchain_openai.chat_models.base as lc_base
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from agent.llm import QwenChatOpenAI, normalize_qwen_responses_event


def _convert(event: object):
    return lc_base._convert_responses_chunk_to_generation_chunk(
        event, -1, -1, -1, output_version="responses/v1"
    )[3]


def test_qwen_reasoning_delta_is_normalized_without_mutating_sdk_event() -> None:
    raw = SimpleNamespace(
        type="response.reasoning_text.delta", item_id="rs_1", output_index=0,
        content_index=4, delta="思考", sequence_number=7,
    )
    # Regression baseline: stock LangChain 1.6.x ignores this Qwen event name.
    assert _convert(raw) is None

    normalized = normalize_qwen_responses_event(raw)
    assert normalized.type == "response.reasoning_summary_text.delta"
    assert normalized.summary_index == 0
    assert normalized.item_id == "rs_1"
    assert normalized.output_index == 0
    assert normalized.delta == "思考"
    assert normalized.sequence_number == 7
    assert raw.type == "response.reasoning_text.delta"

    generation = _convert(normalized)
    assert generation is not None
    assert generation.message.content[0]["type"] == "reasoning"
    assert generation.message.content[0]["summary"][0]["text"] == "思考"


def test_adapter_astream_emits_multiple_reasoning_deltas_before_text() -> None:
    events = [
        SimpleNamespace(type="response.reasoning_text.delta", item_id="rs_1", output_index=0, delta="a"),
        SimpleNamespace(type="response.reasoning_text.delta", item_id="rs_1", output_index=0, delta="b"),
        SimpleNamespace(type="response.output_text.delta", item_id="msg_1", output_index=1, content_index=0, delta="answer"),
    ]

    class FakeResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def __aiter__(self):
            async def stream():
                for event in events:
                    yield event
            return stream()

    class FakeResponses:
        async def create(self, **payload):
            assert payload["stream"] is True
            return FakeResponse()

    class FakeClient:
        responses = FakeResponses()

    llm = QwenChatOpenAI(
        model="qwen3.5-plus", api_key="sk-local", base_url="http://localhost:8000/v1",
        use_responses_api=True, output_version="responses/v1",
    )
    object.__setattr__(llm, "root_async_client", FakeClient())

    async def collect():
        return [item async for item in llm._astream([HumanMessage(content="x")])]

    blocks = [chunk.message.content[0] for chunk in asyncio.run(collect())]
    assert [block["type"] for block in blocks] == ["reasoning", "reasoning", "text"]
    assert "".join(block["summary"][0]["text"] for block in blocks[:2]) == "ab"
    assert blocks[2]["text"] == "answer"


def test_adapter_stream_emits_reasoning_deltas_before_text() -> None:
    events = [
        SimpleNamespace(type="response.reasoning_text.delta", item_id="rs_1", output_index=0, delta="a"),
        SimpleNamespace(type="response.reasoning_text.delta", item_id="rs_1", output_index=0, delta="b"),
        SimpleNamespace(type="response.output_text.delta", item_id="msg_1", output_index=1, content_index=0, delta="answer"),
    ]

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def __iter__(self):
            return iter(events)

    class FakeResponses:
        def create(self, **payload):
            assert payload["stream"] is True
            return FakeResponse()

    class FakeClient:
        responses = FakeResponses()

    llm = QwenChatOpenAI(
        model="qwen3.5-plus", api_key="sk-local", base_url="http://localhost:8000/v1",
        use_responses_api=True, output_version="responses/v1",
    )
    object.__setattr__(llm, "root_client", FakeClient())

    blocks = [chunk.message.content[0] for chunk in llm._stream([HumanMessage(content="x")])]
    assert [block["type"] for block in blocks] == ["reasoning", "reasoning", "text"]
    assert "".join(block["summary"][0]["text"] for block in blocks[:2]) == "ab"
    assert blocks[2]["text"] == "answer"


def test_gateway_extra_body_envelope_and_plain_chatopenai_stay_scoped() -> None:
    llm = QwenChatOpenAI(
        model="qwen3.5-plus", api_key="sk-local", base_url="http://localhost:8000/v1",
        use_responses_api=True, extra_body={"enable_thinking": True},
    )
    assert llm._get_request_payload([HumanMessage(content="x")])["extra_body"] == {
        "extra_body": {"enable_thinking": True}
    }
    assert QwenChatOpenAI._generate is ChatOpenAI._generate


def test_qwen_subclass_keeps_chatopenai_interface_but_provider_wire_shapes_differ() -> None:
    from agent.config import ModelProfile
    from agent.llm import build_chat_model

    qwen = build_chat_model(ModelProfile("qwen", "qwen3.5-plus"))
    compatible = build_chat_model(ModelProfile("generic", "qwen3.5-plus", provider="openai-compatible"))
    assert isinstance(qwen, ChatOpenAI)
    assert type(compatible) is ChatOpenAI
    assert "input" in qwen._get_request_payload([HumanMessage(content="hello")])
    assert "messages" in compatible._get_request_payload([HumanMessage(content="hello")])
    assert compatible.use_responses_api is False
