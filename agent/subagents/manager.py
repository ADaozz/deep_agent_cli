"""Admission and lifetime tracking; the parent owns all shared resources."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
import time
from typing import Literal
from uuid import uuid4

from agent.runtime.control import RunController

TaskStatus = Literal["running", "completed", "failed", "cancelled"]
TaskMode = Literal["sync", "background"]


FINISHED_TASK_LIMIT = 50
STREAM_CLOSE_WAIT = 5.0


@dataclass(eq=False)
class TaskRecord:
    """One sync or background delegation. The runtime writes the terminal status."""

    run_id: str
    parent_run_id: str
    tool_call_id: str
    description: str
    depth: int
    mode: TaskMode
    controller: RunController
    status: TaskStatus = "running"
    result: str | None = None
    elapsed_seconds: float | None = None
    # Set at a session boundary: the result no longer has a conversation to reach.
    discarded: bool = False
    # wait_task claims the result so the host does not also deliver it.
    consumed: bool = False
    notified: bool = False
    done: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)


class SubAgentManager:
    def __init__(
        self,
        parent: RunController,
        *,
        max_concurrent: int,
        max_total: int,
    ) -> None:
        self.parent = parent
        # Per-parent simultaneous direct-child cap.
        self.max_concurrent = max_concurrent
        # Whole-tree simultaneous cap across every SubAgent (excluding Root).
        self.max_total = max_total
        self._lock = threading.RLock()
        self._active: dict[str, RunController] = {}
        self._closing: dict[str, RunController] = {}
        self._parents: dict[str, str] = {}
        self._owners: dict[str, RunController] = {}
        self._tasks: dict[str, TaskRecord] = {}
        self.log_store: object | None = None

    @property
    def active_count(self) -> int:
        """Running plus still-closing tasks (both hold concurrency slots)."""
        with self._lock:
            return self._occupied()

    def set_limits(self, *, max_concurrent: int, max_total: int) -> None:
        """Replace both caps under the same lock admit uses."""
        with self._lock:
            self.max_concurrent = max_concurrent
            self.max_total = max_total

    def capacity_snapshot(self, parent_id: str) -> dict:
        """One consistent reading of this parent's remaining capacity."""
        with self._lock:
            direct_children = self._occupied_children(parent_id)
            tree_occupied = self._occupied()
            return {
                "direct_children": direct_children,
                "max_concurrent": self.max_concurrent,
                "tree_occupied": tree_occupied,
                "max_total": self.max_total,
                "available": max(
                    0,
                    min(
                        self.max_concurrent - direct_children,
                        self.max_total - tree_occupied,
                    ),
                ),
            }

    def active_children(self, parent_id: str) -> int:
        with self._lock:
            return self._occupied_children(parent_id)

    def _occupied(self) -> int:
        return len(self._active) + len(self._closing)

    def _occupied_children(self, parent_id: str) -> int:
        return sum(1 for value in self._parents.values() if value == parent_id)

    def reap_closed(self) -> int:
        """Finish unregistering closing tasks whose model streams have closed.

        Returns how many closing slots were reclaimed.
        """
        with self._lock:
            items = [(run_id, self._closing[run_id], self._owners.get(run_id, self.parent))
                     for run_id in list(self._closing)]
        reclaimed = 0
        for run_id, child, owner in items:
            if not child.wait_model_streams_closed(0.0):
                continue
            owner.unregister_child(child)
            with self._lock:
                if self._closing.pop(run_id, None) is not None:
                    self._parents.pop(run_id, None)
                    self._owners.pop(run_id, None)
                    reclaimed += 1
                    self.parent._emit("subagents_changed", {"count": self._occupied()})
        return reclaimed

    def admit(self, parent_id: str, owner: RunController | None = None) -> tuple[str, RunController]:
        """Take one concurrency permit under ``parent_id``.

        ``owner`` receives ``register_child`` so cancel propagates down the real
        agent tree. Quotas stay global on this manager. Every admit needs exactly
        one release.
        """
        if not isinstance(parent_id, str) or not parent_id.strip():
            raise ValueError("Subagent parent_id must be a non-empty string")
        owner = owner or self.parent
        self.reap_closed()
        with self._lock:
            if self.parent.cancel_requested:
                raise RuntimeError("Parent run cancelled")
            if owner.cancel_requested:
                raise RuntimeError("Owning agent cancelled")
            if self._occupied() >= self.max_total:
                raise RuntimeError(f"Subagent total concurrency limit ({self.max_total}) reached")
            at_parent = self._occupied_children(parent_id)
            if at_parent >= self.max_concurrent:
                raise RuntimeError(
                    f"Subagent per-parent concurrency limit ({self.max_concurrent}) "
                    f"reached for parent {parent_id}"
                )
            run_id = uuid4().hex
            child = RunController()
            child.begin_run()
            child.on_model_streams_idle = self.reap_closed
            owner.register_child(child)
            self._active[run_id] = child
            self._parents[run_id] = parent_id
            self._owners[run_id] = owner
            self.parent._emit("subagents_changed", {"count": self._occupied()})
        return run_id, child

    def release(self, run_id: str) -> None:
        with self._lock:
            child = self._active.get(run_id)
            owner = self._owners.get(run_id, self.parent)
        if child is None:
            # Already closing or unknown — try to reap stale closing entries.
            self.reap_closed()
            return
        child.cancel()
        child.end_run()
        with self._lock:
            if self._active.pop(run_id, None) is None:
                return
            self._closing[run_id] = child
            self.parent._emit("subagents_changed", {"count": self._occupied()})
        # Slot stays occupied until streams close (or reap later).
        if child.wait_model_streams_closed(STREAM_CLOSE_WAIT):
            owner.unregister_child(child)
            with self._lock:
                if self._closing.pop(run_id, None) is not None:
                    self._parents.pop(run_id, None)
                    self._owners.pop(run_id, None)
                    self.parent._emit("subagents_changed", {"count": self._occupied()})
        else:
            self.reap_closed()

    def drop_task(self, run_id: str) -> None:
        """Remove a tracked background task record without touching controllers."""
        with self._lock:
            self._tasks.pop(run_id, None)

    @contextmanager
    def running(self, parent_id: str, owner: RunController | None = None):
        run_id, child = self.admit(parent_id, owner)
        try:
            yield run_id, child
        finally:
            self.release(run_id)

    # Background tasks -----------------------------------------------------

    def track(self, task: TaskRecord) -> None:
        """Register a background task before its thread starts. Does not set a terminal status."""
        with self._lock:
            self._tasks[task.run_id] = task
            self._prune_finished()

    def open_record(
        self,
        *,
        run_id: str,
        parent_run_id: str,
        description: str,
        tool_call_id: str,
        controller: RunController,
        mode: TaskMode,
        depth: int,
    ) -> TaskRecord:
        """Remember a direct child. A background launch already tracked this run."""
        with self._lock:
            existing = self._tasks.get(run_id)
            if existing is not None:
                return existing
            task = TaskRecord(
                run_id=run_id, parent_run_id=parent_run_id, tool_call_id=tool_call_id,
                description=description, depth=depth, mode=mode, controller=controller,
            )
            self._tasks[run_id] = task
            self._prune_finished()
            return task

    def _prune_finished(self) -> None:
        finished = [item for item in self._tasks.values() if item.status != "running"]
        for item in finished[:max(0, len(finished) - FINISHED_TASK_LIMIT)]:
            del self._tasks[item.run_id]

    def record_terminal(
        self, run_id: str, status: str, result: str, elapsed_seconds: float | None,
    ) -> bool:
        """Publish a structured outcome. Returns whether the parent was notified.

        Notification happens under the same lock that ``consume`` uses, so a
        wait cannot race a second delivery of the same result.
        """
        with self._lock:
            task = self._tasks.get(run_id)
            if task is None or task.done.is_set():
                return False
            task.status = status
            task.result = result
            if elapsed_seconds is not None:
                task.elapsed_seconds = elapsed_seconds
            notify = (
                task.mode == "background"
                and not task.consumed
                and not task.discarded
                and status != "cancelled"
            )
            task.notified = notify
            task.done.set()
            if notify:
                self.parent.notify({
                    "run_id": task.run_id,
                    "tool_call_id": task.tool_call_id,
                    "parent_run_id": task.parent_run_id,
                    "status": status,
                    "content": result,
                })
            return notify

    def consume(self, tasks: list[TaskRecord]) -> None:
        """Give these results to wait_task and drop a notification already queued."""
        with self._lock:
            for task in tasks:
                task.consumed = True
                if task.notified:
                    task.notified = False
                    self.parent.drop_notification(task.run_id)

    def task(self, run_id: str, parent: str) -> TaskRecord | None:
        with self._lock:
            found = self._tasks.get(run_id)
        return found if found is not None and found.parent_run_id == parent else None

    def tasks_for(self, parent: str) -> list[TaskRecord]:
        with self._lock:
            return [task for task in self._tasks.values() if task.parent_run_id == parent]

    def background_task(self, run_id: str, parent: str) -> TaskRecord | None:
        found = self.task(run_id, parent)
        return found if found is not None and found.mode == "background" else None

    def background_tasks(self, parent: str | None = None) -> list[TaskRecord]:
        with self._lock:
            return [
                task for task in self._tasks.values()
                if task.mode == "background" and (parent is None or task.parent_run_id == parent)
            ]

    @property
    def background_count(self) -> int:
        with self._lock:
            return sum(
                task.mode == "background" and task.status == "running"
                for task in self._tasks.values()
            )

    def cancel(self, run_id: str, parent: str) -> TaskRecord | None:
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
            tasks = [task for task in self._tasks.values() if task.mode == "background"]
            if discard:
                for task in tasks:
                    task.discarded = True
                for task in tasks:
                    self._tasks.pop(task.run_id, None)
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
