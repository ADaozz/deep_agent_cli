"""Exit request versus resource shutdown for one CLI process.

Key handlers only ask to leave. This object signals the owners of live work,
then waits for CLI tasks until one deadline. Runner.close() still releases the
session lease, model streams, MCP clients, and sandboxes; nothing here calls it.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from agent.cli.app import CliApplication


class ExitPhase(Enum):
    ACTIVE = "active"
    EXIT_REQUESTED = "exit_requested"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"


class TaskStop(Enum):
    """How shutdown treats a task the CLI created.

    CANCEL tasks are UI helpers. OBSERVE tasks are a live run, an MCP
    operation, or a session wait: their owners are signalled first, then
    shutdown waits for them to finish. Neither kind is assumed to survive
    after the event loop itself stops.
    """

    CANCEL = "cancel"
    OBSERVE = "observe"


@dataclass(frozen=True)
class TaskCleanupFailure:
    """One task that failed or timed out while shutting down.

    ``stage`` is ``done`` (already finished with an error), ``wait`` (raised
    while being awaited), or ``timeout``. Cancellation of a UI helper is not
    a failure.
    """

    name: str
    stage: str
    error: BaseException | None = None


async def _refuse_ui_task() -> None:
    """Completed stand-in for a UI task refused after exit was requested."""
    return None


class AppLifecycle:
    def __init__(self, app: CliApplication) -> None:
        self._app = app
        self.phase = ExitPhase.ACTIVE
        self._tracked: list[tuple[asyncio.Task[object], TaskStop]] = []
        self._owners_signalled = False
        self.unfinished: tuple[str, ...] = ()
        self.failures: tuple[TaskCleanupFailure, ...] = ()

    @property
    def exiting(self) -> bool:
        return self.phase is not ExitPhase.ACTIVE

    def spawn(self, coro: object, *, stop: TaskStop, name: str) -> asyncio.Task[object]:
        if stop is TaskStop.CANCEL and self.phase is not ExitPhase.ACTIVE:
            close = getattr(coro, "close", None)
            if close is not None:
                close()
            return asyncio.create_task(_refuse_ui_task(), name=name)
        task = asyncio.create_task(coro, name=name)  # type: ignore[arg-type]
        self._tracked.append((task, stop))
        task.add_done_callback(self._forget)
        return task

    def _forget(self, task: asyncio.Task[object]) -> None:
        self._tracked = [item for item in self._tracked if item[0] is not task]

    def request_exit(self) -> None:
        """Idempotent. Leave the full screen and signal live work to stop.

        Does not close the executor or the runner. A session lease survives
        until the run and its model stream have actually stopped.
        """
        if self.phase is not ExitPhase.ACTIVE:
            return
        self.phase = ExitPhase.EXIT_REQUESTED
        app = self._app
        if app._status_notice_handle is not None:
            app._status_notice_handle.cancel()
            app._status_notice_handle = None
        app._stop_selection_scroll()
        # Stop mouse reports before prompt_toolkit leaves the alternate screen.
        # Otherwise motion generated during shutdown can reach the shell.
        app.application.output.disable_mouse_support()
        app.application.output.flush()
        self._signal_owners()
        app.application.exit()

    def _signal_owners(self) -> None:
        """Tell run, MCP, and session-wait owners to stop. Once per process."""
        if self._owners_signalled:
            return
        app = self._app
        app.sessions.notify_exit()
        manager = getattr(app.runner, "mcp", None)
        if manager is not None:
            manager.cancel_pending_connections()
        if app.state.running:
            app.runner.request_cancel()
        self._owners_signalled = True

    async def shutdown(self, *, task_timeout: float = 2.0) -> None:
        """Finish UI resources after the prompt loop. Safe to call once.

        The executor stays open until tracked tasks have finished or the
        shared deadline has passed. Closing it does not mean an in-flight
        thread has stopped, and it does not release the session lease.
        """
        if self.phase is ExitPhase.STOPPED:
            return
        app = self._app
        try:
            if self.phase is ExitPhase.ACTIVE:
                self._signal_owners()
                self.phase = ExitPhase.EXIT_REQUESTED
            self.phase = ExitPhase.SHUTTING_DOWN
            unfinished, failures = await self._drain_tasks(task_timeout)
            self.unfinished = tuple(unfinished)
            self.failures = tuple(failures)
        finally:
            try:
                app._io_executor.shutdown(wait=False, cancel_futures=True)
            finally:
                app._git_task = None
                self.phase = ExitPhase.STOPPED

    async def _drain_tasks(
        self, task_timeout: float,
    ) -> tuple[list[str], list[TaskCleanupFailure]]:
        snapshot = list(self._tracked)
        for task, stop in snapshot:
            if stop is TaskStop.CANCEL and not task.done():
                task.cancel()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + task_timeout
        unfinished: list[str] = []
        failures: list[TaskCleanupFailure] = []
        for task, stop in snapshot:
            kind = "cancel" if stop is TaskStop.CANCEL else "observe"
            if task.done():
                self._record_done(task, failures)
                continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                unfinished.append(task.get_name())
                failures.append(TaskCleanupFailure(task.get_name(), "timeout"))
                continue
            try:
                await asyncio.wait_for(asyncio.shield(task), remaining)
            except asyncio.TimeoutError:
                unfinished.append(task.get_name())
                failures.append(TaskCleanupFailure(task.get_name(), "timeout"))
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                if not task.done():
                    unfinished.append(task.get_name())
                    failures.append(TaskCleanupFailure(task.get_name(), kind))
            except Exception as exc:
                failures.append(TaskCleanupFailure(task.get_name(), "wait", exc))
                if task.done() and not task.cancelled():
                    task.exception()
                if not task.done():
                    unfinished.append(task.get_name())
            else:
                self._record_done(task, failures)
        return unfinished, failures

    @staticmethod
    def _record_done(task: asyncio.Task[object], failures: list[TaskCleanupFailure]) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            failures.append(TaskCleanupFailure(task.get_name(), "done", error))
