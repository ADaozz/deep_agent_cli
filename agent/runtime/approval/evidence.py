"""Review evidence for Flash: user messages, tool calls, the pending action, and the environment."""
from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from agent.subagents.notifications import NOTIFICATION_KEY

_EVIDENCE_LIMIT = 32000
_USER_MESSAGES = 20
_USER_TEXT_LIMIT = 2000
_HISTORY_CALLS = 20
_HISTORY_TEXT_LIMIT = 400
_ACTION_TEXT_LIMIT = 8000


def user_texts(messages: list[BaseMessage]) -> list[str]:
    """Text of the real user turns, oldest first. Host notifications are not user input."""
    return [
        text for message in messages
        if isinstance(message, HumanMessage) and NOTIFICATION_KEY not in message.additional_kwargs
        and (text := (message.text or "").strip())
    ]


def _clip(value: Any, limit: int) -> Any:
    """Shorten long strings anywhere in a JSON value and leave a marker the reviewer can see."""
    if isinstance(value, str):
        return value if len(value) <= limit else f"{value[:limit]}[truncated {len(value) - limit} chars]"
    if isinstance(value, dict):
        return {str(key): _clip(item, limit) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clip(item, limit) for item in value]
    return value


def build_evidence(
    messages: list[BaseMessage],
    call: dict[str, Any],
    environment: dict[str, Any] | None,
    *,
    parent_request: str | None = None,
) -> str | None:
    """Serialize what the reviewer may see, or None when there is nothing to judge authorization against.

    Assistant prose and tool output are left out, so neither the agent's own
    justification nor injected content can argue for approval.

    ``parent_request`` marks a subagent thread, whose human turn is the task
    description written by the parent agent rather than by the user.
    """
    if environment is None:
        return None
    texts = user_texts(messages)
    if not texts:
        return None
    payload: dict[str, Any] = {"user_messages": texts[-_USER_MESSAGES:]}
    if parent_request is not None:
        if not parent_request.strip():
            return None
        payload = {"user_messages": [parent_request.strip()], "delegated_task": texts[-1]}
    history = [
        {"name": item["name"], "args": _clip(item.get("args") or {}, _HISTORY_TEXT_LIMIT)}
        for message in messages if isinstance(message, AIMessage)
        for item in message.tool_calls or [] if item.get("id") != call.get("id")
    ][-_HISTORY_CALLS:]
    payload = {
        **_clip(payload, _USER_TEXT_LIMIT),
        "tool_calls": history,
        "action": {"tool": call["name"], "args": _clip(call.get("args") or {}, _ACTION_TEXT_LIMIT)},
        "environment": environment,
    }
    while True:
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) <= _EVIDENCE_LIMIT:
            return text
        if not history:
            return None
        del history[0]
