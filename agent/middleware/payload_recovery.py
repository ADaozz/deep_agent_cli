"""Recover oversized model requests without modifying conversation history."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage


_OMITTED = (
    "[部分工具结果因请求体过大而未发送。原始会话记录仍然保留；"
    "如需查看，请分段读取或缩小媒体文件。]"
)
_MAX_TEXT_CHARS = 4096
_MAX_TOOL_MEDIA_BYTES = 2 * 1024 * 1024
_MEDIA_TYPES = frozenset({"image", "image_url", "audio", "file"})


def _limit_tool_media(messages: list[Any], budget: int = _MAX_TOOL_MEDIA_BYTES) -> list[Any] | None:
    """Keep the newest tool media within a request budget, preserving history."""
    result = list(messages)
    changed = False
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, ToolMessage) or not isinstance(message.content, list):
            continue
        content = list(message.content)
        message_changed = False
        for pos in range(len(content) - 1, -1, -1):
            block = content[pos]
            if not isinstance(block, dict) or block.get("type") not in _MEDIA_TYPES:
                continue
            size = len(json.dumps(block, ensure_ascii=False, default=str).encode("utf-8"))
            if size <= budget:
                budget -= size
            else:
                content[pos] = {"type": "text", "text": _OMITTED}
                message_changed = changed = True
        if message_changed:
            result[index] = message.model_copy(update={"content": content})
    return result if changed else None


def _smaller_tool_results(messages: list[Any]) -> list[Any] | None:
    result = []
    changed = False
    for message in messages:
        if not isinstance(message, ToolMessage):
            result.append(message)
            continue
        if isinstance(message.content, str):
            if len(message.content) > _MAX_TEXT_CHARS:
                result.append(message.model_copy(update={"content": message.content[:_MAX_TEXT_CHARS] + "\n" + _OMITTED}))
                changed = True
            else:
                result.append(message)
            continue
        if not isinstance(message.content, list):
            result.append(message)
            continue
        content = []
        for block in message.content:
            if isinstance(block, dict) and block.get("type") in _MEDIA_TYPES:
                content.append({"type": "text", "text": _OMITTED})
                changed = True
            elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str) and len(block["text"]) > _MAX_TEXT_CHARS:
                content.append({**block, "text": block["text"][:_MAX_TEXT_CHARS] + "\n" + _OMITTED})
                changed = True
            elif len(json.dumps(block, ensure_ascii=False, default=str)) > _MAX_TEXT_CHARS:
                content.append({"type": "text", "text": _OMITTED})
                changed = True
            else:
                content.append(block)
        result.append(message.model_copy(update={"content": content}))
    return result if changed else None


class PayloadRecoveryMiddleware(AgentMiddleware):
    """Retry a 413 once with large tool results reduced in this request."""

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        limited = _limit_tool_media(request.messages)
        if limited is not None:
            request = request.override(messages=limited)
        try:
            return handler(request)
        except Exception as exc:
            if getattr(exc, "status_code", None) != 413:
                raise
            messages = _smaller_tool_results(request.messages)
            if messages is None:
                raise
            return handler(request.override(messages=messages))

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        limited = _limit_tool_media(request.messages)
        if limited is not None:
            request = request.override(messages=limited)
        try:
            return await handler(request)
        except Exception as exc:
            if getattr(exc, "status_code", None) != 413:
                raise
            messages = _smaller_tool_results(request.messages)
            if messages is None:
                raise
            return await handler(request.override(messages=messages))
