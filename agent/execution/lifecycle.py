"""Live yielded executions: ownership, background output pump, poll/write/cancel."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
from pathlib import Path
from typing import Any, Literal

from agent.execution.ownership import execution_agent_scope, execution_thread_id
from agent.execution.pool import RemoteCommand, SandboxLostError, SandboxPool
from agent.execution.process import KILL_CONFIRM_SECONDS, LocalExecuteResponse, _OutputCollector


StdinAction = Literal["write", "close", "cancel"]


@dataclass
class LiveExecution:
    execution_id: str
    handle: RemoteCommand
    pool: SandboxPool
    collector: _OutputCollector
    thread_id: str
    agent_scope: str
    timeout: int | None
    deadline: float | None
    workspace: Path
    max_output_bytes: int
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _done: bool = False
    _exit_code: int = 0
    _cancelled: bool = False
    _timed_out: bool = False
    _failure: str | None = None
    _spawn_error: str | None = None
    _kill_sent_at: float | None = None
    _unread: list[str] = field(default_factory=list)
    _pump: threading.Thread | None = field(default=None, repr=False)

    def start_pump(self) -> None:
        if self._pump is not None:
            return
        self._pump = threading.Thread(
            target=self._pump_loop, name=f"exec-pump-{self.execution_id}", daemon=True,
        )
        self._pump.start()

    def _pump_loop(self) -> None:
        while True:
            with self._lock:
                if self._done:
                    return
                now = time.monotonic()
                if self._kill_sent_at is None:
                    if self.deadline is not None and now >= self.deadline:
                        self._timed_out = True
                        self.handle.kill()
                        self._kill_sent_at = now
                elif now - self._kill_sent_at >= KILL_CONFIRM_SECONDS:
                    self.pool.discard(self.handle.sandbox, "sandbox worker did not confirm a command kill")
                    self._failure = "Sandbox worker did not confirm a command kill"
                    self._exit_code = 125
                    self._finish_locked()
                    return
            event = self.handle.next_event(0.1)
            if event is None:
                continue
            kind, payload = event
            with self._lock:
                if kind == "out":
                    self.collector.append(payload)
                    chunk = payload.decode("utf-8", errors="replace")
                    if chunk:
                        self._unread.append(chunk)
                elif kind == "exit":
                    self._exit_code = int(payload)
                    self.collector.flush()
                    self._finish_locked()
                    return
                elif kind == "spawn_error":
                    self._spawn_error = str(payload)
                    self._finish_locked()
                    return
                else:
                    self._failure = (
                        f"Sandbox worker stopped while this command was running ({payload}). "
                        "The command was not retried; the next execute starts a fresh sandbox."
                    )
                    self._exit_code = 125
                    self.pool.discard(self.handle.sandbox, str(payload))
                    self._finish_locked()
                    return

    def _finish_locked(self) -> None:
        if self._done:
            return
        self._done = True
        try:
            self.handle.release()
        except Exception:  # noqa: BLE001
            pass
        self.collector.close()

    def owns(self, *, thread_id: str, agent_scope: str) -> bool:
        return self.thread_id == thread_id and self.agent_scope == agent_scope

    def take_unread(self) -> str:
        with self._lock:
            text = "".join(self._unread)
            self._unread.clear()
            return text

    def snapshot_output(self) -> str:
        with self._lock:
            if self.collector.tail_bytes:
                return bytes(self.collector.tail_bytes).decode("utf-8", errors="replace")
            return ""

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

    def wait_for_output(self, yield_time: float) -> LocalExecuteResponse:
        deadline = time.monotonic() + max(0.0, yield_time)
        while time.monotonic() < deadline:
            with self._lock:
                if self._done:
                    break
            time.sleep(0.05)
        return self.to_response(include_unread=True)

    def to_response(self, *, include_unread: bool = True) -> LocalExecuteResponse:
        with self._lock:
            done = self._done
            cancelled = self._cancelled
            timed_out = self._timed_out
            failure = self._failure
            spawn_error = self._spawn_error
            exit_code = self._exit_code
            truncated = self.collector.truncated
        unread = self.take_unread() if include_unread else ""
        if spawn_error is not None and not (cancelled or timed_out):
            return LocalExecuteResponse(
                f"Error executing command ({spawn_error})", 1, False,
                termination_reason="spawn_error",
                execution_id=self.execution_id,
                running=False,
            )
        if done:
            response = self.collector.response(
                exit_code=exit_code, timeout=self.timeout,
                cancelled=cancelled and not timed_out, timed_out=timed_out, failure=failure,
            )
            response.execution_id = self.execution_id
            response.running = False
            if unread and unread not in response.output:
                # Prefer unread delta when the collector already closed with full tail.
                pass
            return response
        output = unread if unread else "<no new output>"
        return LocalExecuteResponse(
            output, 0, truncated,
            termination_reason=None,
            execution_id=self.execution_id,
            running=True,
            max_output_bytes=self.max_output_bytes,
        )


class ExecutionRegistry:
    """Session-scoped map of yielded executions for write_stdin / poll."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, LiveExecution] = {}

    def register(self, live: LiveExecution) -> LiveExecution:
        with self._lock:
            self._items[live.execution_id] = live
        live.start_pump()
        return live

    def get(self, execution_id: str) -> LiveExecution | None:
        with self._lock:
            return self._items.get(execution_id)

    def require(self, execution_id: str, *, thread_id: str | None = None, agent_scope: str | None = None) -> LiveExecution:
        thread = execution_thread_id() if thread_id is None else thread_id
        scope = execution_agent_scope() if agent_scope is None else agent_scope
        live = self.get(execution_id)
        if live is None:
            raise KeyError(f"Unknown or expired execution_id: {execution_id}")
        # Reject ids from a previous sandbox generation even if somehow still mapped.
        if live.handle.execution_id != execution_id:
            raise KeyError(f"Unknown or expired execution_id: {execution_id}")
        if live.handle.sandbox.lost_reason is not None and live.handle.generation != live.handle.sandbox.generation - 1:
            # After loss, generation bumps; any stale lookup should fail closed.
            if not live._done:
                raise KeyError(f"Unknown or expired execution_id: {execution_id}")
        if not live.owns(thread_id=thread, agent_scope=scope):
            raise PermissionError("execution_id belongs to another session or agent")
        return live

    def discard(self, execution_id: str) -> None:
        with self._lock:
            self._items.pop(execution_id, None)

    def abandon_thread(self, thread_id: str) -> None:
        """Cancel and drop live executions owned by a destroyed session."""
        with self._lock:
            victims = [item for item in self._items.values() if item.thread_id == thread_id]
        for item in victims:
            item.cancel()
            self.discard(item.execution_id)

    def shutdown(self) -> None:
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
        for item in items:
            item.cancel()


_REGISTRY = ExecutionRegistry()


def execution_registry() -> ExecutionRegistry:
    return _REGISTRY


def interact(
    live: LiveExecution,
    *,
    chars: str = "",
    yield_time: float = 1.0,
    action: StdinAction = "write",
) -> LocalExecuteResponse:
    if action == "cancel":
        if chars:
            raise ValueError("cancel cannot include chars")
        live.cancel()
        return live.wait_for_output(yield_time)
    if action == "close":
        if chars:
            raise ValueError("close cannot include chars")
        live.close_stdin()
        return live.wait_for_output(yield_time)
    if action != "write":
        raise ValueError(f"unsupported write_stdin action: {action}")
    if chars:
        live.write(chars)
    return live.wait_for_output(yield_time)
