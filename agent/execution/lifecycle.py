"""Unified command lifecycle for sync execute and yielded write_stdin polls.

Only the background pump consumes ``RemoteCommand`` events. Tools wait on
session state and take unread snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any, Literal

from agent.execution.ownership import execution_thread_id
from agent.execution.pool import RemoteCommand, SandboxPool
from agent.execution.process import KILL_CONFIRM_SECONDS, LocalExecuteResponse, _OutputCollector
from agent.runtime.cancellation import ToolOutputEmitter, get_cancel_context, get_output_emitter


StdinAction = Literal["write", "close", "cancel"]


def format_running_content(output: str, execution_id: str) -> str:
    body = output.rstrip() if output.strip() else "<no new output>"
    return f"{body}\n\n[running execution_id={execution_id}]"


def write_stdin_is_side_effect(args: dict[str, Any] | None) -> bool:
    """Shared Ask/Auto predicate: empty write polls are not side effects."""
    payload = args or {}
    action = str(payload.get("action") or "write")
    chars = str(payload.get("chars") or "")
    return not (action == "write" and chars == "")


@dataclass
class CommandSession:
    """One RemoteCommand with a single pump-owned event loop."""

    handle: RemoteCommand
    pool: SandboxPool
    collector: _OutputCollector
    thread_id: str
    timeout: int | None
    deadline: float | None
    network: bool
    tool_call_id: str
    emit: ToolOutputEmitter | None = None
    _cv: threading.Condition = field(default_factory=threading.Condition, repr=False)
    _done: bool = False
    _exit_code: int | None = None
    _cancelled: bool = False
    _timed_out: bool = False
    _failure: str | None = None
    _spawn_error: str | None = None
    _kill_sent_at: float | None = None
    _pump: threading.Thread | None = field(default=None, repr=False)
    _registry: ExecutionRegistry | None = field(default=None, repr=False)
    _finish_emitted: bool = field(default=False, repr=False)
    _resources_released: bool = field(default=False, repr=False)
    _yielded: bool = False
    _result_consumed: bool = False
    _terminal: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def execution_id(self) -> str:
        return self.handle.execution_id

    def owns(self, thread_id: str) -> bool:
        return self.thread_id == thread_id

    def start_pump(self, registry: ExecutionRegistry | None = None) -> None:
        if registry is not None:
            self._registry = registry
        if self._pump is not None:
            return
        if self.emit is None:
            self.emit = get_output_emitter()
        self._pump = threading.Thread(
            target=self._pump_loop, name=f"exec-pump-{self.execution_id}", daemon=True,
        )
        self._pump.start()

    def _pump_loop(self) -> None:
        try:
            while True:
                with self._cv:
                    if self._done:
                        break
                self._drive(wait=0.1)
                with self._cv:
                    if self._done:
                        break
        finally:
            self._release_resources()
            self._emit_finished()
            with self._cv:
                self._cv.notify_all()

    def join_pump(self, timeout: float | None = 2.0) -> None:
        pump = self._pump
        if pump is not None and pump is not threading.current_thread():
            pump.join(timeout=timeout)

    def _emit_output(self, content: str = "", *, stream: str, result: Any = None) -> None:
        # Prefer the emitter captured at session start; fall back to the live ContextVar
        # so pump threads still publish when the tool thread had the emitter set.
        emitter = self.emit or get_output_emitter()
        if emitter is None:
            return
        if not content and result is None:
            return
        emitter(self.tool_call_id, content, stream, result=result, name="execute")

    def _terminal_result(self) -> dict[str, Any]:
        with self._cv:
            if self._terminal is not None:
                return dict(self._terminal)
            if self._spawn_error is not None:
                reason, exit_code = "spawn_error", self._exit_code if self._exit_code is not None else 1
            elif self._timed_out:
                reason, exit_code = "timeout", self._exit_code if self._exit_code is not None else 124
            elif self._cancelled:
                reason, exit_code = "cancelled", self._exit_code if self._exit_code is not None else 130
            elif self._failure is not None:
                reason, exit_code = "sandbox_lost", self._exit_code if self._exit_code is not None else 125
            else:
                reason, exit_code = None, self._exit_code
            payload = {
                "execution_id": self.execution_id,
                "running": False,
                "exit_code": exit_code,
                "termination_reason": reason,
                "truncated": self.collector.truncated,
                "network": self.network,
            }
            self._terminal = payload
            return dict(payload)

    def _emit_finished(self) -> None:
        with self._cv:
            if self._finish_emitted:
                return
            self._finish_emitted = True
        # Leave unread bytes in the collector for the agent final poll; TUI already
        # received streamed deltas. Only publish structured terminal state here.
        result = self._terminal_result()
        self._emit_output("", stream="execution_finished", result=result)

    def _drive(self, *, wait: float) -> bool:
        """Pump-only: advance one wait slice. Returns True when finished."""
        with self._cv:
            if self._done:
                return True
            now = time.monotonic()
            ctx = get_cancel_context()
            if self._kill_sent_at is None:
                if ctx is not None and ctx.cancelled:
                    self._cancelled = True
                elif self.deadline is not None and now >= self.deadline:
                    self._timed_out = True
                if self._cancelled or self._timed_out:
                    self.handle.kill()
                    self._kill_sent_at = now
                    self._cv.notify_all()
            elif now - self._kill_sent_at >= KILL_CONFIRM_SECONDS:
                self.pool.discard(self.handle.sandbox, "sandbox worker did not confirm a command kill")
                self._failure = "Sandbox worker did not confirm a command kill"
                self._exit_code = 125
                self._finish_locked()
                return True
            if self._kill_sent_at is None and self.deadline is not None:
                wait = max(0.0, min(wait, self.deadline - now))
        event = self.handle.next_event(wait)
        if event is None:
            with self._cv:
                return self._done
        kind, payload = event
        with self._cv:
            if kind == "out":
                self.collector.append(payload)
                self._cv.notify_all()
            elif kind == "exit":
                self._exit_code = int(payload)
                self.collector.flush()
                self._finish_locked()
            elif kind == "spawn_error":
                self._spawn_error = str(payload)
                self._finish_locked()
            else:
                self._failure = (
                    f"Sandbox worker stopped while this command was running ({payload}). "
                    "The command was not retried; the next execute starts a fresh sandbox."
                )
                self._exit_code = 125
                self.pool.discard(self.handle.sandbox, str(payload))
                self._finish_locked()
            return self._done

    def _finish_locked(self) -> None:
        if self._done:
            return
        self._done = True
        self._terminal_result()
        self._cv.notify_all()

    def _release_resources(self) -> None:
        with self._cv:
            if self._resources_released:
                return
            self._resources_released = True
        try:
            self.handle.release()
        except Exception:  # noqa: BLE001
            pass
        self.collector.close()

    def wait_until(self, yield_deadline: float | None) -> LocalExecuteResponse:
        """Wait for process end or yield_deadline. Never consumes RemoteCommand events."""
        self.start_pump(self._registry)
        with self._cv:
            while not self._done:
                now = time.monotonic()
                if yield_deadline is not None and now >= yield_deadline:
                    self._yielded = True
                    return self._snapshot_locked(include_pending=True)
                timeout = 0.1
                if yield_deadline is not None:
                    timeout = max(0.0, min(timeout, yield_deadline - now))
                self._cv.wait(timeout=timeout)
            return self._snapshot_locked(include_pending=True)

    def write(self, data: str) -> None:
        with self._cv:
            if self._done:
                raise ValueError("execution has already finished")
        self.handle.write_stdin(data.encode("utf-8"))

    def close_stdin(self) -> None:
        with self._cv:
            if self._done:
                raise ValueError("execution has already finished")
        self.handle.close_stdin()

    def cancel(self) -> None:
        with self._cv:
            if self._done:
                return
            self._cancelled = True
            if self._kill_sent_at is None:
                self.handle.kill()
                self._kill_sent_at = time.monotonic()
            self._cv.notify_all()

    def apply_stdin(self, *, chars: str, action: StdinAction) -> None:
        if action == "cancel":
            if chars:
                raise ValueError("cancel cannot include chars")
            self.cancel()
            return
        if action == "close":
            if chars:
                raise ValueError("close cannot include chars")
            self.close_stdin()
            return
        if action != "write":
            raise ValueError(f"unsupported write_stdin action: {action}")
        if chars:
            self.write(chars)

    def mark_result_consumed(self) -> None:
        with self._cv:
            self._result_consumed = True
        if self._registry is not None:
            self._registry.discard(self.execution_id)

    def to_response(self, *, include_pending: bool) -> LocalExecuteResponse:
        with self._cv:
            return self._snapshot_locked(include_pending=include_pending)

    def _snapshot_locked(self, *, include_pending: bool) -> LocalExecuteResponse:
        done = self._done
        cancelled = self._cancelled
        timed_out = self._timed_out
        failure = self._failure
        spawn_error = self._spawn_error
        exit_code = self._exit_code
        truncated = self.collector.truncated
        yielded = self._yielded
        pending = self.collector.take_pending() if include_pending else ""
        if spawn_error is not None and not (cancelled or timed_out):
            return LocalExecuteResponse(
                f"Error executing command ({spawn_error})", 1, False,
                termination_reason="spawn_error",
                execution_id=self.execution_id,
                running=False,
            )
        if done:
            response = self.collector.response(
                exit_code=int(exit_code or 0),
                timeout=self.timeout,
                cancelled=cancelled and not timed_out,
                timed_out=timed_out,
                failure=failure,
                unread_only=yielded,
                pending=pending if include_pending else "",
            )
            if yielded and include_pending and pending and pending not in response.output:
                # pending already applied via unread_only; keep response as-is
                pass
            response.execution_id = self.execution_id
            response.running = False
            return response
        content = format_running_content(pending, self.execution_id)
        return LocalExecuteResponse(
            content, None, truncated,
            termination_reason=None,
            execution_id=self.execution_id,
            running=True,
            max_output_bytes=self.collector.max_output_bytes,
        )


class ExecutionRegistry:
    """Per-SandboxPool map of live and retained terminal executions."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, CommandSession] = {}

    def register(self, session: CommandSession) -> CommandSession:
        with self._lock:
            self._items[session.execution_id] = session
        session.start_pump(self)
        return session

    def get(self, execution_id: str) -> CommandSession | None:
        with self._lock:
            return self._items.get(execution_id)

    def require(self, execution_id: str, *, thread_id: str | None = None) -> CommandSession:
        owner = execution_thread_id() if thread_id is None else thread_id
        session = self.get(execution_id)
        if session is None or session.handle.execution_id != execution_id:
            raise KeyError(f"Unknown or expired execution_id: {execution_id}")
        with session._cv:
            consumed = session._result_consumed
            done = session._done
            lost = session.handle.sandbox.lost_reason is not None and not done
        if consumed:
            raise KeyError(f"Unknown or expired execution_id: {execution_id}")
        if lost:
            raise KeyError(f"Unknown or expired execution_id: {execution_id}")
        if not session.owns(owner):
            raise PermissionError("execution_id belongs to another session or agent")
        return session

    def discard(self, execution_id: str) -> None:
        with self._lock:
            self._items.pop(execution_id, None)

    def shutdown(self) -> None:
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
        for item in items:
            item.cancel()
        for item in items:
            item.join_pump(timeout=2.0)


def interact(
    session: CommandSession,
    *,
    chars: str = "",
    yield_time: float = 1.0,
    action: StdinAction = "write",
) -> LocalExecuteResponse:
    session.apply_stdin(chars=chars, action=action)
    response = session.wait_until(time.monotonic() + max(0.0, yield_time))
    if not response.running:
        session.mark_result_consumed()
    return response
