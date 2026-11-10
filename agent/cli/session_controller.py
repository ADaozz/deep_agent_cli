"""CLI session workflow: /new, /resume, and waiting out a session held elsewhere.

The controller owns the resume picker, the busy-session wait, and applying a
restored snapshot to the visible state. It drives the runner but never touches
the prompt_toolkit layout.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Awaitable
import asyncio
from typing import Any

from agent.cli.interactions import InteractionController
from agent.cli.lifecycle import TaskStop


if TYPE_CHECKING:
    from agent.runtime.runner import AgentRunner
    from agent.cli.lifecycle import AppLifecycle
    from prompt_toolkit.buffer import Buffer
    from agent.cli.state import CliState
    from agent.cli.view_state import DialogState
    from agent.cli.interaction_flow import InteractionFlow


# Shown while the runner waits for a session held by another window.
WAIT_STATUS = "Waiting for session {} · Esc cancels"


class SessionController:
    def __init__(
        self, *,
        _restore_queued_to_editor: Callable[..., Any],
        _run_blocking: Callable[..., Awaitable[Any]],
        buffer: Buffer,
        dialogs: DialogState,
        interaction_flow: Callable[[], InteractionFlow],
        lifecycle: Callable[[], AppLifecycle],
        reset_session_view: Callable[..., Any],
        runner: AgentRunner,
        set_status: Callable[..., None],
        state: CliState,
        invalidate: Callable[[], None],
    ) -> None:
        self._restore_queued_to_editor = _restore_queued_to_editor
        self._run_blocking = _run_blocking
        self.buffer = buffer
        self.dialogs = dialogs
        self.interaction_flow = interaction_flow
        self.lifecycle = lifecycle
        self.reset_session_view = reset_session_view
        self.runner = runner
        self.set_status = set_status
        self.state = state
        self.invalidate = invalidate
        self.open_picker_on_start = False
        self.startup_session_id: str | None = None
        self._wait_target: str | None = None
        self._wait_cancelled = False
        self._wait_task: asyncio.Task[None] | None = None


    @property
    def waiting(self) -> bool:
        return self._wait_target is not None

    @property
    def wait_target(self) -> str | None:
        return self._wait_target

    @property
    def wait_task(self) -> asyncio.Task[None] | None:
        return self._wait_task

    def wait_status(self) -> str:
        assert self._wait_target is not None
        return WAIT_STATUS.format(self._wait_target[:8])

    async def start(self) -> None:
        if self.startup_session_id is not None:
            await self._switch_with_wait(self.startup_session_id)
        elif self.open_picker_on_start:
            await self.resume()

    def new_session(self) -> None:
        if self.state.running:
            self.set_status("Cancel the active run before starting a new session")
            return
        if self.waiting:
            self.set_status(self.wait_status(), transient=False)
            return
        self._restore_queued_to_editor(self.buffer)
        try:
            info = self.runner.new_session()
        except RuntimeError as exc:
            self.state.add_system(str(exc), error=True)
            return
        self.reset_session_view()
        self.state.clear()
        self.state.context_pristine = True
        self.state.attachments.clear()
        self.state.add_system(f"Started session {info.id}")
        self.set_status("Ready", transient=False)

    async def resume(self, arg: str = "") -> None:
        if self.lifecycle().exiting:
            return
        if self.state.running:
            self.set_status("Cancel the active run before switching sessions")
            return
        if self.runner.session_store is None:
            self.state.add_system(
                "/resume is unavailable: persistent session storage is not configured", error=True,
            )
            return
        if self.waiting:
            return
        if arg.strip():
            self._restore_queued_to_editor(self.buffer)
            await self._switch_with_wait(arg.strip())
            return
        if self.lifecycle().exiting:
            return
        sessions = self._sessions_with_content(limit=50)
        if not sessions:
            self.state.add_system("No sessions with conversation content.", error=True)
            return
        options = [
            {
                "value": item.id,
                "label": f"{item.id[:8]} · {item.last_run_status.value} · {item.title[:40]}",
                "right_label": item.updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            }
            for item in sessions
        ]
        self.dialogs.current = InteractionController(
            kind="resume",
            title="Resume session",
            question="",
            fields=[{
                "id": "session",
                "type": "single_select",
                "label": "Session",
                "required": True,
                "options": options,
            }],
        )
        self.set_status("Select a session · Enter confirm · Esc cancel", transient=False)
        self.invalidate()

    def begin_switch(self, session_id: str) -> None:
        """Picker Enter: switch immediately, or enter the visible wait."""
        try:
            self._restore_queued_to_editor(self.buffer)
            result = self.runner.begin_session_switch(session_id)
        except (KeyError, RuntimeError) as exc:
            self.state.add_system(str(exc), error=True)
            return
        if result.busy:
            self._enter_wait(result.target_id)
            return
        assert result.snapshot is not None
        self.apply_snapshot(result.snapshot)

    def apply_snapshot(self, snapshot: Any) -> None:
        self.reset_session_view()
        self.state.load_transcript(snapshot.transcript)
        self.state.load_model_metrics(snapshot.model_metrics_history)
        self.state.add_system(
            f"Resumed session {snapshot.info.id} (last run: {snapshot.info.last_run_status.value})"
        )
        for notice in snapshot.notices:
            self.state.add_system(notice)
        if not self.interaction_flow()._reopen_pending_interaction(notify_missing=False):
            self.set_status("Ready", transient=False)
        self.state.usage = dict(self.runner.latest_usage())
        self.invalidate()

    def cancel_wait(self) -> None:
        self._wait_cancelled = True
        self.set_status("Cancelling wait…", transient=False)

    def notify_exit(self) -> None:
        self._wait_cancelled = True

    async def _switch_with_wait(self, session_id: str) -> None:
        try:
            result = await self._run_blocking(self.runner.begin_session_switch, session_id)
        except (KeyError, RuntimeError) as exc:
            if not self.lifecycle().exiting:
                self.state.add_system(str(exc), error=True)
            return
        if self.lifecycle().exiting:
            return
        if result.busy:
            self._enter_wait(result.target_id)
            return
        assert result.snapshot is not None
        self.apply_snapshot(result.snapshot)

    def _enter_wait(self, target_id: str) -> None:
        """Detach into a visible wait for a session busy in another window."""
        if self.lifecycle().exiting:
            return
        self._wait_target = target_id
        self._wait_cancelled = False
        self.dialogs.current = None
        self.dialogs.deferred = None
        self.state.add_system(
            f"Session {target_id[:8]} is open in another window; waiting for it to be released."
        )
        self.set_status(self.wait_status(), transient=False)
        self._wait_task = self.lifecycle().spawn(
            self._wait_for_session(target_id), stop=TaskStop.OBSERVE, name="session-wait",
        )
        self.invalidate()

    async def _wait_for_session(self, target_id: str) -> None:
        try:
            snapshot = await self._run_blocking(
                self.runner.complete_session_switch, target_id,
                cancelled=lambda: self._wait_cancelled,
            )
        except (KeyError, RuntimeError) as exc:
            self._wait_target = None
            if not self.lifecycle().exiting:
                self.state.add_system(str(exc), error=True)
                self.set_status("Session switch failed")
            return
        self._wait_target = None
        if self.lifecycle().exiting:
            # Exit cancelled the wait. Esc is the only path that reopens the picker.
            # A switch that already took the lease stays with the runner.
            return
        if snapshot is None:
            # Cancelled: the runner stays detached, so the picker (or /new) must
            # choose the next session explicitly.
            self.set_status("Waiting cancelled · pick a session", transient=False)
            await self.resume()
            return
        self.apply_snapshot(snapshot)

    def _sessions_with_content(self, *, limit: int = 50) -> list[Any]:
        if self.runner.session_store is None:
            return []
        sessions = []
        for info in self.runner.list_sessions(limit=limit):
            if not self.runner.thread_has_content(info.id):
                continue
            sessions.append(info)
        return sessions
