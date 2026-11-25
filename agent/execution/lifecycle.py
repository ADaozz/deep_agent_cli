"""Unified command lifecycle for sync execute and yielded write_stdin polls."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Literal

from agent.execution.ownership import execution_thread_id
from agent.execution.pool import RemoteCommand, SandboxPool
from agent.execution.process import KILL_CONFIRM_SECONDS, LocalExecuteResponse, _OutputCollector
from agent.runtime.cancellation import emit_tool_output, get_cancel_context


StdinAction = Literal["write", "close", "cancel"]


def format_running_content(output: str, execution_id: str) -> str:
    body = output.rstrip() if output.strip() else "<no new output>"
    return f"{body}\n\n[running execution_id={execution_id}]"


@dataclass
class CommandSession:
    """One RemoteCommand with shared timeout/cancel/event handling."""

    handle: RemoteCommand
    pool: SandboxPool
    collector: _OutputCollector
    thread_id: str
    timeout: int | None
    deadline: float | None
    network: bool
    tool_call_id: str
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
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

    @property
    def execution_id(self) -> str:
        return self.handle.execution_id

    def owns(self, thread_id: str) -> bool:
        return self.thread_id == thread_id

    def start_background_pump(self, registry: ExecutionRegistry) -> None:
        self._registry = registry
        if self._pump is not None:
            return
        self._pump = threading.Thread(
            target=self._pump_loop, name=f"exec-pump-{self.execution_id}", daemon=True,
        )
        self._pump.start()

    def _pump_loop(self) -> None:
        try:
            while True:
                with self._lock:
                    if self._done:
                        break
                self.drive(wait=0.1)
                with self._lock:
                    if self._done:
                        break
        finally:
            with self._lock:
                finished = self._done
            if finished:
                self._emit_finished()

    def join_pump(self, timeout: float | None = 2.0) -> None:
        pump = self._pump
        if pump is not None and pump is not threading.current_thread():
            pump.join(timeout=timeout)

    def _emit_finished(self) -> None:
        # Only the background pump emits the terminal TUI update, once.
        with self._lock:
            if self._finish_emitted:
                return
            self._finish_emitted = True
        pending = self.collector.take_pending()
        if pending:
            emit_tool_output(self.tool_call_id, pending, stream="merged")
        response = self.to_response(include_pending=False)
        payload = (
            f"\n\n[finished execution_id={self.execution_id} "
            f"exit_code={response.exit_code} reason={response.termination_reason or 'exit'}]"
        )
        emit_tool_output(self.tool_call_id, payload, stream="execution_finished")
        if self._registry is not None:
            self._registry.discard(self.execution_id)

    def drive(self, *, wait: float) -> bool:
        """Advance one wait slice. Returns True when the command has finished."""
        with self._lock:
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
            with self._lock:
                return self._done
        kind, payload = event
        with self._lock:
            if kind == "out":
                self.collector.append(payload)
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
        try:
            self.handle.release()
        except Exception:  # noqa: BLE001
            pass
        self.collector.close()

    def wait_until(self, yield_deadline: float | None) -> LocalExecuteResponse:
        """Block until the process ends, or until yield_deadline (monotonic) if set."""
        while True:
            with self._lock:
                if self._done:
                    break
            now = time.monotonic()
            if yield_deadline is not None and now >= yield_deadline:
                return self.to_response(include_pending=True)
            wait = 0.1
            if yield_deadline is not None:
                wait = max(0.0, min(wait, yield_deadline - now))
            self.drive(wait=wait)
            with self._lock:
                if self._done:
                    break
        return self.to_response(include_pending=True)

    def write(self, data: str) -> None:
        with self._lock:
            if self._done:
                raise ValueError("execution has already finished")
        self.handle.write_stdin(data.encode("utf-8"))

    def close_stdin(self) -> None:
        with self._lock:
            if self._done:
                raise ValueError("execution has already finished")
        self.handle.close_stdin()

    def cancel(self) -> None:
        with self._lock:
            if self._done:
                return
            self._cancelled = True
            if self._kill_sent_at is None:
                self.handle.kill()
                self._kill_sent_at = time.monotonic()

    def apply_stdin(self, *, chars: str, action: StdinAction) -> None:
        """Apply a write_stdin action, surfacing write failures to the caller."""
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

    def to_response(self, *, include_pending: bool) -> LocalExecuteResponse:
        with self._lock:
            done = self._done
            cancelled = self._cancelled
            timed_out = self._timed_out
            failure = self._failure
            spawn_error = self._spawn_error
            exit_code = self._exit_code
            truncated = self.collector.truncated
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
                exit_code=int(exit_code or 0), timeout=self.timeout,
                cancelled=cancelled and not timed_out, timed_out=timed_out, failure=failure,
            )
            if pending and pending not in response.output:
                body = pending.rstrip()
                if response.output and response.output != "<no output>":
                    # Final tool content prefers unread delta + standard suffixes from response.
                    response.output = f"{body}\n\n{response.output}" if body else response.output
                elif body:
                    response.output = body
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


# Backwards-compatible alias used by older tests/imports.
LiveExecution = CommandSession


class ExecutionRegistry:
    """Per-SandboxPool map of yielded executions."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, CommandSession] = {}

    def register(self, session: CommandSession) -> CommandSession:
        with self._lock:
            self._items[session.execution_id] = session
        session.start_background_pump(self)
        return session

    def get(self, execution_id: str) -> CommandSession | None:
        with self._lock:
            return self._items.get(execution_id)

    def require(self, execution_id: str, *, thread_id: str | None = None) -> CommandSession:
        owner = execution_thread_id() if thread_id is None else thread_id
        session = self.get(execution_id)
        if session is None or session.handle.execution_id != execution_id:
            raise KeyError(f"Unknown or expired execution_id: {execution_id}")
        if session.handle.sandbox.lost_reason is not None and not session._done:
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
    return session.wait_until(time.monotonic() + max(0.0, yield_time))
