"""Apply runner events and results to the CLI interaction state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable
from typing import Any

from agent.cli.completion import model_display_name
from agent.runtime.contracts import RunEvent, RunResult


if TYPE_CHECKING:
    from agent.runtime.runner import AgentRunner
    from prompt_toolkit.buffer import Buffer
    from agent.cli.state import CliState
    from agent.cli.view_state import DialogState
    from agent.cli.working_messages import WorkingMessageRotation


class RunEventsController:
    def __init__(
        self, *,
        _allow_confirm_open: Callable[..., Any],
        _announce_mcp_switch: Callable[..., Any],
        _note_interrupt_behind_allow_confirm: Callable[..., Any],
        _reopen_pending_interaction: Callable[..., Any],
        _show_allow_confirm_status: Callable[..., Any],
        _start_queued_run: Callable[..., Any],
        _working_messages: Callable[[], WorkingMessageRotation],
        buffer: Buffer,
        dialogs: DialogState,
        runner: AgentRunner,
        set_status: Callable[..., None],
        state: CliState,
        invalidate: Callable[[], None],
    ) -> None:
        self._allow_confirm_open = _allow_confirm_open
        self._announce_mcp_switch = _announce_mcp_switch
        self._note_interrupt_behind_allow_confirm = _note_interrupt_behind_allow_confirm
        self._reopen_pending_interaction = _reopen_pending_interaction
        self._show_allow_confirm_status = _show_allow_confirm_status
        self._start_queued_run = _start_queued_run
        self._working_messages = _working_messages
        self.buffer = buffer
        self.dialogs = dialogs
        self.runner = runner
        self.set_status = set_status
        self.state = state
        self.invalidate = invalidate


    def _apply_event(self, event: RunEvent, *, announce: bool = True) -> None:
        previous_status = self.state.status
        self.state.apply(event)
        if self.state.status != previous_status:
            if event.type == "steering_queued":
                notice = self.state.status
                self.state.status = previous_status
                self.set_status(notice)
            elif self._allow_confirm_open() and event.type == "interaction_requested":
                self._show_allow_confirm_status()
            else:
                self.set_status(self.state.status, transient=False)
        if event.type in {"run_started", "run_cancelling"} or not self.state.running:
            self._working_messages().reset()
        if event.type == "runtime_config_applied" and isinstance(event.result, dict):
            changes = []
            if event.result.get("model_changed"):
                self.state.usage.clear()
                model = self.runner.current_model()
                changes.append(f"Model: {model_display_name(model) if model else event.result['model_id']} · {event.result['reasoning_effort']}")
            elif event.result.get("reasoning_changed"):
                model = self.runner.current_model()
                changes.append(f"Model: {model_display_name(model) if model else event.result['model_id']} · {event.result['reasoning_effort']}")
            if event.result.get("permission_changed"):
                changes.append(f"permission: {event.result['permission_mode']}")
            if event.result.get("sandbox_changed"):
                changes.append(f"sandbox: {event.result['sandbox_mode']}")
            if event.result.get("mcp_changed"):
                self._announce_mcp_switch()
            if announce:
                if event.result.get("model_changed") or event.result.get("reasoning_changed"):
                    self.set_status("; ".join(changes))
                if event.result.get("permission_changed"):
                    self.state.add_system(f"permission: {event.result['permission_mode']}")
                if event.result.get("sandbox_changed"):
                    self.state.add_system(f"sandbox: {event.result['sandbox_mode']}")
        elif event.type == "runtime_config_failed":
            self.state.add_system(f"Pending runtime config failed: {event.content}", error=True)
        self.invalidate()

    def _handle_result(self, result: RunResult) -> None:
        if result.status == "cancelled":
            self.state.running = False
            if result.error:
                self.state.add_system(result.error, error=True)
                self.set_status("Cancel cleanup not confirmed", transient=False)
            elif self.state.status != "Cancelled":
                self.set_status("Cancelled", transient=False)
        if result.status == "paused":
            self.state.running = False
            self.state.add_system("Paused at a checkpoint. Submit /pause again is unnecessary; press Enter to resume.")
        if result.status in {"waiting_confirmation", "waiting_human", "paused"}:
            if self._allow_confirm_open():
                self._note_interrupt_behind_allow_confirm(result.status)
            elif result.status == "waiting_confirmation" and (
                self.buffer.text or self.state.attachments or self.dialogs.current is not None
            ):
                # An asynchronous approval must not take over an editor or picker.
                self.state.running = False
                self.set_status("Approval still pending · F2 to decide", transient=False)
            else:
                self._reopen_pending_interaction()
        elif self.dialogs.deferred is not None:
            self.dialogs.current = self.dialogs.deferred
            self.dialogs.deferred = None
            self._show_allow_confirm_status()
        elif result.status == "completed":
            # Input can arrive after the worker's final queue check, while
            # the UI still considers that worker to be running.
            self._start_queued_run()
        self.invalidate()
