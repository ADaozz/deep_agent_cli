"""Unified command lifecycle for sync execute and yielded write_stdin polls.

Only the background pump consumes ``RemoteCommand`` events. Tools wait on
session state and take unread snapshots. ``CommandSession`` is the single
authority for terminal state; both ``ToolMessage`` and ``RunEvent`` derive from
it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from typing import Any, Callable, Literal

from agent.execution.ownership import execution_thread_id
from agent.execution.pool import RemoteCommand, SandboxPool
from agent.execution.process import (
    KILL_CONFIRM_SECONDS,
    LocalExecuteResponse,
    _OutputCollector,
    format_terminal_output,
)
from agent.runtime.cancellation import (
    ToolCancelContext,
    ToolOutputEmitter,
    emit_output_with,
)


StdinAction = Literal["write", "close", "cancel"]

DEFAULT_MAX_TERMINAL_RECORDS = 64
DEFAULT_TERMINAL_TTL_SECONDS = 300.0


def format_running_content(output: str, execution_id: str) -> str:
    body = output.rstrip() if output.strip() else "<no new output>"
    return f"{body}\n\n[running execution_id={execution_id}]"


@dataclass
class CommandSession:
    """One RemoteCommand with a single pump-owned event loop and terminal state."""

    handle: RemoteCommand | None
    pool: SandboxPool
    collector: _OutputCollector
    thread_id: str
    timeout: int | None
    deadline: float | None
    network: bool
    tool_call_id: str
    sandbox_mode: str = "restricted"
    emit: ToolOutputEmitter | None = None
    cancel_context: ToolCancelContext | None = field(default=None, repr=False)
    _execution_id: str = field(init=False, default="")
    _stdin_mode: str = field(init=False, default="none")
    _cv: threading.Condition = field(default_factory=threading.Condition, repr=False)
    _done: bool = False
    _exit_code: int | None = None
    _cancelled: bool = False
    _timed_out: bool = False
    _failure: str | None = None
    _spawn_error: str | None = None
    _kill_sent_at: float | None = None
    _finished_at: float | None = None
    _pump: threading.Thread | None = field(default=None, repr=False)
    _registry: ExecutionRegistry | None = field(default=None, repr=False)
    _finish_emitted: bool = field(default=False, repr=False)
    _resources_released: bool = field(default=False, repr=False)
    _yielded: bool = False
    _result_consumed: bool = False
    _terminal: dict[str, Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.handle is None:
            raise ValueError("CommandSession requires an active RemoteCommand handle")
        self._execution_id = self.handle.execution_id
        self._stdin_mode = self.handle.stdin_mode
        if self.cancel_context is not None:
            self.cancel_context.register_process(self)

    @property
    def execution_id(self) -> str:
        return self._execution_id

    @property
    def stdin_mode(self) -> str:
        return self._stdin_mode

    @property
    def is_done(self) -> bool:
        with self._cv:
            return self._done

    @property
    def finished_at(self) -> float | None:
        with self._cv:
            return self._finished_at

    def owns(self, thread_id: str) -> bool:
        return self.thread_id == thread_id

    def detach_cancel_context(self) -> None:
        """Unbind the launching ToolCancelContext (e.g. after execute yields)."""
        with self._cv:
            ctx = self.cancel_context
            self.cancel_context = None
        if ctx is not None:
            ctx.clear_process(self)

    def start_pump(self, registry: ExecutionRegistry | None = None) -> None:
        if registry is not None:
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
                with self._cv:
                    if self._done:
                        break
                if self._drive(wait=0.1):
                    break
        except Exception as exc:  # noqa: BLE001
            with self._cv:
                if not self._done and self._failure is None and self._spawn_error is None:
                    self._failure = f"Execution pump error ({type(exc).__name__}): {exc}"
        finally:
            self._release_resources()
            self.detach_cancel_context()
            self._finalize_terminal()
            if self._registry is not None:
                self._registry.on_finished(self)
            self._emit_finished()

    def join_pump(self, timeout: float | None = 2.0) -> None:
        pump = self._pump
        if pump is not None and pump is not threading.current_thread():
            pump.join(timeout=timeout)

    def _emit_output(self, content: str = "", *, stream: str, result: Any = None) -> None:
        emit_output_with(
            self.emit,
            self.tool_call_id,
            content,
            stream=stream,
            result=result,
            name="execute",
            thread_id=self.thread_id,
            execution_id=self.execution_id,
        )

    def _compute_terminal_locked(self) -> dict[str, Any]:
        """Single source of truth for exit_code, termination_reason, and log metadata."""
        if self._terminal is not None:
            return dict(self._terminal)
        if self._spawn_error is not None and not (self._cancelled or self._timed_out):
            reason, exit_code = "spawn_error", 1
        elif self._failure is not None:
            reason, exit_code = "sandbox_lost", 125
        elif self._timed_out:
            reason, exit_code = "timeout", 124
        elif self._cancelled:
            reason, exit_code = "cancelled", 130
        else:
            reason, exit_code = None, int(self._exit_code if self._exit_code is not None else 0)
        meta = self.collector.log_metadata()
        payload = {
            "execution_id": self.execution_id,
            "running": False,
            "exit_code": exit_code,
            "termination_reason": reason,
            "truncated": meta["truncated"],
            "host_log_path": meta["host_log_path"],
            "agent_log_path": meta["agent_log_path"],
            "log_error": meta["log_error"],
            "max_output_bytes": meta["max_output_bytes"],
        }
        self._terminal = payload
        return dict(payload)

    def _finalize_terminal(self) -> None:
        now = self._registry.now() if self._registry is not None else time.monotonic()
        with self._cv:
            if not self._done:
                self._compute_terminal_locked()
                self._finished_at = now
                self._done = True
            self._cv.notify_all()

    def _emit_finished(self) -> None:
        with self._cv:
            if self._finish_emitted:
                return
            self._finish_emitted = True
            result = self._compute_terminal_locked()
        # Leave unread bytes in the collector for the agent final poll; TUI already
        # received streamed deltas. Only publish structured terminal state here.
        self._emit_output("", stream="execution_finished", result=result)

    def _cancel_locked(self) -> None:
        if self._done:
            return
        self._cancelled = True
        if self._kill_sent_at is None:
            if self.handle is not None:
                self.handle.kill()
            self._kill_sent_at = time.monotonic()
        self._cv.notify_all()

    def _drive(self, *, wait: float) -> bool:
        """Pump-only: advance one wait slice. Returns True once a terminal event arrives."""
        discard_sandbox = None
        with self._cv:
            if self._done:
                return True
            now = time.monotonic()
            ctx = self.cancel_context
            if self._kill_sent_at is None:
                if ctx is not None and ctx.cancelled:
                    self._cancel_locked()
                elif self.deadline is not None and now >= self.deadline:
                    self._timed_out = True
                    if self.handle is not None:
                        self.handle.kill()
                    self._kill_sent_at = now
                    self._cv.notify_all()
            elif now - self._kill_sent_at >= KILL_CONFIRM_SECONDS:
                self._failure = "Sandbox worker did not confirm a command kill"
                self._exit_code = 125
                if self.handle is not None:
                    discard_sandbox = self.handle.sandbox
            handle = self.handle
            if discard_sandbox is None and handle is None:
                return True
            if self._kill_sent_at is None and self.deadline is not None:
                wait = max(0.0, min(wait, self.deadline - now))
        if discard_sandbox is not None:
            self.pool.discard(discard_sandbox, "sandbox worker did not confirm a command kill")
            return True
        assert handle is not None
        event = handle.next_event(wait)
        if event is None:
            return False
        kind, payload = event
        if kind == "out":
            self.collector.append(payload)
            with self._cv:
                self._cv.notify_all()
            return False
        if kind == "exit":
            self.collector.flush()
            with self._cv:
                self._exit_code = int(payload)
            return True
        if kind == "spawn_error":
            with self._cv:
                self._spawn_error = str(payload)
            return True
        self.collector.flush()
        self.pool.discard(handle.sandbox, str(payload))
        with self._cv:
            self._failure = (
                f"Sandbox worker stopped while this command was running ({payload}). "
                "The command was not retried; the next execute starts a fresh sandbox."
            )
            self._exit_code = 125
        return True

    def _release_resources(self) -> None:
        with self._cv:
            if self._resources_released:
                return
            self._resources_released = True
            handle = self.handle
            self.handle = None
        if handle is not None:
            try:
                handle.release()
            except Exception:  # noqa: BLE001
                pass
        self.collector.close()

    def wait_until(self, yield_deadline: float | None) -> LocalExecuteResponse:
        """Wait for process end or yield_deadline. Never consumes RemoteCommand events."""
        self.start_pump(self._registry)
        ctx_to_detach: ToolCancelContext | None = None
        try:
            with self._cv:
                while not self._done:
                    if self.cancel_context is not None and self.cancel_context.cancelled:
                        self._cancel_locked()
                    # If the launching ToolCancelContext cancelled this sync/pre-yield wait,
                    # wait for process termination rather than yielding a cancelled process.
                    cancelling_via_ctx = self.cancel_context is not None and self._cancelled
                    now = time.monotonic()
                    if yield_deadline is not None and not cancelling_via_ctx and now >= yield_deadline:
                        self._yielded = True
                        ctx_to_detach = self.cancel_context
                        self.cancel_context = None
                        return self._snapshot_locked(include_pending=True)
                    timeout = 0.1
                    if yield_deadline is not None and not cancelling_via_ctx:
                        timeout = max(0.0, min(timeout, yield_deadline - now))
                    self._cv.wait(timeout=timeout)
                return self._snapshot_locked(include_pending=True)
        finally:
            if ctx_to_detach is not None:
                ctx_to_detach.clear_process(self)

    def write(self, data: str, *, timeout: float = 15.0) -> None:
        with self._cv:
            if self._done or self.handle is None:
                raise ValueError("execution has already finished")
            handle = self.handle
        handle.write_stdin(data.encode("utf-8"), timeout=timeout)

    def close_stdin(self) -> None:
        with self._cv:
            if self._done or self.handle is None:
                raise ValueError("execution has already finished")
            handle = self.handle
        handle.close_stdin()

    def cancel(self) -> None:
        with self._cv:
            self._cancel_locked()

    # ToolCancelContext invokes cancel_execution() when cancelling a bound execution.
    cancel_execution = cancel

    def apply_stdin(self, *, chars: str, action: StdinAction, write_timeout: float = 15.0) -> None:
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
            self.write(chars, timeout=write_timeout)

    def mark_result_consumed(self) -> None:
        with self._cv:
            self._result_consumed = True
        if self._registry is not None:
            self._registry.discard(self.execution_id)

    def execution_metadata(self) -> dict[str, Any]:
        """Unified execution state metadata from CommandSession (running or terminal)."""
        with self._cv:
            if self._done:
                return self._compute_terminal_locked()
            meta = self.collector.log_metadata()
            return {
                "execution_id": self.execution_id,
                "running": True,
                "exit_code": None,
                "termination_reason": None,
                **meta,
            }

    def restore_snapshot(self) -> dict[str, Any]:
        """Read-only state snapshot for TUI session restore (never consumes unread output)."""
        with self._cv:
            if self._done:
                term = self._compute_terminal_locked()
                return {
                    "running": False,
                    "artifact": term,
                    "output": self.collector.tail_text(),
                }
            meta = self.collector.log_metadata()
            return {
                "running": True,
                "artifact": {
                    "execution_id": self.execution_id,
                    "running": True,
                    "exit_code": None,
                    "termination_reason": None,
                    **meta,
                },
                "output": self.collector.tail_text(),
            }

    def to_response(self, *, include_pending: bool = True) -> LocalExecuteResponse:
        with self._cv:
            return self._snapshot_locked(include_pending=include_pending)

    def _snapshot_locked(self, *, include_pending: bool) -> LocalExecuteResponse:
        if self._done:
            term = self._compute_terminal_locked()
            unread_only = self._yielded
            if unread_only:
                body = self.collector.take_pending() if include_pending else ""
            else:
                body = self.collector.tail_text()
            output = format_terminal_output(
                body,
                exit_code=term["exit_code"],
                termination_reason=term["termination_reason"],
                timeout=self.timeout,
                failure=self._failure,
                spawn_error=self._spawn_error,
                unread_only=unread_only,
                truncation_detail=self.collector.truncation_detail(),
            )
            return LocalExecuteResponse(
                output,
                term["exit_code"],
                term["truncated"],
                host_log_path=term["host_log_path"],
                agent_log_path=term["agent_log_path"],
                log_error=term["log_error"],
                termination_reason=term["termination_reason"],
                max_output_bytes=term["max_output_bytes"],
                execution_id=term["execution_id"],
                running=False,
            )
        pending = self.collector.take_pending() if include_pending else ""
        meta = self.collector.log_metadata()
        return LocalExecuteResponse(
            format_running_content(pending, self.execution_id),
            None,
            meta["truncated"],
            host_log_path=meta["host_log_path"],
            agent_log_path=meta["agent_log_path"],
            log_error=meta["log_error"],
            termination_reason=None,
            max_output_bytes=meta["max_output_bytes"],
            execution_id=self.execution_id,
            running=True,
        )


class ExecutionRegistry:
    """Per-SandboxPool map of live and bounded retained terminal executions."""

    def __init__(
        self,
        *,
        max_terminal: int = DEFAULT_MAX_TERMINAL_RECORDS,
        terminal_ttl_seconds: float = DEFAULT_TERMINAL_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, CommandSession] = {}
        self._max_terminal = max(0, int(max_terminal))
        self._terminal_ttl = max(0.0, float(terminal_ttl_seconds))
        self._clock = clock

    def now(self) -> float:
        return self._clock()

    def _cleanup_locked(self) -> None:
        """Lazily evict consumed, expired, or excess terminal records; never evict running ones."""
        now = self._clock()
        terminal: list[tuple[float, str]] = []
        drop: list[str] = []
        for execution_id, session in self._items.items():
            with session._cv:
                done = session._done
                consumed = session._result_consumed
                finished_at = session._finished_at
            if not done:
                continue
            if consumed:
                drop.append(execution_id)
                continue
            if finished_at is not None and (now - finished_at) >= self._terminal_ttl:
                drop.append(execution_id)
                continue
            terminal.append((finished_at if finished_at is not None else 0.0, execution_id))
        for execution_id in drop:
            self._items.pop(execution_id, None)
        if len(terminal) > self._max_terminal:
            terminal.sort()
            excess = len(terminal) - self._max_terminal
            for _finished_at, execution_id in terminal[:excess]:
                self._items.pop(execution_id, None)

    def register(self, session: CommandSession) -> CommandSession:
        with self._lock:
            self._cleanup_locked()
            self._items[session.execution_id] = session
        session.start_pump(self)
        return session

    def on_finished(self, _session: CommandSession) -> None:
        with self._lock:
            self._cleanup_locked()

    def get(self, execution_id: str) -> CommandSession | None:
        with self._lock:
            self._cleanup_locked()
            return self._items.get(execution_id)

    def list_for_thread(self, thread_id: str) -> list[CommandSession]:
        with self._lock:
            self._cleanup_locked()
            return [session for session in self._items.values() if session.owns(thread_id)]

    def require(self, execution_id: str, *, thread_id: str | None = None) -> CommandSession:
        owner = execution_thread_id() if thread_id is None else thread_id
        session = self.get(execution_id)
        if session is None or session.execution_id != execution_id:
            raise KeyError(f"Unknown or expired execution_id: {execution_id}")
        with session._cv:
            consumed = session._result_consumed
        if consumed:
            raise KeyError(f"Unknown or expired execution_id: {execution_id}")
        if not session.owns(owner):
            raise PermissionError("execution_id belongs to another session or agent")
        return session

    def discard(self, execution_id: str) -> None:
        with self._lock:
            self._items.pop(execution_id, None)

    def cleanup_owner(self, thread_id: str, *, join_timeout: float = 2.0) -> int:
        """Cancel running executions and drop all records owned by ``thread_id``."""
        with self._lock:
            owned = [
                (execution_id, session)
                for execution_id, session in self._items.items()
                if session.owns(thread_id)
            ]
            for execution_id, _session in owned:
                self._items.pop(execution_id, None)
        for _execution_id, session in owned:
            if not session.is_done:
                session.cancel()
        for _execution_id, session in owned:
            session.join_pump(timeout=join_timeout)
        return len(owned)

    def shutdown(self) -> None:
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
        for item in items:
            if not item.is_done:
                item.cancel()
        for item in items:
            item.join_pump(timeout=2.0)


def interact(
    session: CommandSession,
    *,
    chars: str = "",
    yield_time: float = 1.0,
    action: StdinAction = "write",
    write_timeout: float = 15.0,
) -> LocalExecuteResponse:
    session.apply_stdin(chars=chars, action=action, write_timeout=write_timeout)
    response = session.wait_until(time.monotonic() + max(0.0, yield_time))
    if not response.running:
        session.mark_result_consumed()
    return response
