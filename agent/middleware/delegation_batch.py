"""Reject a tool-call batch that mixes synchronous and background delegation.

The check runs before any start_task body. A mixed batch starts nothing and
does not take a concurrency slot. Other tools in the same batch still run.
"""

from __future__ import annotations

from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

from agent.subagents.notifications import background_requested

MIXED_BATCH = (
    "Synchronous and background subagents cannot be launched in the same tool-call batch. "
    "Separate them into different batches and retry."
)


def batch_is_mixed(state: Any) -> bool:
    messages = state.get("messages") if isinstance(state, dict) else getattr(state, "messages", None)
    if not messages:
        return False
    message = next(
        (item for item in reversed(messages) if isinstance(item, AIMessage) and item.tool_calls),
        None,
    )
    if message is None:
        return False
    modes = [
        background_requested(call.get("args"))
        for call in message.tool_calls
        if call.get("name") == "start_task"
    ]
    return any(modes) and not all(modes)


class DelegationBatchMiddleware(AgentMiddleware):
    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        rejected = self._reject(request)
        if rejected is not None:
            return rejected
        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        rejected = self._reject(request)
        if rejected is not None:
            return rejected
        return await handler(request)

    def _reject(self, request: Any) -> ToolMessage | None:
        call = request.tool_call
        if call.get("name") != "start_task" or not batch_is_mixed(request.state):
            return None
        return ToolMessage(
            content=MIXED_BATCH,
            name="start_task",
            tool_call_id=str(call.get("id") or ""),
            status="error",
        )
