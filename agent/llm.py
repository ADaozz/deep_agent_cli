"""Qwen Responses compatibility without patching ``langchain-openai`` globally."""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
import base64
from typing import Any, ClassVar

import langchain_openai.chat_models.base as _lc_base
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.messages.content import create_image_block, create_text_block
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr, model_validator

from agent.attachments import ATTACHMENT_META_KEY, AttachmentStore, refs_from_message
from agent.config import ModelProfile


_QWEN_REASONING_EVENT_TYPES = {
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
    def summary_index(self) -> int:
        # Qwen's streamed reasoning is the final summary. content_index has a
        # different Responses meaning and must not be reused here.
        return 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._event, name)


def normalize_qwen_responses_event(event: Any) -> Any:
    """Normalize only Qwen's reasoning event spelling for LangChain 1.6.x."""
    event_type = _QWEN_REASONING_EVENT_TYPES.get(getattr(event, "type", None))
    return event if event_type is None else ResponsesEventProxy(event, event_type)


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


class QwenChatOpenAI(ChatOpenAI):
    """Qwen Responses adapter with ChatOpenAI-compatible fallback routing."""

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

    def _stream(self, *args: Any, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        # ChatOpenAI routes directly to BaseChatOpenAI._stream_responses.
        if self._use_responses_api({**kwargs, **self.model_kwargs}):
            yield from self._stream_responses(*args, **kwargs)
        else:
            yield from super()._stream(*args, **kwargs)

    async def _astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
        # ChatOpenAI._astream directly calls BaseChatOpenAI._astream_responses,
        # so it would bypass an override of _astream_responses on this class.
        if self._use_responses_api({**kwargs, **self.model_kwargs}):
            async for chunk in self._astream_responses(*args, **kwargs):
                yield chunk
        else:
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Keep the Gateway's documented nested ``extra_body`` on the wire.

        OpenAI SDK treats an outer ``extra_body`` argument as transport options
        and merges it into JSON. This extra envelope is removed once by that
        SDK, producing Gateway's required ``{"extra_body": {...}}`` body.
        """
        materialized = self._materialize_attachments(input_)
        payload = super()._get_request_payload(materialized, stop=stop, **kwargs)
        if not self._use_responses_api({**kwargs, **self.model_kwargs}):
            return payload
        extra_body = payload.get("extra_body")
        if isinstance(extra_body, dict) and set(extra_body) != {"extra_body"}:
            payload["extra_body"] = {"extra_body": extra_body}
        return payload

    def _materialize_attachments(self, input_: Any) -> Any:
        return materialize_attachment_refs(input_, self._attachment_store)

    def _stream_responses(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """LangChain's sync Responses loop with Qwen event normalization."""
        self._ensure_sync_client_available()
        kwargs["stream"] = True
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        headers: dict[str, Any] = {}
        base_generation_info: dict[str, Any] = {}
        try:
            if self.include_response_headers or self._uses_gateway:
                raw_context_manager = self.root_client.with_raw_response.responses.create(**payload)
                context_manager = raw_context_manager.parse()
                if self.include_response_headers:
                    headers = {"headers": dict(raw_context_manager.headers)}
                _lc_base._add_gateway_metadata(base_generation_info, raw_context_manager)
            else:
                context_manager = self.root_client.responses.create(**payload)

            original_schema_obj = kwargs.get("response_format")
            with context_manager as response:
                is_first_chunk = True
                current_index = current_output_index = current_sub_index = -1
                has_reasoning = False
                for raw_chunk in response:
                    chunk = normalize_qwen_responses_event(raw_chunk)
                    metadata = headers if is_first_chunk else {}
                    current_index, current_output_index, current_sub_index, generation_chunk = (
                        _lc_base._convert_responses_chunk_to_generation_chunk(
                            chunk, current_index, current_output_index, current_sub_index,
                            schema=original_schema_obj, metadata=metadata,
                            has_reasoning=has_reasoning, output_version=self.output_version,
                        )
                    )
                    if generation_chunk:
                        if is_first_chunk and base_generation_info:
                            generation_chunk.generation_info = {
                                **base_generation_info,
                                **(generation_chunk.generation_info or {}),
                            }
                        if run_manager:
                            run_manager.on_llm_new_token(generation_chunk.text, chunk=generation_chunk)
                        is_first_chunk = False
                        if "reasoning" in generation_chunk.message.additional_kwargs:
                            has_reasoning = True
                        yield generation_chunk
        except _lc_base.openai.BadRequestError as error:
            _lc_base._handle_openai_bad_request(error)
        except _lc_base.openai.APIError as error:
            _lc_base._handle_openai_api_error(error)

    async def _astream_responses(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """LangChain's Responses loop with one normalization before conversion."""
        kwargs["stream"] = True
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        headers: dict[str, Any] = {}
        base_generation_info: dict[str, Any] = {}
        try:
            if self.include_response_headers or self._uses_gateway:
                raw_context_manager = await self.root_async_client.with_raw_response.responses.create(**payload)
                context_manager = raw_context_manager.parse()
                if self.include_response_headers:
                    headers = {"headers": dict(raw_context_manager.headers)}
                _lc_base._add_gateway_metadata(base_generation_info, raw_context_manager)
            else:
                context_manager = await self.root_async_client.responses.create(**payload)

            original_schema_obj = kwargs.get("response_format")
            async with context_manager as response:
                is_first_chunk = True
                current_index = current_output_index = current_sub_index = -1
                has_reasoning = False
                async for raw_chunk in _lc_base._astream_with_chunk_timeout(
                    response, self.stream_chunk_timeout, model_name=self.model_name
                ):
                    chunk = normalize_qwen_responses_event(raw_chunk)
                    metadata = headers if is_first_chunk else {}
                    current_index, current_output_index, current_sub_index, generation_chunk = (
                        _lc_base._convert_responses_chunk_to_generation_chunk(
                            chunk, current_index, current_output_index, current_sub_index,
                            schema=original_schema_obj, metadata=metadata,
                            has_reasoning=has_reasoning, output_version=self.output_version,
                        )
                    )
                    if generation_chunk:
                        if is_first_chunk and base_generation_info:
                            generation_chunk.generation_info = {
                                **base_generation_info,
                                **(generation_chunk.generation_info or {}),
                            }
                        if run_manager:
                            await run_manager.on_llm_new_token(generation_chunk.text, chunk=generation_chunk)
                        is_first_chunk = False
                        if "reasoning" in generation_chunk.message.additional_kwargs:
                            has_reasoning = True
                        yield generation_chunk
        except _lc_base.openai.BadRequestError as error:
            _lc_base._handle_openai_bad_request(error)
        except _lc_base.openai.APIError as error:
            _lc_base._handle_openai_api_error(error)


# Backwards-compatible template name. It now uses the Qwen Responses API when
# configured by chat_openai(), rather than the legacy Chat Completions field.
StreamingChatOpenAI = QwenChatOpenAI


def chat_openai(
    *,
    model: str,
    api_key: str,
    base_url: str,
    streaming: bool = True,
    attachment_store: AttachmentStore | None = None,
) -> QwenChatOpenAI:
    # Ignore ALL_PROXY/HTTP_PROXY from the shell (common WSL/SOCKS setups break
    # localhost gateways and require optional httpx[socks]).
    import httpx

    client = QwenChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        streaming=streaming,
        use_responses_api=True,
        output_version="responses/v1",
        reasoning={"effort": "low"},
        extra_body={"enable_thinking": True},
        http_socket_options=(),
        http_client=httpx.Client(trust_env=False),
        http_async_client=httpx.AsyncClient(trust_env=False),
    )
    if attachment_store is not None:
        client.set_attachment_store(attachment_store)
    return client


def build_chat_model(
    profile: ModelProfile,
    *,
    streaming: bool = True,
    attachment_store: AttachmentStore | None = None,
) -> ChatOpenAI:
    """Build the template chat client from a ModelProfile."""
    if profile.provider == "qwen-responses":
        return chat_openai(
            model=profile.model,
            api_key=profile.api_key,
            base_url=profile.base_url,
            streaming=streaming,
            attachment_store=attachment_store,
        )
    if profile.provider == "openai-compatible":
        import httpx

        return ChatOpenAI(
            model=profile.model,
            api_key=profile.api_key,
            base_url=profile.base_url,
            streaming=streaming,
            use_responses_api=False,
            http_socket_options=(),
            http_client=httpx.Client(trust_env=False),
            http_async_client=httpx.AsyncClient(trust_env=False),
        )
    raise ValueError(f"Unsupported model provider: {profile.provider}")
