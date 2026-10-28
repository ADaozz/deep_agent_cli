"""Admission and lifetime tracking; the parent owns all shared resources."""
from __future__ import annotations

from contextlib import contextmanager
import threading
from uuid import uuid4

from agent.control import RunController


class SubAgentManager:
    def __init__(self, parent: RunController, max_concurrent: int = 2) -> None:
        self.parent = parent
        self.max_concurrent = max_concurrent
        self._lock = threading.RLock()
        self._active: dict[str, RunController] = {}
        self._retired: list[RunController] = []

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    @contextmanager
    def running(self):
        with self._lock:
            if self.parent.cancel_requested:
                raise RuntimeError("Parent run cancelled")
            if len(self._active) >= self.max_concurrent:
                raise RuntimeError(f"Subagent concurrency limit ({self.max_concurrent}) reached")
            run_id = uuid4().hex
            child = RunController()
            child.begin_run()
            self.parent.register_child(child)
            self._active[run_id] = child
            self.parent._emit("subagents_changed", {"count": len(self._active)})
        try:
            yield run_id, child
        finally:
            child.cancel()
            child.end_run()
            with self._lock:
                self._active.pop(run_id)
                self._retired.append(child)
                self.parent._emit("subagents_changed", {"count": len(self._active)})
            # Keep the child attached until its HTTP requests confirm closure.
            if child.wait_model_streams_closed(5.0):
                self.parent.unregister_child(child)
                with self._lock:
                    self._retired.remove(child)
