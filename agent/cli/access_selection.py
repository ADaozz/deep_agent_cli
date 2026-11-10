"""CLI access selection workflow."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable
from typing import Any

from agent.cli.interactions import InteractionController
from agent.execution.policy import parse_sandbox_mode
from agent.runtime.permissions import (
    PERMISSION_ALLOW_WARNING,
    PermissionMode,
    allow_mode_available,
    allow_mode_unavailable_reason,
    parse_permission_mode,
)
from agent.runtime.contracts import InterruptKind, RunEvent, UnknownInterruptError


if TYPE_CHECKING:
    from agent.runtime.runner import AgentRunner
    from agent.cli.state import CliState
    from agent.cli.view_state import DialogState
    from agent.cli.interaction_flow import InteractionFlow
    from agent.cli.run_events_controller import RunEventsController
    from agent.cli.session_controller import SessionController


class AccessSelection:
    def __init__(
        self, *,
        dialogs: DialogState,
        interaction_flow: Callable[[], InteractionFlow],
        run_events: Callable[[], RunEventsController],
        runner: AgentRunner,
        sessions: Callable[[], SessionController],
        set_status: Callable[..., None],
        state: CliState,
        invalidate: Callable[[], None],
    ) -> None:
        self.dialogs = dialogs
        self.interaction_flow = interaction_flow
        self.run_events = run_events
        self.runner = runner
        self.sessions = sessions
        self.set_status = set_status
        self.state = state
        self.invalidate = invalidate


    def _request_permission_mode(self, mode: PermissionMode) -> None:
        before = self.runner.permission_mode()
        self.runner.request_permission_change(mode)
        interrupt = self.runner.current_interrupt()
        events: list[RunEvent] = []
        if not self.state.running and interrupt is None and self.runner.apply_pending_config_if_idle(events.append):
            for event in events:
                self.run_events()._apply_event(event, announce=False)
            if any(event.type == "runtime_config_failed" for event in events):
                self.set_status(f"Permission switch failed; permission remains {self.runner.permission_mode().value}.")
                return
        if self.runner.pending_permission_mode() is None:
            notice = f"Permission: {self.runner.permission_mode().value}."
        else:
            notice = f"Permission switch queued: {before.value} → {mode.value}. Current work uses {before.value}."
        if interrupt is not None and interrupt.kind is InterruptKind.WAITING_CONFIRMATION:
            notice += " Existing approval remains under ask; press F2 to approve or reject."
        self.state.add_system(notice)
        self.set_status(notice)

    def _request_sandbox_mode(self, mode: str) -> None:
        before = self.runner.sandbox_mode()
        try:
            self.runner.request_sandbox_mode(mode)
        except ValueError as exc:
            self.state.add_system(str(exc), error=True)
            return
        interrupt = self.runner.current_interrupt()
        events: list[RunEvent] = []
        if not self.state.running and interrupt is None and self.runner.apply_pending_config_if_idle(events.append):
            for event in events:
                self.run_events()._apply_event(event, announce=False)
            if any(event.type == "runtime_config_failed" for event in events):
                self.set_status(f"Sandbox switch failed; filesystem mode remains {self.runner.sandbox_mode()}.")
                return
        if self.runner.pending_sandbox_mode() is None:
            notice = f"Sandbox filesystem: {self.runner.sandbox_mode()}."
        else:
            notice = f"Sandbox switch queued: {before} → {mode}. Current work uses {before}."
        self.state.add_system(notice)
        self.set_status(notice)

    async def select_permission(self, arg: str = "") -> None:
        if self.sessions().waiting:
            self.set_status(self.sessions().wait_status(), transient=False)
            return
        if self.dialogs.current is not None:
            self.set_status("Finish the current interaction before changing permission mode")
            return
        raw = arg.strip()
        if raw:
            mode = parse_permission_mode(raw)
            if mode is None:
                hint = "ask or allow" if self._allow_available() else "ask"
                self.state.add_system(
                    f"Unknown permission mode. Use /permission {hint}",
                    error=True,
                )
                return
            await self._apply_permission_mode(mode)
            return
        if self.state.running:
            self.set_status("Use /permission ask|allow while a run is active")
            return
        current = self.runner.permission_mode()
        options = [
            {
                "value": PermissionMode.ASK.value,
                "label": "ask · require approval for every execute and side-effect tool"
                + (" · current" if current is PermissionMode.ASK else ""),
            },
        ]
        if self._allow_available():
            options.append({
                "value": PermissionMode.ALLOW.value,
                "label": "allow · auto-approve all tools; sandbox network open (SANDBOXED, HIGH RISK)"
                + (" · current" if current is PermissionMode.ALLOW else ""),
            })
        if len(options) == 1:
            self.state.add_system(
                "Permission mode is locked to ask because ALLOW requires SANDBOXED execution.",
            )
            self.set_status("Permission: ask")
            return
        self.dialogs.current = InteractionController(
            kind="permission",
            title="Permission mode",
            question="Choose how tools are approved before they run",
            fields=[{
                "id": "mode",
                "type": "single_select",
                "label": "Mode",
                "required": True,
                "options": options,
            }],
        )
        self.dialogs.current.option_index = 0 if current is PermissionMode.ASK else 1
        self.set_status("Select permission mode · Enter confirm · Esc cancel", transient=False)
        self.invalidate()

    def _show_sandbox_mounts(self) -> None:
        from agent.execution.mounts import render_launch_manifest

        backend = getattr(self.runner.prepared, "backend", None)
        executor = getattr(backend, "executor", None)
        config = getattr(executor, "config", None) or self.runner._sandbox_config
        if config is None:
            self.state.add_system("Sandbox mount policy is not available for this backend.", error=True)
            return
        skills = getattr(backend, "skills_dir", None)
        self.state.add_system(render_launch_manifest(
            config, skills_dir=skills, pending_mode=self.runner.pending_sandbox_mode(),
        ))
        self.invalidate()

    async def select_sandbox(self, arg: str = "") -> None:
        if self.sessions().waiting:
            self.set_status(self.sessions().wait_status(), transient=False)
            return
        if self.dialogs.current is not None:
            self.set_status("Finish the current interaction before changing sandbox mode")
            return
        raw = arg.strip()
        if raw == "mounts":
            self._show_sandbox_mounts()
            return
        if raw:
            mode = parse_sandbox_mode(raw)
            if mode is None:
                self.state.add_system(
                    "Unknown sandbox mode. Use /sandbox restricted, /sandbox full-access, or /sandbox mounts",
                    error=True,
                )
                return
            self._request_sandbox_mode(mode)
            return
        if self.state.running:
            self.set_status("Use /sandbox restricted|full-access while a run is active")
            return
        current = self.runner.sandbox_mode()
        options = [
            {
                "value": "restricted",
                "label": "restricted · workspace, skills, and declared mounts; system directories stay read-only"
                + (" · current" if current == "restricted" else ""),
            },
            {
                "value": "full-access",
                "label": "full-access · write the real filesystem except the agent install and configuration (HIGH RISK)"
                + (" · current" if current == "full-access" else ""),
            },
        ]
        self.dialogs.current = InteractionController(
            kind="sandbox",
            title="Sandbox filesystem",
            question="Choose which files execute and file tools can change. ask/allow is unchanged.",
            fields=[{
                "id": "mode",
                "type": "single_select",
                "label": "Mode",
                "required": True,
                "options": options,
            }],
        )
        self.dialogs.current.option_index = 0 if current == "restricted" else 1
        self.set_status("Select sandbox mode · Enter confirm · Esc cancel", transient=False)
        self.invalidate()

    def _allow_available(self) -> bool:
        return allow_mode_available(self.runner.prepared.execution_mode)

    async def _apply_permission_mode(self, mode: PermissionMode) -> None:
        if mode is PermissionMode.ALLOW and not self._allow_available():
            self.state.add_system(
                allow_mode_unavailable_reason(self.runner.prepared.execution_mode),
                error=True,
            )
            return
        if (mode is PermissionMode.ALLOW and self.runner.permission_mode() is not PermissionMode.ALLOW
                and self.runner.pending_permission_mode() is not PermissionMode.ALLOW):
            self._begin_allow_permission_confirm()
            return
        try:
            self._request_permission_mode(mode)
        except (RuntimeError, ValueError) as exc:
            self.state.add_system(str(exc), error=True)
            return

    def _begin_allow_permission_confirm(self) -> None:
        self.dialogs.current = InteractionController(
            kind="permission_confirm",
            title="Enable ALLOW? (HIGH RISK)",
            question=PERMISSION_ALLOW_WARNING,
            fields=[{
                "id": "confirm",
                "type": "text",
                "label": "Type ALLOW to confirm",
                "required": True,
                "options": [],
            }],
        )
        self._show_allow_confirm_status()
        self.invalidate()

    def _allow_confirm_open(self) -> bool:
        return self.dialogs.current is not None and self.dialogs.current.kind == "permission_confirm"

    def _current_interrupt_or_none(self) -> Any:
        try:
            return self.runner.current_interrupt()
        except UnknownInterruptError:
            return None

    def _show_allow_confirm_status(self) -> None:
        status = "Type ALLOW to confirm · Esc cancel"
        interrupt = self._current_interrupt_or_none()
        kind = interrupt.kind if interrupt is not None else None
        if kind is InterruptKind.WAITING_CONFIRMATION:
            status += " · approval pending · F2"
        elif kind is InterruptKind.WAITING_HUMAN:
            status += " · question pending · F2"
        elif kind is InterruptKind.PAUSED:
            status += " · pause pending · F2"
        self.set_status(status, transient=False)

    def _note_interrupt_behind_allow_confirm(self, status: str) -> None:
        if status == "waiting_confirmation":
            notice = "Tool approval is waiting. This ALLOW prompt stays open; F2 opens the approval and Esc returns here."
        elif status == "waiting_human":
            notice = "A question is waiting. This ALLOW prompt stays open; F2 opens it and Esc returns here."
        else:
            notice = "A pause is waiting. This ALLOW prompt stays open; F2 opens it and Esc returns here."
        self.state.add_system(notice)
        self._show_allow_confirm_status()


    def finish_interaction(self, interaction: InteractionController, *, cancelled: bool) -> None:
        if interaction.kind == "permission":
            if cancelled:
                self.dialogs.current = None
                self.set_status("Permission change cancelled")
                return
            mode_raw = str(interaction.values.get("mode") or "")
            self.dialogs.current = None
            mode = parse_permission_mode(mode_raw)
            if mode is None:
                self.state.add_system("No permission mode selected", error=True)
                return
            if mode is PermissionMode.ALLOW and not allow_mode_available(self.runner.prepared.execution_mode):
                self.state.add_system(
                    allow_mode_unavailable_reason(self.runner.prepared.execution_mode),
                    error=True,
                )
                return
            if (mode is PermissionMode.ALLOW and self.runner.permission_mode() is not PermissionMode.ALLOW
                    and self.runner.pending_permission_mode() is not PermissionMode.ALLOW):
                self._begin_allow_permission_confirm()
                return
            try:
                self._request_permission_mode(mode)
            except (RuntimeError, ValueError) as exc:
                self.state.add_system(str(exc), error=True)
                return
            return
        if interaction.kind == "sandbox":
            if cancelled:
                self.dialogs.current = None
                self.set_status("Sandbox change cancelled")
                return
            mode_raw = str(interaction.values.get("mode") or "")
            self.dialogs.current = None
            sandbox_mode = parse_sandbox_mode(mode_raw)
            if sandbox_mode is None:
                self.state.add_system("No sandbox mode selected", error=True)
                return
            self._request_sandbox_mode(sandbox_mode)
            return
        if interaction.kind == "permission_confirm":
            if cancelled:
                self.dialogs.current = None
                if self.interaction_flow()._reopen_pending_interaction(notify_missing=False):
                    self.state.add_system("ALLOW mode cancelled")
                else:
                    self.set_status("ALLOW mode cancelled")
                return
            typed = str(interaction.values.get("confirm") or "").strip()
            self.dialogs.current = None
            if typed != "ALLOW":
                self.state.add_system(
                    'ALLOW not enabled. Type ALLOW exactly to confirm, or use /permission ask.',
                    error=True,
                )
                if not self.interaction_flow()._reopen_pending_interaction(notify_missing=False):
                    self.set_status("Permission: ask")
                return
            try:
                self._request_permission_mode(PermissionMode.ALLOW)
            except (RuntimeError, ValueError) as exc:
                self.state.add_system(str(exc), error=True)
            self.interaction_flow()._reopen_pending_interaction(notify_missing=False)
            return
