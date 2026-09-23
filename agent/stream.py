# Token-level streaming for reasoning / assistant via LangChain callbacks.
# ChatOpenAI(..., streaming=True) emits AIMessageChunk; this splits think vs visible text.
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessageChunk, BaseMessage

DeltaHandler = Callable[[str, str], None]


def message_text(message: BaseMessage) -> str:
    text = getattr(message, "text", None)
    if isinstance(text, str) and text:
        return text
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts)


def _split_think(text: str) -> tuple[str, str]:
    """Split complete or streaming think tags without exposing partial tags."""
    visible: list[str] = []
    reasoning: list[str] = []
    lower = text.lower()
    cursor = 0
    while (start := lower.find("<think>", cursor)) >= 0:
        visible.append(_without_partial_tag(text[cursor:start]))
        end = lower.find("</think>", start + len("<think>"))
        if end < 0:
            reasoning.append(_without_partial_tag(text[start + len("<think>"):]))
            return "".join(visible), "".join(reasoning)
        reasoning.append(text[start + len("<think>"):end])
        cursor = end + len("</think>")
    visible.append(_without_partial_tag(text[cursor:]))
    return "".join(visible), "".join(reasoning)


def _without_partial_tag(text: str) -> str:
    marker = text.rfind("<")
    if marker >= 0 and any(
        tag.startswith(text[marker:].lower()) for tag in ("<think>", "</think>")
    ):
        return text[:marker]
    return text


def reasoning_text(message: BaseMessage) -> str:
    extra = getattr(message, "additional_kwargs", None) or {}
    for key in ("reasoning_content", "reasoning", "thinking", "reasoning_summary"):
        value = extra.get(key)
        if isinstance(value, str) and value:
            return value
    meta = getattr(message, "response_metadata", None) or {}
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            return value
    content = message.content
    parts: list[str] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "") not in {"reasoning", "thinking"}:
            continue
        summary = block.get("summary")
        if isinstance(summary, list):
            for item in summary:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or ""))
                elif item:
                    parts.append(str(item))
        parts.append(str(block.get("text") or block.get("reasoning") or block.get("reasoning_content") or ""))
    if parts:
        return "".join(parts)
    return _split_think(message_text(message))[1]


def visible_text(message: BaseMessage) -> str:
    return _split_think(message_text(message))[0]


def _chunk_message(chunk: Any) -> BaseMessage | None:
    if chunk is None:
        return None
    if isinstance(chunk, BaseMessage):
        return chunk
    message = getattr(chunk, "message", None)
    return message if isinstance(message, BaseMessage) else None


class StreamDeltaCallback(BaseCallbackHandler):
    """LangChain token callback → on_delta("reasoning"|"assistant", cumulative_text)."""

    def __init__(
        self,
        on_delta: DeltaHandler,
        *,
        on_reasoning: Callable[[str], None] | None = None,
        on_assistant: Callable[[str], None] | None = None,
        on_start: Callable[[], None] | None = None,
        on_end: Callable[[str, str], None] | None = None,
    ) -> None:
        super().__init__()
        self._on_delta = on_delta
        self._on_reasoning = on_reasoning
        self._on_assistant = on_assistant
        self._on_start = on_start
        self._on_end = on_end
        self._assistant = ""
        self._reasoning = ""
        self._message: AIMessageChunk | None = None
        self._raw_text = ""

    def on_llm_start(self, *args: Any, **kwargs: Any) -> None:
        self._assistant = ""
        self._reasoning = ""
        self._message = None
        self._raw_text = ""
        if self._on_start:
            self._on_start()

    def on_llm_new_token(self, token: str, *, chunk: Any = None, **kwargs: Any) -> None:
        message = _chunk_message(chunk)
        if isinstance(message, AIMessageChunk):
            self._message = message if self._message is None else self._message + message
            visible = visible_text(self._message)
            reasoning = reasoning_text(self._message)
        else:
            self._raw_text += token
            visible, reasoning = _split_think(self._raw_text)
        if reasoning != self._reasoning:
            self._reasoning = reasoning
            self._on_delta("reasoning", reasoning)
            if self._on_reasoning:
                self._on_reasoning(reasoning)
        if visible != self._assistant:
            self._assistant = visible
            self._on_delta("assistant", visible)
            if self._on_assistant:
                self._on_assistant(visible)

    def on_llm_end(self, *args: Any, **kwargs: Any) -> None:
        if self._on_end:
            self._on_end(self._assistant, self._reasoning)
        self._assistant = ""
        self._reasoning = ""


def merge_stream_callbacks(config: dict[str, Any], callback: BaseCallbackHandler) -> dict[str, Any]:
    merged = dict(config)
    existing = merged.get("callbacks")
    if isinstance(existing, list):
        merged["callbacks"] = [*existing, callback]
    elif existing:
        merged["callbacks"] = [existing, callback]
    else:
        merged["callbacks"] = [callback]
    return merged
