"""One-shot recovery context for model requests, never checkpoint messages."""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

from agent.session import StopReason


_active_context: ContextVar[RecoveryContext | None] = ContextVar("recovery_context", default=None)


@dataclass
class RecoveryContext:
    text: str | None
    armed: bool = False

    @classmethod
    def for_stop_reason(cls, reason: StopReason) -> "RecoveryContext | None":
        descriptions = {
            StopReason.PENDING: "The previous run did not record a normal end and may have exited unexpectedly.",
            StopReason.ABORTED: "The previous run was aborted by the user.",
            StopReason.ERROR: "The previous run ended with an execution error.",
        }
        description = descriptions.get(reason)
        if description is None:
            return None
        return cls(
            text="[Previous run status]\n"
            f"{description} Some side effects may have partially completed. "
            "Check the checkpoint and current workspace state before continuing. "
            "Do not assume unfinished operations succeeded or failed. "
            "Follow the user's current instruction."
        )


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
