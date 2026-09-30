"""Inject steering before the next model or proposed tool call."""
from __future__ import annotations

from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ExtendedModelResponse, ModelResponse, hook_config
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from agent.control import RunController


class SteeringMiddleware(AgentMiddleware):
    """Apply steering before the next model call or before proposed tools start."""

    def __init__(self, controller: RunController) -> None:
        super().__init__()
        self._controller = controller

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ANN401
        return self._consume(state)

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:  # noqa: ANN401
        response = handler(request)
        text = self._controller.pop_steering()
        if text is None:
            return response
        # A proposed tool call has not reached approval or execution yet. Remove it
        # from the model result so both paths replan with the new user message.
        messages = [
            message.model_copy(update={
                "tool_calls": [],
                "invalid_tool_calls": [],
                "additional_kwargs": {
                    key: value for key, value in message.additional_kwargs.items()
                    if key != "tool_calls"
                },
            })
            if isinstance(message, AIMessage) and message.tool_calls else message
            for message in response.result
        ]
        return ExtendedModelResponse(
            model_response=ModelResponse(result=messages, structured_response=response.structured_response),
            command=Command(update={"messages": [HumanMessage(content=text)], "jump_to": "model"}),
        )

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
