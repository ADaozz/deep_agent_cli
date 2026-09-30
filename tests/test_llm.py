import asyncio
from types import SimpleNamespace

import httpx
import langchain_openai.chat_models.base as lc_base
import openai
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_openai import ChatOpenAI

from agent.llm import QwenChatOpenAI, TokenPlanChatOpenAI, normalize_qwen_responses_event


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
            return SimpleNamespace(headers={"x-test": "async"}, parse=lambda: FakeResponse())

    class FakeClient:
        responses = FakeResponses()
        with_raw_response = SimpleNamespace(responses=responses)

    llm = QwenChatOpenAI(
        model="qwen3.5-plus", api_key="sk-local", base_url="http://localhost:8000/v1",
        use_responses_api=True, output_version="responses/v1", include_response_headers=True,
    )
    object.__setattr__(llm, "root_async_client", FakeClient())

    async def collect():
        return [item async for item in llm._astream([HumanMessage(content="x")])]

    chunks = asyncio.run(collect())
    blocks = [chunk.message.content[0] for chunk in chunks]
    assert chunks[0].message.response_metadata["headers"]["x-test"] == "async"
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
            return SimpleNamespace(headers={"x-test": "sync"}, parse=lambda: FakeResponse())

    class FakeClient:
        responses = FakeResponses()
        with_raw_response = SimpleNamespace(responses=responses)

    llm = QwenChatOpenAI(
        model="qwen3.5-plus", api_key="sk-local", base_url="http://localhost:8000/v1",
        use_responses_api=True, output_version="responses/v1", include_response_headers=True,
    )
    object.__setattr__(llm, "root_client", FakeClient())

    chunks = list(llm._stream([HumanMessage(content="x")]))
    blocks = [chunk.message.content[0] for chunk in chunks]
    assert chunks[0].message.response_metadata["headers"]["x-test"] == "sync"
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


def test_qwen_stream_preserves_context_overflow_error_mapping() -> None:
    request = httpx.Request("POST", "http://localhost:8000/v1/responses")
    response = httpx.Response(400, request=request)

    class FakeResponses:
        def create(self, **_payload):
            raise openai.BadRequestError(
                "context_length_exceeded", response=response, body={},
            )

    llm = QwenChatOpenAI(
        model="qwen3.5-plus", api_key="sk-local", base_url="http://localhost:8000/v1",
        use_responses_api=True,
    )
    object.__setattr__(llm, "root_client", SimpleNamespace(responses=FakeResponses()))
    with pytest.raises(lc_base.OpenAIContextOverflowError):
        list(llm._stream([HumanMessage(content="x")]))


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


def test_token_plan_preserves_streamed_and_saved_reasoning() -> None:
    from agent.config import ModelProfile
    from agent.llm import build_chat_model

    model = build_chat_model(ModelProfile(
        "plan/auto", "auto", provider="openai-compatible",
        base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    ))
    assert isinstance(model, TokenPlanChatOpenAI)
    chunk = model._convert_chunk_to_generation_chunk({
        "choices": [{"delta": {"role": "assistant", "reasoning_content": "先分析"}}],
    }, AIMessageChunk, None)
    assert chunk is not None
    assert chunk.message.additional_kwargs["reasoning_content"] == "先分析"

    result = model._create_chat_result({
        "model": "auto",
        "choices": [{"message": {
            "role": "assistant", "content": "答案", "reasoning_content": "先分析",
        }, "finish_reason": "stop"}],
    })
    message = result.generations[0].message
    assert message.additional_kwargs["reasoning_content"] == "先分析"
    from agent.stream import StreamDeltaCallback, reasoning_text

    assert reasoning_text(message) == "先分析"
    deltas: list[tuple[str, str]] = []
    callback = StreamDeltaCallback(lambda kind, text: deltas.append((kind, text)))
    callback.on_llm_start({})
    callback.on_llm_new_token("", chunk=chunk)
    assert deltas == [("reasoning", "先分析")]
    payload = model._get_request_payload([HumanMessage(content="问题"), message, HumanMessage(content="继续")])
    assert payload["messages"][1]["reasoning_content"] == "先分析"


def test_token_plan_profile_uses_reasoning_adapter_only_for_token_plan_host() -> None:
    from agent.config import ModelProfile
    from agent.llm import build_chat_model

    token_plan = build_chat_model(ModelProfile(
        "plan/auto", "auto", provider="openai-compatible",
        base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    ))
    generic = build_chat_model(ModelProfile(
        "generic/auto", "auto", provider="openai-compatible",
        base_url="https://example.com/v1",
    ))
    assert isinstance(token_plan, TokenPlanChatOpenAI)
    assert type(generic) is ChatOpenAI


@pytest.mark.parametrize("provider", ["qwen-responses", "openai-compatible"])
def test_configured_context_window_drives_deepagents_compaction(provider: str) -> None:
    from agent.config import ModelProfile
    from agent.llm import build_chat_model
    from deepagents.middleware.summarization import compute_summarization_defaults

    configured = build_chat_model(ModelProfile(
        "configured", "qwen3.5-plus", provider=provider, context_window=128_000,
    ))
    assert configured.profile is not None
    assert configured.profile["max_input_tokens"] == 128_000
    defaults = compute_summarization_defaults(configured)
    assert defaults["trigger"] == ("fraction", 0.85)
    assert round(configured.profile["max_input_tokens"] * defaults["trigger"][1]) == 108_800
    assert defaults["keep"] == ("fraction", 0.10)

    unknown = build_chat_model(ModelProfile("unknown", "qwen3.5-plus", provider=provider))
    assert compute_summarization_defaults(unknown)["trigger"] == ("tokens", 170_000)
