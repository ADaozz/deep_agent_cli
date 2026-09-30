"""CLI session workflow: /new, /resume, and waiting out a session held elsewhere.

The controller owns the resume picker, the busy-session wait, and applying a
restored snapshot to the visible state. It drives the runner but never touches
the prompt_toolkit layout.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from agent.cli.interactions import InteractionController

if TYPE_CHECKING:
    from agent.cli.app import CliApplication


# Shown while the runner waits for a session held by another window.
WAIT_STATUS = "Waiting for session {} · Esc cancels"


class SessionController:
    def __init__(self, app: CliApplication) -> None:
        self._app = app
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

    def schedule_start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self.startup_session_id is not None:
            loop.create_task(self._switch_with_wait(self.startup_session_id))
        elif self.open_picker_on_start:
            loop.create_task(self.resume())

    async def start(self) -> None:
        if self.startup_session_id is not None:
            await self._switch_with_wait(self.startup_session_id)
        elif self.open_picker_on_start:
            await self.resume()

    def new_session(self) -> None:
        app = self._app
        if app.state.running:
            app.set_status("Cancel the active run before starting a new session")
            return
        if self.waiting:
            app.set_status(self.wait_status())
            return
        app._restore_queued_to_editor(app.buffer)
        try:
            info = app.runner.new_session()
        except RuntimeError as exc:
            app.state.add_system(str(exc), error=True)
            return
        app.state.clear()
        app.state.attachments.clear()
        app.interaction = None
        app._deferred_config_interaction = None
        app._reviewing = False
        app._transcript_anchor = None
        app._renderer.clear()
        app.state.add_system(f"Started session {info.id}")
        app.set_status("Ready")

    async def resume(self, arg: str = "") -> None:
        app = self._app
        if app.state.running:
            app.set_status("Cancel the active run before switching sessions")
            return
        if app.runner.session_store is None:
            app.state.add_system(
                "/resume is unavailable: persistent session storage is not configured", error=True,
            )
            return
        if self.waiting:
            return
        if arg.strip():
            app._restore_queued_to_editor(app.buffer)
            await self._switch_with_wait(arg.strip())
            return
        sessions = self._sessions_with_content(limit=50)
        if not sessions:
            app.state.add_system("No sessions with conversation content.", error=True)
            return
        options = [
            {
                "value": item.id,
                "label": f"{item.id[:8]} · {item.last_run_status.value} · {item.title[:40]}",
                "right_label": item.updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            }
            for item in sessions
        ]
        app.interaction = InteractionController(
            kind="resume",
            title="Resume session",
            question="Select a session to restore",
            fields=[{
                "id": "session",
                "type": "single_select",
                "label": "Session",
                "required": True,
                "options": options,
            }],
        )
        app.set_status("Select a session · Enter confirm · Esc cancel")
        app.application.invalidate()

    def begin_switch(self, session_id: str) -> None:
        """Picker Enter: switch immediately, or enter the visible wait."""
        app = self._app
        try:
            app._restore_queued_to_editor(app.buffer)
            result = app.runner.begin_session_switch(session_id)
        except (KeyError, RuntimeError) as exc:
            app.state.add_system(str(exc), error=True)
            return
        if result.busy:
            self._enter_wait(result.target_id)
            return
        assert result.snapshot is not None
        self.apply_snapshot(result.snapshot)

    def apply_snapshot(self, snapshot: Any) -> None:
        app = self._app
        app.state.load_transcript(snapshot.transcript)
        app.state.todos = list(snapshot.todos)
        app.interaction = None
        app._deferred_config_interaction = None
        app._reviewing = False
        app.state.add_system(
            f"Resumed session {snapshot.info.id} (last run: {snapshot.info.last_run_status.value})"
        )
        for notice in snapshot.notices:
            app.state.add_system(notice)
        if not app._reopen_pending_interaction(notify_missing=False):
            app.set_status("Ready")
        app._transcript_anchor = None
        app._renderer.clear()
        app.state.usage = dict(app.runner.latest_usage())
        app.application.invalidate()

    def cancel_wait(self) -> None:
        self._wait_cancelled = True
        self._app.set_status("Cancelling wait…")

    def notify_exit(self) -> None:
        self._wait_cancelled = True

    async def _switch_with_wait(self, session_id: str) -> None:
        app = self._app
        try:
            result = await app._run_blocking(app.runner.begin_session_switch, session_id)
        except (KeyError, RuntimeError) as exc:
            app.state.add_system(str(exc), error=True)
            return
        if result.busy:
            self._enter_wait(result.target_id)
            return
        assert result.snapshot is not None
        self.apply_snapshot(result.snapshot)

    def _enter_wait(self, target_id: str) -> None:
        """Detach into a visible wait for a session busy in another window."""
        app = self._app
        self._wait_target = target_id
        self._wait_cancelled = False
        app.interaction = None
        app._deferred_config_interaction = None
        app.state.add_system(
            f"Session {target_id[:8]} is open in another window; waiting for it to be released."
        )
        app.set_status(self.wait_status())
        self._wait_task = asyncio.create_task(self._wait_for_session(target_id))
        app.application.invalidate()

    async def _wait_for_session(self, target_id: str) -> None:
        app = self._app
        try:
            snapshot = await app._run_blocking(
                app.runner.complete_session_switch, target_id,
                cancelled=lambda: self._wait_cancelled,
            )
        except (KeyError, RuntimeError) as exc:
            self._wait_target = None
            app.state.add_system(str(exc), error=True)
            app.set_status("Session switch failed")
            return
        self._wait_target = None
        if snapshot is None:
            # Cancelled: the runner stays detached, so the picker (or /new) must
            # choose the next session explicitly.
            app.set_status("Waiting cancelled · pick a session")
            await self.resume()
            return
        self.apply_snapshot(snapshot)

    def _sessions_with_content(self, *, limit: int = 50) -> list[Any]:
        app = self._app
        if app.runner.session_store is None:
            return []
        sessions = []
        for info in app.runner.list_sessions(limit=limit):
            if not app.runner.thread_has_content(info.id):
                continue
            sessions.append(info)
        return sessions
