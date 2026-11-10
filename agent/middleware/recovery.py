"""One-shot recovery context for model requests, never checkpoint messages."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

from agent.sessions.recovery import RecoveryContext


_active_context: ContextVar[RecoveryContext | None] = ContextVar("recovery_context", default=None)


def set_recovery_context(context: RecoveryContext | None) -> Token[RecoveryContext | None]:
    return _active_context.set(context)


def reset_recovery_context(token: Token[RecoveryContext | None]) -> None:
    _active_context.reset(token)


class RecoveryContextMiddleware(AgentMiddleware):
    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        context = _active_context.get()
        if context is None or not context.armed or context.text is None:
            return handler(request)
        original = request.system_message
        if original is None:
            system_message = SystemMessage(content=context.text)
        elif isinstance(original.content, str):
            system_message = original.model_copy(update={"content": f"{original.content}\n\n{context.text}"})
        else:
            system_message = original.model_copy(update={
                "content": [*original.content, {"type": "text", "text": context.text}],
            })
        response = handler(request.override(system_message=system_message))
        context.text = None
        return response
