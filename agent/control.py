"""Run-scoped control surface shared by the TUI, graph worker, and tool threads."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from itertools import count
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from langgraph.runtime import RunControl

from agent.cancel import ToolCancelContext, clear_cancel_context, set_cancel_context

EventEmitter = Callable[[str, dict[str, Any]], None]


@dataclass
class QueuedMessage:
    text: str
    mode: str  # "steer" | "followUp"
    order: int
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(eq=False)
class _RunScope:
    """Cancel state of one run generation.

    A scope is created either by ``reserve_run`` (the caller has dispatched an
    operation that has not reached ``begin_run`` yet) or by ``begin_run``.
    Cancelling a scope never affects another generation.
    """

    generation: int
    cancel_event: threading.Event = field(default_factory=threading.Event)
    run_control: RunControl | None = None
    model_streams: set[Any] = field(default_factory=set)


class RunController:
    """Thread-safe steering / follow-up queues, cancel state, and active tool registry."""

    def __init__(self, *, on_control_event: EventEmitter | None = None) -> None:
        self._lock = threading.RLock()
        self._steering: deque[QueuedMessage] = deque()
        self._follow_ups: deque[QueuedMessage] = deque()
        self._queue_order = count()
        self._pause_requested = False
        self._generations = count(1)
        self._active: _RunScope | None = None
        self._reserved: _RunScope | None = None
        self._last: _RunScope | None = None
        self._stream_scopes: dict[Any, _RunScope] = {}
        self._unclosed_streams: set[Any] = set()
        self._active_tools: dict[str, ToolCancelContext] = {}
        self._defer_steering = False
        self._on_control_event = on_control_event
        self._pending_config_check: Callable[[], bool] = lambda: False

    def set_pending_config_check(self, check: Callable[[], bool]) -> None:
        with self._lock:
            self._pending_config_check = check

    def has_pending_config(self) -> bool:
        with self._lock:
            check = self._pending_config_check
        return check()

    def reserve_run(self) -> int:
        """Reserve the generation of a run the caller is about to dispatch.

        A cancel issued before that run reaches ``begin_run`` is kept on the
        reservation instead of being cleared when the run starts.
        """
        with self._lock:
            if self._reserved is None:
                self._reserved = _RunScope(next(self._generations))
            return self._reserved.generation

    def release_reservation(self, generation: int | None = None) -> None:
        """Drop an unconsumed reservation (all of them when generation is None)."""
        with self._lock:
            if self._reserved is not None and generation in (None, self._reserved.generation):
                self._reserved = None

    def begin_run(self) -> RunControl:
        with self._lock:
            previous = self._active
            scope = self._reserved or _RunScope(next(self._generations))
            self._reserved = None
            if previous is not None:
                # begin_run without end_run continues the same operation
                # (follow-up turns), so an accepted cancel carries over.
                if previous.cancel_event.is_set():
                    scope.cancel_event.set()
                self._retire_streams(previous)
            scope.run_control = RunControl()
            self._active = scope
            self._last = scope
            self._active_tools.clear()
            cancelled = scope.cancel_event.is_set()
            control = scope.run_control
        if cancelled:
            control.request_drain("cancelled")
        return control

    def end_run(self) -> None:
        with self._lock:
            scope = self._active
            self._active = None
            self._active_tools.clear()
            if scope is not None:
                self._retire_streams(scope)

    @property
    def run_control(self) -> RunControl | None:
        with self._lock:
            return self._active.run_control if self._active is not None else None

    @property
    def run_generation(self) -> int | None:
        with self._lock:
            return self._active.generation if self._active is not None else None

    def _current_scope(self) -> _RunScope | None:
        return self._active or self._reserved or self._last

    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            scope = self._current_scope()
            return scope is not None and scope.cancel_event.is_set()

    @property
    def pause_requested(self) -> bool:
        with self._lock:
            return self._pause_requested

    def request_pause(self) -> None:
        with self._lock:
            self._pause_requested = True

    def clear_pause(self) -> None:
        with self._lock:
            self._pause_requested = False

    def set_event_handler(self, handler: EventEmitter | None) -> None:
        with self._lock:
            self._on_control_event = handler

    def set_defer_steering(self, value: bool) -> None:
        with self._lock:
            self._defer_steering = value

    def steer(self, text: str) -> QueuedMessage:
        with self._lock:
            message = QueuedMessage(text=text, mode="steer", order=next(self._queue_order))
            self._steering.append(message)
        self._emit("steering_queued", {"content": text, "mode": "steer", "id": message.id})
        return message

    def follow_up(self, text: str) -> QueuedMessage:
        with self._lock:
            message = QueuedMessage(text=text, mode="followUp", order=next(self._queue_order))
            self._follow_ups.append(message)
        self._emit("steering_queued", {"content": text, "mode": "followUp", "id": message.id})
        return message

    def pop_steering(self) -> str | None:
        """Consume one steering message at a model-safe boundary (pi one-at-a-time)."""
        with self._lock:
            if self._defer_steering or self.cancel_requested:
                return None
            if not self._steering:
                return None
            message = self._steering.popleft()
        self._emit("steering_applied", {"content": message.text, "id": message.id})
        return message.text

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
            messages = sorted((*self._steering, *self._follow_ups), key=lambda item: item.order)
            self._steering.clear()
            self._follow_ups.clear()
            return messages

    def cancel(self) -> None:
        """Cancel the active (or reserved) run; repeated calls are no-ops.

        State changes happen under the lock; draining the graph, cancelling
        model streams and tools, and emitting events happen after it.
        """
        with self._lock:
            scope = self._active or self._reserved
            if scope is None:
                # Idle: record the request without touching any future run.
                scope = self._last
                if scope is None:
                    scope = self._last = _RunScope(next(self._generations))
            self._pause_requested = False
            if scope.cancel_event.is_set():
                return
            scope.cancel_event.set()
            control = scope.run_control
            streams = list(scope.model_streams)
            tools = list(self._active_tools.values())
        for stream in streams:
            stream.cancel()
        if control is not None:
            control.request_drain("cancelled")
        for ctx in tools:
            ctx.request_cancel()
        self._emit("run_cancelling", {})

    def register_model_stream(self, stream: Any) -> bool:
        """Attach a model stream to the active run; False when that run is cancelled or absent."""
        with self._lock:
            scope = self._active
            if scope is None or scope.cancel_event.is_set():
                return False
            scope.model_streams.add(stream)
            self._stream_scopes[stream] = scope
            return True

    def unregister_model_stream(self, stream: Any) -> None:
        with self._lock:
            scope = self._stream_scopes.pop(stream, None)
            if scope is not None:
                scope.model_streams.discard(stream)
            if not stream.closed:
                self._unclosed_streams.add(stream)

    def unclosed_model_streams(self) -> int:
        """Finished-or-cancelled model streams whose HTTP request has not closed yet."""
        with self._lock:
            self._unclosed_streams = {item for item in self._unclosed_streams if not item.closed}
            return len(self._unclosed_streams)

    def wait_model_streams_closed(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._lock:
            streams = list(self._unclosed_streams)
        for stream in streams:
            if not stream.wait_closed(max(0.0, deadline - time.monotonic())):
                return False
        return self.unclosed_model_streams() == 0

    def _retire_streams(self, scope: _RunScope) -> None:
        for stream in scope.model_streams:
            self._stream_scopes.pop(stream, None)
            if not stream.closed:
                self._unclosed_streams.add(stream)
        scope.model_streams.clear()

    def register_tool(self, ctx: ToolCancelContext) -> None:
        with self._lock:
            self._active_tools[ctx.tool_call_id or ctx.token] = ctx

    def unregister_tool(self, ctx: ToolCancelContext) -> None:
        with self._lock:
            self._active_tools.pop(ctx.tool_call_id or ctx.token, None)

    def open_tool_context(self, *, tool_name: str, tool_call_id: str) -> ToolCancelContext:
        with self._lock:
            scope = self._current_scope()
            event = scope.cancel_event if scope is not None else threading.Event()
        ctx = ToolCancelContext(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            cancel_event=event,
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
