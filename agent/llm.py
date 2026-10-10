"""Two explicit OpenAI protocols with local, optional-field compatibility."""
from __future__ import annotations

import base64
from typing import Any, ClassVar

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr, model_validator

from agent.attachments import ATTACHMENT_META_KEY, AttachmentStore, refs_from_message
from agent.config import ModelProfile


_REASONING_EVENT_TYPES = {
    "response.reasoning_text.delta": "response.reasoning_summary_text.delta",
    # 1.6.x does not consume this event yet, but normalize it without inventing
    # any fields in case an upstream converter starts doing so.
    "response.reasoning_text.done": "response.reasoning_summary_text.done",
}


class ResponsesEventProxy:
    """Read-only view over an OpenAI SDK event; the source object is untouched."""

    __slots__ = ("_event", "_type")

    def __init__(self, event: Any, event_type: str) -> None:
        self._event = event
        self._type = event_type

    @property
    def type(self) -> str:
        return self._type

    @property
    def response(self) -> Any:
        value = self._event.get("response") if isinstance(self._event, dict) else getattr(self._event, "response", None)
        return _normalize_response_usage(value)

    @property
    def summary_index(self) -> int:
        # Qwen's streamed reasoning is the final summary. content_index has a
        # different Responses meaning and must not be reused here.
        return 0

    def __getattr__(self, name: str) -> Any:
        return self._event.get(name) if isinstance(self._event, dict) else getattr(self._event, name)


def normalize_responses_event(event: Any) -> Any:
    """Normalize only Qwen's reasoning event spelling for LangChain 1.6.x."""
    raw_type = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
    event_type = _REASONING_EVENT_TYPES.get(raw_type)
    if event_type is not None:
        return ResponsesEventProxy(event, event_type)
    if raw_type in {"response.completed", "response.incomplete"}:
        response = event.get("response") if isinstance(event, dict) else getattr(event, "response", None)
        if _normalize_response_usage(response) is not response:
            return ResponsesEventProxy(event, raw_type)
    return event


def _normalize_response_usage(response: Any) -> Any:
    usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    if usage is None:
        return response
    count = usage.get("input_tokens") if isinstance(usage, dict) else getattr(usage, "input_tokens", None)
    if type(count) is int and count >= 0:
        return response
    # LangChain defaults a missing input count to zero. Optional/malformed
    # provider usage must remain Unknown, rather than inventing a valid zero.
    if isinstance(response, dict):
        return {**response, "usage": None}
    copier = getattr(response, "model_copy", None)
    if callable(copier):
        return copier(update={"usage": None})
    return _NoUsageResponse(response)


def materialize_attachment_refs(input_: Any, store: AttachmentStore | None) -> Any:
    """Return request-local messages with image bytes; leave checkpoint messages unchanged."""
    if not isinstance(input_, (list, tuple)):
        return input_
    messages: list[Any] = []
    changed = False
    for message in input_:
        refs = refs_from_message(message) if isinstance(message, HumanMessage) else ()
        if not refs:
            messages.append(message)
            continue
        if store is None:
            raise RuntimeError("Image attachment storage is not configured for this model")
        blocks: list[dict[str, Any]] = []
        content = message.content
        if isinstance(content, str):
            if content:
                blocks.append(create_text_block(content))
        elif isinstance(content, list):
            blocks.extend(content)
        for ref in refs:
            attachment = store.read(ref)
            blocks.append(create_image_block(
                base64=base64.b64encode(attachment.data).decode("ascii"),
                mime_type=attachment.mime_type,
            ))
        additional = dict(message.additional_kwargs)
        additional.pop(ATTACHMENT_META_KEY, None)
        messages.append(message.model_copy(update={
            "content": blocks,
            "additional_kwargs": additional,
        }))
        changed = True
    return messages if changed else input_


class _Delegate:
    def __init__(self, target: Any) -> None:
        self._target = target

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


class _NoUsageResponse(_Delegate):
    usage = None


class _EventStream(_Delegate):
    """Normalize known event spellings; SDK owns transport and stream lifetime."""
    def __enter__(self):
        self._target.__enter__()
        return self

    def __exit__(self, *args):
        return self._target.__exit__(*args)

    async def __aenter__(self):
        await self._target.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self._target.__aexit__(*args)

    def __iter__(self):
        for event in self._target:
            yield normalize_responses_event(event)

    async def __aiter__(self):
        async for event in self._target:
            yield normalize_responses_event(event)


class _RawStreamResponse(_Delegate):
    def __init__(self, target: Any, *, stream: bool = True) -> None:
        super().__init__(target)
        self._stream = stream

    def parse(self, *args, **kwargs):
        value = self._target.parse(*args, **kwargs)
        return _EventStream(value) if self._stream else _normalize_response_usage(value)


class _ResponsesResource(_Delegate):
    def __init__(self, target: Any, *, raw: bool = False) -> None:
        super().__init__(target)
        self._raw = raw

    @property
    def with_raw_response(self):
        return _ResponsesResource(self._target.with_raw_response, raw=True)

    def create(self, **kwargs):
        return self._call("create", kwargs)

    def parse(self, **kwargs):
        # Pydantic 结构化输出走 SDK parse，需与普通 create 共用响应兼容逻辑。
        return self._call("parse", kwargs)

    def _call(self, method: str, kwargs: dict[str, Any]):
        import inspect
        result = getattr(self._target, method)(**kwargs)
        def wrap(value):
            stream = bool(kwargs.get("stream"))
            if self._raw:
                return _RawStreamResponse(value, stream=stream)
            return _EventStream(value) if stream else _normalize_response_usage(value)
        if inspect.isawaitable(result):
            async def resolve():
                return wrap(await result)
            return resolve()
        return wrap(result)


class _ResponsesClient(_Delegate):
    @property
    def responses(self):
        return _ResponsesResource(self._target.responses)

    @property
    def with_raw_response(self):
        return _RawResponsesClient(self._target.with_raw_response)


class _RawResponsesClient(_Delegate):
    @property
    def responses(self):
        return _ResponsesResource(self._target.responses, raw=True)


class ProtocolChatOpenAI(ChatOpenAI):
    """Shared request-local attachment resolution and proxy-free transport."""
    _attachment_store: AttachmentStore | None = PrivateAttr(default=None)
    materializes_attachment_refs: ClassVar[bool] = True

    @model_validator(mode="before")
    @classmethod
    def _use_proxy_free_clients(cls, values: Any) -> Any:
        """Keep direct construction safe from inherited WSL SOCKS settings too."""
        if not isinstance(values, dict):
            return values
        if values.get("http_client") is None or values.get("http_async_client") is None:
            import httpx

            values = dict(values)
            values.setdefault("http_socket_options", ())
            if values.get("http_client") is None:
                values["http_client"] = httpx.Client(trust_env=False)
            if values.get("http_async_client") is None:
                values["http_async_client"] = httpx.AsyncClient(trust_env=False)
        return values

    def set_attachment_store(self, store: AttachmentStore) -> None:
        self._attachment_store = store

    def _get_request_payload(self, input_: Any, *, stop=None, **kwargs) -> dict[str, Any]:
        materialized = materialize_attachment_refs(input_, self._attachment_store)
        return super()._get_request_payload(materialized, stop=stop, **kwargs)


class ResponsesChatOpenAI(ProtocolChatOpenAI):
    """Responses API：兼容已知非标准响应事件，统一使用 SDK 请求格式。"""
    @model_validator(mode="after")
    def _normalize_response_streams(self):
        # Wrap SDK resource results, leaving LangChain's sync/async parsers intact.
        if not isinstance(self.root_client, _ResponsesClient):
            self.root_client = _ResponsesClient(self.root_client)
        if not isinstance(self.root_async_client, _ResponsesClient):
            self.root_async_client = _ResponsesClient(self.root_async_client)
        return self



def chat_openai(
    *,
    model: str,
    api_key: str,
    base_url: str,
    streaming: bool = True,
    attachment_store: AttachmentStore | None = None,
    reasoning_effort: str | None = None,
) -> ResponsesChatOpenAI:
    """Legacy Responses constructor; prefer build_chat_model with an API profile."""
    return build_chat_model(
        ModelProfile("legacy", model, api_key=api_key, base_url=base_url, api="responses",
                     reasoning_efforts=(reasoning_effort,) if reasoning_effort else ()),
        streaming=streaming, attachment_store=attachment_store, reasoning_effort=reasoning_effort,
    )


def _optional_reasoning(fields: Any) -> tuple[str, str] | None:
    # 兼容端点采用两种字段名；保留实际字段名用于历史回传，避免重复拼接。
    if not isinstance(fields, dict):
        return None
    for key in ("reasoning_content", "reasoning"):
        value = fields.get(key)
        if isinstance(value, str) and value:
            return key, value
    return None


class ChatCompletionsChatOpenAI(ProtocolChatOpenAI):
    """Preserve compatible providers' Chat Completions reasoning field."""

    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        generation = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info,
        )
        if generation is None or not isinstance(generation.message, AIMessageChunk):
            return generation
        raw_usage = chunk.get("usage")
        if isinstance(raw_usage, dict) and (type(raw_usage.get("prompt_tokens")) is not int or raw_usage["prompt_tokens"] < 0):
            generation.message.usage_metadata = None
        choices = chunk.get("choices") or (chunk.get("chunk") or {}).get("choices") or []
        if choices:
            reasoning = _optional_reasoning(choices[0].get("delta"))
            if reasoning is not None:
                field, text = reasoning
                generation.message.additional_kwargs["reasoning_content"] = text
                # 布尔映射可安全合并多个流式块，字符串标记则会被 LangChain 重复拼接。
                generation.message.response_metadata["deep_agent_reasoning_fields"] = {field: True}
        return generation

    def _create_chat_result(
        self, response: Any, generation_info: dict | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        data = response if isinstance(response, dict) else response.model_dump()
        raw_usage = data.get("usage") or {}
        if (type(raw_usage.get("prompt_tokens")) is not int or raw_usage["prompt_tokens"] < 0):
            for generation in result.generations:
                generation.message.usage_metadata = None
        for generation, choice in zip(result.generations, data.get("choices") or [], strict=False):
            reasoning = _optional_reasoning(choice.get("message"))
            if isinstance(generation.message, AIMessage) and reasoning is not None:
                field, text = reasoning
                generation.message.additional_kwargs["reasoning_content"] = text
                generation.message.response_metadata["deep_agent_reasoning_fields"] = {field: True}
        return result

    def _get_request_payload(
        self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if "messages" in payload:
            source = self._convert_input(input_).to_messages()
            for original, outbound in zip(source, payload["messages"], strict=False):
                if isinstance(original, AIMessage):
                    reasoning = original.additional_kwargs.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        fields = original.response_metadata.get("deep_agent_reasoning_fields")
                        # 旧会话无标记时沿用 reasoning_content；两个别名均出现时优先使用它。
                        uses_reasoning = (
                            isinstance(fields, dict)
                            and fields.get("reasoning") is True
                            and fields.get("reasoning_content") is not True
                        )
                        field = "reasoning" if uses_reasoning else "reasoning_content"
                        outbound[field] = reasoning
        return payload


# Deprecated Python aliases retained for existing integrations. Use the protocol names.
QwenChatOpenAI = ResponsesChatOpenAI
TokenPlanChatOpenAI = ChatCompletionsChatOpenAI
ReasoningChatOpenAI = ChatCompletionsChatOpenAI
normalize_qwen_responses_event = normalize_responses_event


def build_chat_model(
    profile: ModelProfile,
    *,
    streaming: bool = True,
    attachment_store: AttachmentStore | None = None,
    reasoning_effort: str | None = None,
) -> ChatOpenAI:
    """Build the template chat client from a ModelProfile."""
    if reasoning_effort is not None and reasoning_effort not in profile.reasoning_efforts:
        raise ValueError(f"Unsupported reasoning effort for {profile.id}: {reasoning_effort}")
    import httpx

    if profile.api == "responses":
        adapter = ResponsesChatOpenAI
        protocol_kwargs = {
            "use_responses_api": True, "output_version": "responses/v1",
            **({"reasoning": {"effort": reasoning_effort}} if reasoning_effort is not None else {}),
        }
    elif profile.api == "chat_completions":
        adapter = ChatCompletionsChatOpenAI
        protocol_kwargs = {
            "use_responses_api": False, "stream_usage": True,
            **({"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}),
        }
    else:
        raise ValueError(f"Unsupported model API: {profile.api}")
    model = adapter(
        model=profile.model, api_key=profile.api_key, base_url=profile.base_url,
        streaming=streaming, max_retries=2, timeout=600.0,
        http_socket_options=(), http_client=httpx.Client(trust_env=False),
        http_async_client=httpx.AsyncClient(trust_env=False), **protocol_kwargs,
    )
    object.__setattr__(model, "_deep_agent_model_id", profile.id)
    if attachment_store is not None:
        model.set_attachment_store(attachment_store)
    if profile.context_window > 0:
        # deepagents reads this model profile to choose its 85% compaction
        # threshold and to check the request's input budget.
        model.profile = {**(model.profile or {}), "max_input_tokens": profile.context_window}
    return model
