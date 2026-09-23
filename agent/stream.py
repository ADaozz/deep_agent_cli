# Token-level streaming for reasoning / assistant via LangChain callbacks.
# ChatOpenAI(..., streaming=True) emits AIMessageChunk; this splits think vs visible text.
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage

THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
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


def reasoning_text(message: BaseMessage) -> str:
    extra = getattr(message, "additional_kwargs", None) or {}
    for key in ("reasoning_content", "reasoning", "thinking", "reasoning_summary"):
        value = extra.get(key)
        if isinstance(value, str) and value.strip():
            return value
    meta = getattr(message, "response_metadata", None) or {}
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
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
    parts.extend(item.strip() for item in THINK_BLOCK.findall(message_text(message)) if item.strip())
    return "".join(parts).strip()


def visible_text(message: BaseMessage) -> str:
    text = message_text(message)
    if not text:
        return ""
    visible = THINK_BLOCK.sub("", text)
    open_tag = re.search(r"<think>", visible, re.IGNORECASE)
    if open_tag:
        visible = visible[:open_tag.start()]
    return visible.strip()


def append_buf(current: str, delta: str) -> str:
    if not delta:
        return current
    if not current:
        return delta
    if delta.startswith(current):
        return delta
    if current.startswith(delta):
        return current
    return current + delta


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

    def on_llm_start(self, *args: Any, **kwargs: Any) -> None:
        self._assistant = ""
        self._reasoning = ""
        if self._on_start:
            self._on_start()

    def on_llm_new_token(self, token: str, *, chunk: Any = None, **kwargs: Any) -> None:
        message = _chunk_message(chunk)
        if message is not None:
            reasoning = reasoning_text(message)
            if reasoning:
                self._reasoning = append_buf(self._reasoning, reasoning)
                self._on_delta("reasoning", self._reasoning)
                if self._on_reasoning:
                    self._on_reasoning(self._reasoning)
            visible = visible_text(message)
            if visible:
                self._assistant = append_buf(self._assistant, visible)
                self._on_delta("assistant", self._assistant)
                if self._on_assistant:
                    self._on_assistant(self._assistant)
            return
        if token:
            self._assistant = append_buf(self._assistant, token)
            self._on_delta("assistant", self._assistant)
            if self._on_assistant:
                self._on_assistant(self._assistant)

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
