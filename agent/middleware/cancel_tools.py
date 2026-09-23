"""Establish per-tool cancel contexts and optional cancellable tool hooks."""
from __future__ import annotations

from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from agent.control import RunController


class ToolCancelMiddleware(AgentMiddleware):
    """Wrap each tool call with a cancel context bound to the active RunController."""

    def __init__(self, controller: RunController) -> None:
        super().__init__()
        self._controller = controller

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = str(tool_call.get("name") or getattr(getattr(request, "tool", None), "name", "") or "tool")
        tool_call_id = str(tool_call.get("id") or "")
        ctx = self._controller.open_tool_context(tool_name=tool_name, tool_call_id=tool_call_id)
        tool = getattr(request, "tool", None)
        if tool is not None:
            cancel = getattr(tool, "cancel", None)
            if callable(cancel):
                ctx.register_callback(cancel)
        try:
            if ctx.cancelled:
                return ToolMessage(
                    content="Cancelled by user.",
                    tool_call_id=tool_call_id or tool_name,
                    name=tool_name,
                    status="error",
                )
            return handler(request)
        finally:
            self._controller.close_tool_context(ctx)
