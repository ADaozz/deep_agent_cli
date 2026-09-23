"""Inject mid-turn steering HumanMessages at model-safe boundaries."""
from __future__ import annotations

from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import hook_config
from langchain_core.messages import HumanMessage

from agent.control import RunController


class SteeringMiddleware(AgentMiddleware):
    """Consume one steering message per before_model / after_agent boundary."""

    def __init__(self, controller: RunController) -> None:
        super().__init__()
        self._controller = controller

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ANN401
        return self._consume(state)

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ANN401
        update = self._consume(state)
        if update is None:
            return None
        update["jump_to"] = "model"
        return update

    def _consume(self, state: Any) -> dict[str, Any] | None:  # noqa: ANN401
        text = self._controller.pop_steering()
        if text is None:
            return None
        return {"messages": [HumanMessage(content=text)]}
