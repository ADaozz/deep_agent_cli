"""Exit request versus resource shutdown for one CLI process.

Key handlers only ask to leave. This object performs the UI-side cleanup and
keeps a registry of tasks the CLI started. Runner.close() still releases the
session lease, model streams, MCP clients, and sandboxes; nothing here calls it.
"""
from __future__ import annotations

import asyncio
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

    CANCEL tasks are UI helpers and are cancelled. OBSERVE tasks are a live
    run, an MCP operation, or a session wait: their existing owners stop them,
    and shutdown does not cancel them or wait for the run to finish.
    """

    CANCEL = "cancel"
    OBSERVE = "observe"


class AppLifecycle:
    def __init__(self, app: CliApplication) -> None:
        self._app = app
        self.phase = ExitPhase.ACTIVE
        self._tracked: list[tuple[asyncio.Task[object], TaskStop]] = []
        self.unfinished: tuple[str, ...] = ()

    @property
    def exiting(self) -> bool:
        return self.phase is not ExitPhase.ACTIVE

    def spawn(self, coro: object, *, stop: TaskStop, name: str) -> asyncio.Task[object]:
        task = asyncio.create_task(coro, name=name)  # type: ignore[arg-type]
        self._tracked.append((task, stop))
        task.add_done_callback(self._forget)
        return task

    def _forget(self, task: asyncio.Task[object]) -> None:
        self._tracked = [item for item in self._tracked if item[0] is not task]

    def request_exit(self) -> None:
        """Idempotent. Leaves the full screen and asks the active run to cancel.

        Does not close the runner, so a session lease survives until the run
        and its model stream have actually stopped.
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
        app.sessions.notify_exit()
        manager = getattr(app.runner, "mcp", None)
        if manager is not None:
            manager.cancel_pending_connections()
        if app.state.running:
            app.runner.request_cancel()
        app._io_executor.shutdown(wait=False, cancel_futures=True)
        app.application.exit()

    async def shutdown(self, *, task_timeout: float = 2.0) -> None:
        """Finish UI resources after the prompt loop. Safe to call once."""
        if self.phase is ExitPhase.STOPPED:
            return
        if self.phase is ExitPhase.ACTIVE:
            self.phase = ExitPhase.EXIT_REQUESTED
        self.phase = ExitPhase.SHUTTING_DOWN
        app = self._app
        manager = getattr(app.runner, "mcp", None)
        if manager is not None:
            manager.cancel_pending_connections()
        app.sessions.notify_exit()
        await app._stop_git_watch()
        unfinished = await self._stop_cancel_tasks(task_timeout)
        for task, stop in list(self._tracked):
            if stop is TaskStop.OBSERVE and not task.done():
                unfinished.append(task.get_name())
        self.unfinished = tuple(unfinished)
        app._io_executor.shutdown(wait=False, cancel_futures=True)
        self.phase = ExitPhase.STOPPED

    async def _stop_cancel_tasks(self, task_timeout: float) -> list[str]:
        pending = [
            task for task, stop in list(self._tracked)
            if stop is TaskStop.CANCEL and not task.done()
        ]
        for task in pending:
            task.cancel()
        if not pending:
            return []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + task_timeout
        unfinished: list[str] = []
        for task in pending:
            remaining = deadline - loop.time()
            if remaining <= 0:
                unfinished.append(task.get_name())
                continue
            try:
                await asyncio.wait_for(asyncio.shield(task), remaining)
            except asyncio.TimeoutError:
                unfinished.append(task.get_name())
            except asyncio.CancelledError:
                if not task.done():
                    unfinished.append(task.get_name())
        return unfinished
