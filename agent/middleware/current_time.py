"""Refresh the user's local date and time zone on every model request."""
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage


class CurrentTimeMiddleware(AgentMiddleware):
    def __init__(self, timezone_name: str) -> None:
        super().__init__()
        self._timezone = ZoneInfo(timezone_name)

    def _with_current_time(self, request: Any) -> Any:
        date = datetime.now(self._timezone).strftime("%Y-%m-%d")
        text = f"# 当前用户时间\n当前日期：{date}\n用户时区：{self._timezone.key}"
        original = request.system_message
        if original is None:
            message = SystemMessage(content=text)
        elif isinstance(original.content, str):
            message = original.model_copy(update={"content": f"{original.content}\n\n{text}"})
        else:
            message = original.model_copy(update={
                "content": [*original.content, {"type": "text", "text": text}],
            })
        return request.override(system_message=message)

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(self._with_current_time(request))

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return await handler(self._with_current_time(request))
