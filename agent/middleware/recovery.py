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
            StopReason.PENDING: "上一轮未记录正常结束状态，可能已异常退出。",
            StopReason.ABORTED: "上一轮已被用户中止。",
            StopReason.ERROR: "上一轮因执行错误结束。",
        }
        description = descriptions.get(reason)
        if description is None:
            return None
        return cls(
            text="[上一轮运行状态]\n"
            f"{description} 部分操作可能已经产生副作用。"
            "继续前先检查会话检查点和工作区现状。"
            "不要假定未完成的操作已经成功或失败。"
            "遵循用户当前的指令。"
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
