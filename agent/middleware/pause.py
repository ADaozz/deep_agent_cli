# Pause at the next model-call safe point via LangGraph interrupt().
# Do not build a custom pause engine; the checkpointer stores state and resume uses Command(resume=...).
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langgraph.types import interrupt


class PauseGateMiddleware(AgentMiddleware):
    def __init__(self, should_pause: Callable[[], bool]) -> None:
        super().__init__()
        self._should_pause = should_pause

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        if self._should_pause():
            interrupt({"type": "pause"})
        return handler(request)
