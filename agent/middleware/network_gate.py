"""Honor declared NETWORK capability for execute tool calls."""
from __future__ import annotations

from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware

from agent.network import network_requested, reset_execute_network, set_execute_network


class NetworkGateMiddleware(AgentMiddleware):
    """Set per-invoke network ContextVar from execute(network=...) args.

    Under ask, HITL interrupts every execute; after approve the same args reach
    this middleware. Under sandboxed allow, declared network is auto-honored.
    Undeclared execute always runs with network=False (physical --unshare-net).
    """

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = str(tool_call.get("name") or getattr(getattr(request, "tool", None), "name", "") or "")
        if tool_name != "execute":
            return handler(request)
        args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
        token = set_execute_network(network_requested(args))
        try:
            return handler(request)
        finally:
            reset_execute_network(token)
