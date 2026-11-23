"""Admission and lifetime tracking; the parent owns all shared resources."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
import time
from uuid import uuid4

from agent.runtime.control import RunController


FINISHED_TASK_LIMIT = 50


@dataclass(eq=False)
class BackgroundTask:
    """One delegation that outlives the tool call which started it."""

    run_id: str
    tool_call_id: str
    parent: str
    description: str
    controller: RunController
    status: str = "running"  # running | completed | failed | cancelled
    result: str = ""
    # Set at a session boundary: the result no longer has a conversation to reach.
    discarded: bool = False
    thread: threading.Thread | None = field(default=None, repr=False)


class SubAgentManager:
    def __init__(
        self,
        parent: RunController,
        max_concurrent: int = 4,
        max_total: int = 10,
    ) -> None:
        self.parent = parent
        # Per-parent simultaneous direct-child cap.
        self.max_concurrent = max_concurrent
        # Whole-tree simultaneous cap across every SubAgent (excluding Root).
        self.max_total = max_total
        self._lock = threading.RLock()
        self._active: dict[str, RunController] = {}
        self._parents: dict[str, str] = {}
        self._owners: dict[str, RunController] = {}
        self._retired: list[RunController] = []
        self._tasks: dict[str, BackgroundTask] = {}

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def active_children(self, parent_id: str) -> int:
        with self._lock:
            return sum(1 for value in self._parents.values() if value == parent_id)

    def admit(self, parent_id: str, owner: RunController | None = None) -> tuple[str, RunController]:
        """Take one concurrency permit under ``parent_id``.

        ``owner`` receives ``register_child`` so cancel propagates down the real
        agent tree. Quotas stay global on this manager. Every admit needs exactly
        one release.
        """
        if not isinstance(parent_id, str) or not parent_id.strip():
            raise ValueError("Subagent parent_id must be a non-empty string")
        owner = owner or self.parent
        with self._lock:
            if self.parent.cancel_requested:
                raise RuntimeError("Parent run cancelled")
            if owner.cancel_requested:
                raise RuntimeError("Owning agent cancelled")
            if len(self._active) >= self.max_total:
                raise RuntimeError(f"Subagent total concurrency limit ({self.max_total}) reached")
            at_parent = sum(1 for value in self._parents.values() if value == parent_id)
            if at_parent >= self.max_concurrent:
                raise RuntimeError(
                    f"Subagent per-parent concurrency limit ({self.max_concurrent}) "
                    f"reached for parent {parent_id}"
                )
            run_id = uuid4().hex
            child = RunController()
            child.begin_run()
            owner.register_child(child)
            self._active[run_id] = child
            self._parents[run_id] = parent_id
            self._owners[run_id] = owner
            self.parent._emit("subagents_changed", {"count": len(self._active)})
        return run_id, child

    def release(self, run_id: str) -> None:
        with self._lock:
            child = self._active.get(run_id)
            owner = self._owners.get(run_id, self.parent)
        if child is None:
            return
        child.cancel()
        child.end_run()
        with self._lock:
            self._active.pop(run_id, None)
            self._parents.pop(run_id, None)
            self._owners.pop(run_id, None)
            self._retired.append(child)
            self.parent._emit("subagents_changed", {"count": len(self._active)})
        # Keep the child attached until its HTTP requests confirm closure.
        if child.wait_model_streams_closed(5.0):
            owner.unregister_child(child)
            with self._lock:
                if child in self._retired:
                    self._retired.remove(child)

    @contextmanager
    def running(self, parent_id: str, owner: RunController | None = None):
        run_id, child = self.admit(parent_id, owner)
        try:
            yield run_id, child
        finally:
            self.release(run_id)

    # Background tasks -----------------------------------------------------

    def track(self, task: BackgroundTask) -> None:
        with self._lock:
            self._tasks[task.run_id] = task
            finished = [item for item in self._tasks.values() if item.status != "running"]
            for item in finished[:max(0, len(finished) - FINISHED_TASK_LIMIT)]:
                del self._tasks[item.run_id]

    def finish(self, task: BackgroundTask, status: str, result: str) -> None:
        with self._lock:
            task.status, task.result = status, result

    def background_task(self, run_id: str, parent: str) -> BackgroundTask | None:
        with self._lock:
            task = self._tasks.get(run_id)
        return task if task is not None and task.parent == parent else None

    def background_tasks(self, parent: str | None = None) -> list[BackgroundTask]:
        with self._lock:
            return [task for task in self._tasks.values() if parent is None or task.parent == parent]

    @property
    def background_count(self) -> int:
        with self._lock:
            return sum(task.status == "running" for task in self._tasks.values())

    def cancel(self, run_id: str, parent: str) -> BackgroundTask | None:
        """Request one background task to stop. Its worker reports the outcome."""
        task = self.background_task(run_id, parent)
        if task is not None and task.status == "running":
            task.controller.cancel()
        return task

    def cancel_all(self, *, wait: float | None = None, discard: bool = False) -> bool:
        """Stop every background task. True when all workers ended within ``wait``.

        ``discard`` drops results that would otherwise be reported to a
        conversation the runner is leaving.
        """
        with self._lock:
            tasks = list(self._tasks.values())
            if discard:
                for task in tasks:
                    task.discarded = True
                self._tasks.clear()
        running = [task for task in tasks if task.status == "running"]
        for task in running:
            task.controller.cancel()
        if wait is None:
            return not running
        deadline = time.monotonic() + wait
        for task in running:
            if task.thread is not None and task.thread is not threading.current_thread():
                task.thread.join(max(0.0, deadline - time.monotonic()))
        return not any(task.thread is not None and task.thread.is_alive() for task in running)
