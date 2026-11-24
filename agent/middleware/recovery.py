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
    def _request(self, request: Any, context: RecoveryContext | None) -> Any:
        if context is None or not context.armed or context.text is None:
            return request
        original = request.system_message
        if original is None:
            system_message = SystemMessage(content=context.text)
        elif isinstance(original.content, str):
            system_message = original.model_copy(update={"content": f"{original.content}\n\n{context.text}"})
        else:
            system_message = original.model_copy(update={
                "content": [*original.content, {"type": "text", "text": context.text}],
            })
        return request.override(system_message=system_message)

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        context = _active_context.get()
        response = handler(self._request(request, context))
        if context is not None and context.armed:
            context.text = None
        return response

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        context = _active_context.get()
        response = await handler(self._request(request, context))
        if context is not None and context.armed:
            context.text = None
        return response
