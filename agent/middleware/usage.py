"""Persist model identity with API usage without changing graph execution."""
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage


class UsageIdentityMiddleware(AgentMiddleware):
    def wrap_model_call(self, request: Any, handler: Callable) -> Any:
        response = handler(request)
        self._tag(response, request.model)
        return response

    async def awrap_model_call(self, request: Any, handler: Callable) -> Any:
        response = await handler(request)
        self._tag(response, request.model)
        return response

    @staticmethod
    def _tag(response: Any, model: Any) -> None:
        model_id = getattr(model, "_deep_agent_model_id", None)
        if model_id is None:
            return
        # Summarization can return an ExtendedModelResponse. Tag the ordinary
        # model result before checkpointing, never its internal summary usage.
        ordinary = getattr(response, "model_response", response)
        for message in getattr(ordinary, "result", []):
            if isinstance(message, AIMessage):
                message.response_metadata["deep_agent_model_id"] = model_id
