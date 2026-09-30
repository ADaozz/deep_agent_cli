"""Stop before a new model call so the runner can rebuild its graph safely."""
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langgraph.types import interrupt


class RuntimeConfigGateMiddleware(AgentMiddleware):
    def __init__(self, has_pending: Callable[[], bool]) -> None:
        super().__init__()
        self._has_pending = has_pending

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        if self._has_pending():
            interrupt({"type": "runtime_config_boundary"})
        return handler(request)
