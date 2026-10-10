"""Exercise both protocols through the actual OpenAI SDK with memory transport."""
import asyncio
import json

import httpx
import openai
import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage

from agent.attachments import ATTACHMENT_META_KEY, AttachmentStore, image_attachment_from_bytes
from agent.config import ModelProfile
from agent.llm import build_chat_model, normalize_responses_event
from agent.stream import reasoning_text, visible_text


def response_body(usage):
    return {"id": "resp_1", "object": "response", "created_at": 0,
            "status": "completed", "model": "arbitrary-model", "output": [], "usage": usage}


def make_model(api, handler, *, profile=None, **kwargs):
    profile = profile or ModelProfile("s/arbitrary", "arbitrary-model", api=api,
                                      base_url="https://standard.example/v1")
    model = build_chat_model(profile, **kwargs)
    # OpenAI SDK still owns request serialization, SSE parsing and error mapping.
    sync = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    asynchronous = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    sync_sdk = openai.OpenAI(api_key="test", base_url="https://standard.example/v1", http_client=sync, max_retries=0)
    async_sdk = openai.AsyncOpenAI(api_key="test", base_url="https://standard.example/v1", http_client=asynchronous, max_retries=0)
    from agent.llm import _ResponsesClient
    model.root_client = _ResponsesClient(sync_sdk) if api == "responses" else sync_sdk
    model.root_async_client = _ResponsesClient(async_sdk) if api == "responses" else async_sdk
    model.client = sync_sdk.chat.completions
    model.async_client = async_sdk.chat.completions
    return model


def sse(events, *, done=False):
    content = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
    if done:
        content += 'data: [DONE]\n\n'
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("input_tokens", [0, 37, None])
def test_stream_thinking_tool_arguments_and_actual_usage(api, asynchronous, input_tokens):
    requests = []
    def serve(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["stream"] is True
        if api == "chat_completions":
            assert request.url.path == "/v1/chat/completions"
            assert body["stream_options"] == {"include_usage": True}
            deltas = [
                {"reasoning": "think"}, {"content": "answer"},
                {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":'}}]},
                {"tool_calls": [{"index": 0, "function": {"arguments": '"a"}'}}]},
            ]
            events = [{"id": "chat_1", "object": "chat.completion.chunk", "created": 0, "model": "arbitrary-model",
                       "choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in deltas]
            if input_tokens is not None:
                events.append({"id": "chat_1", "object": "chat.completion.chunk", "created": 0,
                               "model": "arbitrary-model", "choices": [], "usage": {
                                   "prompt_tokens": input_tokens, "completion_tokens": 9, "total_tokens": (input_tokens + 9),
                               }})
            return sse(events, done=True)
        assert request.url.path == "/v1/responses"
        assert "stream_options" not in body
        usage = None if input_tokens is None else {"input_tokens": input_tokens, "output_tokens": 9, "total_tokens": input_tokens + 9}
        return sse([
            {"type": "response.reasoning_text.delta", "sequence_number": 0, "output_index": 0, "item_id": "rs_1", "delta": "think"},
            {"type": "response.output_text.delta", "sequence_number": 1, "output_index": 1, "content_index": 0, "item_id": "msg_1", "delta": "answer"},
            {"type": "response.output_item.added", "sequence_number": 2, "output_index": 2,
             "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read_file", "arguments": "", "status": "in_progress"}},
            {"type": "response.function_call_arguments.delta", "sequence_number": 3, "output_index": 2, "item_id": "fc_1", "delta": '{"path":'},
            {"type": "response.function_call_arguments.delta", "sequence_number": 4, "output_index": 2, "item_id": "fc_1", "delta": '"a"}'},
            {"type": "response.completed", "sequence_number": 5, "response": response_body(usage)},
        ])
    model = make_model(api, serve)
    if asynchronous:
        async def collect():
            return [chunk async for chunk in model.astream([HumanMessage(content="hello")])]
        chunks = asyncio.run(collect())
    else:
        chunks = list(model.stream([HumanMessage(content="hello")]))
    combined = chunks[0]
    for chunk in chunks[1:]:
        combined += chunk
    assert reasoning_text(combined) == "think"
    assert visible_text(combined) == "answer"
    assert combined.tool_calls[0]["id"] == "call_1"
    assert combined.tool_calls[0]["args"] == {"path": "a"}
    if input_tokens is None:
        assert combined.usage_metadata is None
    else:
        assert combined.usage_metadata["input_tokens"] == input_tokens
    assert len(requests) == 1


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_request_local_images_with_both_protocols(api, asynchronous, tmp_path):
    store = AttachmentStore(tmp_path / "images")
    ref = store.put(image_attachment_from_bytes(b'\x89PNG\r\n\x1a\nfixture', filename='a.png'))
    original = HumanMessage(content="look", additional_kwargs={ATTACHMENT_META_KEY: [ref.to_dict()]})
    requests = []
    def serve(request):
        body = json.loads(request.content)
        requests.append(body)
        assert "base64" in str(body)
        if api == "responses":
            assert body["input"][0]["content"][1]["type"] == "input_image"
            return httpx.Response(200, json=response_body({"input_tokens": 10, "output_tokens": 0, "total_tokens": 10}))
        assert body["messages"][0]["content"][1]["type"] == "image_url"
        return httpx.Response(200, json={"id": "c", "model": "arbitrary-model", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})
    model = make_model(api, serve, attachment_store=store, streaming=False)
    if asynchronous:
        asyncio.run(model.ainvoke([original]))
    else:
        model.invoke([original])
    assert len(requests) == 1
    assert original.content == "look"
    assert original.additional_kwargs[ATTACHMENT_META_KEY] == [ref.to_dict()]


def test_standard_responses_extra_body_is_not_nested():
    model = build_chat_model(ModelProfile("standard", "qwen-anything", api="responses", base_url="https://standard.example/v1"))
    model.extra_body = {"custom": True}
    assert model._get_request_payload([HumanMessage(content="hi")])["extra_body"] == {"custom": True}
    # Protocol selection remains explicit even when the model looks like Qwen.
    assert model.use_responses_api is True


@pytest.mark.parametrize("fields,expected", [
    ({"reasoning_content": "one", "reasoning": "two"}, "one"),
    ({"reasoning_content": ["invalid"], "reasoning": "two"}, "two"),
    ({"reasoning_content": {"text": "invalid"}}, ""),
])
def test_optional_reasoning_is_typed_and_not_duplicated(fields, expected):
    model = build_chat_model(ModelProfile("s/m", "m", api="chat_completions"))
    chunk = model._convert_chunk_to_generation_chunk({"choices": [{"delta": fields}]}, AIMessageChunk, None)
    assert reasoning_text(chunk.message) == expected
    result = model._create_chat_result({"choices": [{"message": {"role": "assistant", "content": "answer", **fields}}]})
    assert reasoning_text(result.generations[0].message) == expected


def test_usage_missing_input_is_not_fabricated_as_zero():
    model = build_chat_model(ModelProfile("s/m", "m", api="chat_completions"))
    chunk = model._convert_chunk_to_generation_chunk({"choices": [], "usage": {"completion_tokens": 5, "total_tokens": 5}}, AIMessageChunk, None)
    assert chunk.message.usage_metadata is None


def test_include_usage_rejection_is_reported_without_fallback():
    requests = []
    def serve(request):
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "include_usage unsupported", "type": "invalid_request_error"}})
    model = make_model("chat_completions", serve)
    with pytest.raises(openai.BadRequestError, match="include_usage"):
        list(model.stream("hi"))
    assert len(requests) == 1
    assert requests[0]["stream_options"] == {"include_usage": True}


def test_known_dictionary_event_and_standard_object_identity():
    raw = {"type": "response.reasoning_text.delta", "delta": "thought", "output_index": 0}
    event = normalize_responses_event(raw)
    assert event.delta == "thought"
    assert raw["type"] == "response.reasoning_text.delta"
    standard = {"type": "response.output_text.delta", "delta": "text"}
    assert normalize_responses_event(standard) is standard


@pytest.mark.parametrize("streaming", [False, True])
def test_responses_missing_input_is_unknown(streaming):
    def serve(request):
        body = response_body({"output_tokens": 3, "total_tokens": 3})
        if streaming:
            return sse([{"type": "response.completed", "sequence_number": 0, "response": body}])
        return httpx.Response(200, json=body)
    model = make_model("responses", serve, streaming=streaming)
    result = model.invoke("hi")
    assert result.usage_metadata is None


@pytest.mark.parametrize("endpoint", [
    "https://standard.example/v1", "http://localhost:8000/v1",
    "http://127.0.0.1:8000/v1", "http://[::1]:8000/v1/",
    "http://localhost:9000/v1", "http://host.docker.internal:8000/v1",
    "http://127.0.0.2:8000/v1", "http://localhost:8000/other",
])
def test_extra_body_actual_sdk_serialization(endpoint):
    requests = []
    def serve(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_body(None))
    model = make_model("responses", serve, streaming=False)
    model.openai_api_base = endpoint
    model.root_client._target.base_url = endpoint
    model.invoke("hi", extra_body={"enable_thinking": True})
    assert requests[0]["enable_thinking"] is True
    assert "extra_body" not in requests[0]


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
@pytest.mark.parametrize("name", ["future-unlisted-model", "qwen-future", "gpt-6-future"])
@pytest.mark.parametrize("endpoint", ["https://new-provider.example/v1", "http://localhost:8000/v1"])
def test_protocol_selection_depends_only_on_api(api, name, endpoint):
    profile = ModelProfile("new/model", name, api=api, base_url=endpoint)
    model = build_chat_model(profile)
    payload = model._get_request_payload([HumanMessage(content="hi")])
    assert model.use_responses_api is (api == "responses")
    assert ("input" in payload) is (api == "responses")
    assert ("messages" in payload) is (api == "chat_completions")
    assert payload["model"] == name


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
def test_new_model_runs_in_existing_agent_from_configuration_only(api):
    from deepagents.backends import StateBackend
    from agent.config import Settings
    from agent.runner import AgentRunner
    settings = Settings.from_mapping({"llm": {
        "default": "new-provider/new-model", "models": {"new-provider": {
            "api": api, "base_url": "https://new-provider.example/v1", "api_key": "test",
            "reasoning_efforts": ["low"], "models": {"new-model": {
                "model": "future-unlisted-model", "input": ["text", "image"], "context_window": "128k",
            }},
        }},
    }})
    requests = []
    def serve(request):
        body = json.loads(request.content)
        requests.append(body)
        if api == "responses":
            assert request.url.path == "/v1/responses"
            assert body["reasoning"] == {"effort": "low"}
            return sse([
                {"type": "response.output_text.delta", "sequence_number": 0,
                 "output_index": 0, "content_index": 0, "item_id": "m", "delta": "done"},
                {"type": "response.completed", "sequence_number": 1,
                 "response": response_body({"input_tokens": 37, "output_tokens": 1, "total_tokens": 38})},
            ])
        assert request.url.path == "/v1/chat/completions"
        assert body["reasoning_effort"] == "low"
        assert body["stream_options"] == {"include_usage": True}
        return sse([
            {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
             "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": None}]},
            {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
             "choices": [], "usage": {"prompt_tokens": 37, "completion_tokens": 1, "total_tokens": 38}},
        ], done=True)
    model = make_model(api, serve, profile=settings.active_profile, reasoning_effort="low")
    runner = AgentRunner(settings=settings, model=model, backend=StateBackend())
    try:
        result = runner.invoke("hi")
        assert result.status == "completed"
        assert result.output == "done"
        assert runner.latest_usage()["input_tokens"] == 37
        assert runner.supports_input("image")
        assert runner.context_window() == 128000
        assert requests[0]["model"] == "future-unlisted-model"
    finally:
        runner.close()


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_reasoning_round_trip_uses_received_field(field, streaming, asynchronous):
    from langchain_core.messages import AIMessage, message_chunk_to_message
    requests = []
    def serve(request):
        requests.append(json.loads(request.content))
        if streaming:
            return sse([
                {"id": "chat_1", "object": "chat.completion.chunk", "created": 0,
                 "model": "arbitrary-model", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                for delta in [{field: "先"}, {field: "思考"}, {"content": "回答"}]
            ], done=True)
        return httpx.Response(200, json={"id": "chat_1", "object": "chat.completion", "created": 0,
            "model": "arbitrary-model", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "回答", field: "先思考"}}]})
    model = make_model("chat_completions", serve, streaming=streaming)
    async def invoke(messages):
        if streaming:
            chunks = [chunk async for chunk in model.astream(messages)]
            return message_chunk_to_message(sum(chunks[1:], chunks[0]))
        return await model.ainvoke(messages)
    def invoke_sync(messages):
        if streaming:
            chunks = list(model.stream(messages))
            return message_chunk_to_message(sum(chunks[1:], chunks[0]))
        return model.invoke(messages)
    call = (lambda messages: asyncio.run(invoke(messages))) if asynchronous else invoke_sync
    first = call([HumanMessage(content="第一轮")])
    assert reasoning_text(first) == "先思考"
    # 模拟 checkpoint 序列化与恢复，字段标记必须跨会话保留。
    restored = AIMessage.model_validate(first.model_dump())
    call([HumanMessage(content="第一轮"), restored, HumanMessage(content="第二轮")])
    outbound = requests[1]["messages"][1]
    assert outbound[field] == "先思考"
    assert ("reasoning" if field == "reasoning_content" else "reasoning_content") not in outbound
    assert "deep_agent_reasoning_fields" not in outbound


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("usage, expected", [
    ({"input_tokens": 0, "output_tokens": 1, "total_tokens": 1}, 0),
    ({"input_tokens": 37, "output_tokens": 1, "total_tokens": 38}, 37),
    (None, None), ({"output_tokens": 1, "total_tokens": 1}, None),
])
def test_responses_structured_output_normalizes_usage(asynchronous, usage, expected):
    from pydantic import BaseModel
    class Answer(BaseModel):
        answer: str
    def serve(request):
        body = response_body(usage)
        body["output"] = [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": '{"answer":"ok"}', "annotations": []}]}]
        return httpx.Response(200, json=body, headers={"x-test": "structured"})
    model = make_model("responses", serve, streaming=False)
    model.include_response_headers = True
    bound = model.bind(response_format=Answer)
    result = asyncio.run(bound.ainvoke("hi")) if asynchronous else bound.invoke("hi")
    assert result.additional_kwargs["parsed"] == Answer(answer="ok")
    assert result.response_metadata["headers"]["x-test"] == "structured"
    if expected is None:
        assert result.usage_metadata is None
    else:
        assert result.usage_metadata["input_tokens"] == expected


@pytest.mark.parametrize("fields, expected_field", [
    ({"reasoning_content": "one", "reasoning": "two"}, "reasoning_content"),
    ({"reasoning_content": ["invalid"], "reasoning": "two"}, "reasoning"),
])
def test_reasoning_alias_selection_is_preserved_in_outbound_payload(fields, expected_field):
    model = build_chat_model(ModelProfile("s/model", "model", api="chat_completions"), streaming=False)
    result = model._create_chat_result({"choices": [{"message": {"role": "assistant", "content": "ok", **fields}}]})
    payload = model._get_request_payload([result.generations[0].message])
    outbound = payload["messages"][0]
    assert outbound[expected_field] == fields[expected_field]
    assert ("reasoning" if expected_field == "reasoning_content" else "reasoning_content") not in outbound


def test_legacy_reasoning_message_keeps_existing_wire_field():
    from langchain_core.messages import AIMessage
    model = build_chat_model(ModelProfile("s/model", "model", api="chat_completions"), streaming=False)
    payload = model._get_request_payload([AIMessage(content="ok", additional_kwargs={"reasoning_content": "legacy"})])
    assert payload["messages"][0]["reasoning_content"] == "legacy"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("raw", [False, True])
def test_sdk_responses_parse_resource_normalizes_missing_input(asynchronous, raw):
    from pydantic import BaseModel
    class Answer(BaseModel):
        answer: str
    def serve(request):
        body = response_body({"output_tokens": 1, "total_tokens": 1})
        body["output"] = [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": '{"answer":"ok"}', "annotations": []}]}]
        return httpx.Response(200, json=body)
    model = make_model("responses", serve, streaming=False)
    resource = (model.root_async_client if asynchronous else model.root_client).responses
    if raw:
        resource = resource.with_raw_response
    async def parse():
        return await resource.parse(model="model", input="hi", text_format=Answer)
    result = asyncio.run(parse()) if asynchronous else resource.parse(model="model", input="hi", text_format=Answer)
    if raw:
        result = result.parse()
    assert result.usage is None
    assert result.output_parsed == Answer(answer="ok")
