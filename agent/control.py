"""Run-scoped control surface shared by the TUI, graph worker, and tool threads."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import threading
from typing import Any, Callable
from uuid import uuid4

from langgraph.runtime import RunControl

from agent.cancel import ToolCancelContext, clear_cancel_context, set_cancel_context

EventEmitter = Callable[[str, dict[str, Any]], None]


@dataclass
class QueuedMessage:
    text: str
    mode: str  # "steer" | "followUp"
    id: str = field(default_factory=lambda: str(uuid4()))


class RunController:
    """Thread-safe steering / follow-up queues, cancel state, and active tool registry."""

    def __init__(self, *, on_control_event: EventEmitter | None = None) -> None:
        self._lock = threading.RLock()
        self._steering: deque[QueuedMessage] = deque()
        self._follow_ups: deque[QueuedMessage] = deque()
        self._cancel_requested = False
        self._cancel_event = threading.Event()
        self._run_control: RunControl | None = None
        self._run_token: str = ""
        self._active_tools: dict[str, ToolCancelContext] = {}
        self._defer_steering = False
        self._on_control_event = on_control_event

    def begin_run(self) -> RunControl:
        with self._lock:
            self._cancel_requested = False
            self._cancel_event.clear()
            self._run_token = str(uuid4())
            self._run_control = RunControl()
            self._active_tools.clear()
            return self._run_control

    def end_run(self) -> None:
        with self._lock:
            self._run_control = None
            self._run_token = ""
            self._active_tools.clear()

    @property
    def run_control(self) -> RunControl | None:
        with self._lock:
            return self._run_control

    @property
    def run_token(self) -> str:
        with self._lock:
            return self._run_token

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def set_defer_steering(self, value: bool) -> None:
        with self._lock:
            self._defer_steering = value

    def steer(self, text: str) -> QueuedMessage:
        message = QueuedMessage(text=text, mode="steer")
        with self._lock:
            self._steering.append(message)
        self._emit("steering_queued", {"content": text, "mode": "steer", "id": message.id})
        return message

    def follow_up(self, text: str) -> QueuedMessage:
        message = QueuedMessage(text=text, mode="followUp")
        with self._lock:
            self._follow_ups.append(message)
        self._emit("steering_queued", {"content": text, "mode": "followUp", "id": message.id})
        return message

    def pop_steering(self) -> str | None:
        """Consume one steering message at a model-safe boundary (pi one-at-a-time)."""
        with self._lock:
            if self._defer_steering or self._cancel_requested:
                return None
            if not self._steering:
                return None
            message = self._steering.popleft()
        self._emit("steering_applied", {"content": message.text, "id": message.id})
        return message.text

    def has_pending_steering(self) -> bool:
        with self._lock:
            return bool(self._steering) and not self._defer_steering

    def pending_steering_count(self) -> int:
        with self._lock:
            return len(self._steering)

    def pending_follow_up_count(self) -> int:
        with self._lock:
            return len(self._follow_ups)

    def pop_follow_up(self) -> str | None:
        with self._lock:
            if not self._follow_ups:
                return None
            return self._follow_ups.popleft().text

    def take_unapplied(self) -> list[QueuedMessage]:
        """Alt+Up: reclaim steering/follow-up that has not entered a checkpoint yet."""
        with self._lock:
            messages = list(self._steering) + list(self._follow_ups)
            self._steering.clear()
            self._follow_ups.clear()
            return messages

    def cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True
            self._cancel_event.set()
            control = self._run_control
            tools = list(self._active_tools.values())
        self._emit("run_cancelling", {})
        if control is not None:
            control.request_drain("cancelled")
        for ctx in tools:
            ctx.request_cancel()

    def register_tool(self, ctx: ToolCancelContext) -> None:
        with self._lock:
            self._active_tools[ctx.tool_call_id or ctx.token] = ctx

    def unregister_tool(self, ctx: ToolCancelContext) -> None:
        with self._lock:
            self._active_tools.pop(ctx.tool_call_id or ctx.token, None)

    def open_tool_context(self, *, tool_name: str, tool_call_id: str) -> ToolCancelContext:
        ctx = ToolCancelContext(
            run_token=self.run_token,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            cancel_event=self._cancel_event,
        )
        if self.cancel_requested:
            ctx.request_cancel()
        self.register_tool(ctx)
        set_cancel_context(ctx)
        return ctx

    def close_tool_context(self, ctx: ToolCancelContext) -> None:
        self.unregister_tool(ctx)
        clear_cancel_context()

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._on_control_event is not None:
            try:
                self._on_control_event(event_type, payload)
            except Exception:  # noqa: BLE001
                pass
