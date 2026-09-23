"""Optional cancellation protocol for synchronous tools and execute."""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
import threading
from typing import Any, Callable
from uuid import uuid4


CancelCallback = Callable[[], None]


@dataclass
class ToolCancelContext:
    tool_name: str
    tool_call_id: str
    cancel_event: threading.Event
    token: str = field(default_factory=lambda: str(uuid4()))
    _callbacks: list[CancelCallback] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _process: Any = None
    _cancelled: bool = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled or self.cancel_event.is_set()

    def register_callback(self, callback: CancelCallback) -> None:
        with self._lock:
            if self.cancelled:
                callback()
                return
            self._callbacks.append(callback)

    def register_process(self, process: Any) -> None:
        with self._lock:
            self._process = process
            if self.cancelled:
                _kill_process_group(process)

    def clear_process(self) -> None:
        with self._lock:
            self._process = None

    def request_cancel(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            callbacks = list(self._callbacks)
            process = self._process
        for callback in callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001
                pass
        if process is not None:
            _kill_process_group(process)


_CURRENT: ContextVar[ToolCancelContext | None] = ContextVar("deep_agent_cancel_ctx", default=None)
_OUTPUT_EMITTER: ContextVar[Callable[[str, str, str], None] | None] = ContextVar(
    "deep_agent_tool_output_emitter", default=None,
)


def set_cancel_context(ctx: ToolCancelContext) -> Token:
    return _CURRENT.set(ctx)


def clear_cancel_context() -> None:
    _CURRENT.set(None)


def get_cancel_context() -> ToolCancelContext | None:
    return _CURRENT.get()


def set_output_emitter(emitter: Callable[[str, str, str], None] | None) -> Token:
    return _OUTPUT_EMITTER.set(emitter)


def get_output_emitter() -> Callable[[str, str, str], None] | None:
    return _OUTPUT_EMITTER.get()


def emit_tool_output(tool_call_id: str, content: str, *, stream: str = "merged") -> None:
    emitter = get_output_emitter()
    if emitter is not None and content:
        emitter(tool_call_id, content, stream)


def _kill_process_group(process: Any) -> None:
    import os
    import signal

    pid = getattr(process, "pid", None)
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass
