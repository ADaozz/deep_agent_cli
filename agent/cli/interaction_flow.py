"""CLI interaction flow workflow."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable
from typing import Any
from functools import partial

from agent.cli.interactions import InteractionController
from agent.cli.pasted_content import PastedContentDraft
from agent.cli.skills import SkillDraft
from agent.runtime.contracts import InterruptKind, UnknownInterruptError


if TYPE_CHECKING:
    from agent.cli.access_selection import AccessSelection
    from agent.runtime.runner import AgentRunner
    from prompt_toolkit.buffer import Buffer
    from agent.cli.state import CliState
    from agent.cli.view_state import DialogState
    from agent.cli.view_state import InputDrafts
    from agent.cli.model_selection import ModelSelection
    from agent.cli.run_dispatch import RunDispatch
    from agent.cli.session_controller import SessionController


class InteractionFlow:
    def __init__(
        self, *,
        _on_event_thread: Callable[..., Any],
        access_selection: Callable[[], AccessSelection],
        buffer: Buffer,
        dialogs: DialogState,
        drafts: InputDrafts,
        model_selection: Callable[[], ModelSelection],
        skills: Callable[..., Any],
        run_dispatch: Callable[[], RunDispatch],
        runner: AgentRunner,
        sessions: Callable[[], SessionController],
        set_status: Callable[..., None],
        state: CliState,
        invalidate: Callable[[], None],
        finish_mcp: Callable[..., None],
        finish_secrets: Callable[..., None] | None = None,
        clear_secret_buffer: Callable[[], None] | None = None,
        attach_process: Callable[[str, str], None] | None = None,
    ) -> None:
        self._on_event_thread = _on_event_thread
        self.access_selection = access_selection
        self.buffer = buffer
        self.dialogs = dialogs
        self.drafts = drafts
        self.skills = skills
        self.model_selection = model_selection
        self.run_dispatch = run_dispatch
        self.runner = runner
        self.sessions = sessions
        self.set_status = set_status
        self.state = state
        self.invalidate = invalidate
        self.finish_mcp = finish_mcp
        self.finish_secrets = finish_secrets
        self._clear_secret_buffer = clear_secret_buffer or (lambda: None)
        self._attach_process = attach_process

    def _finish_interaction(self, *, cancelled: bool = False) -> None:
        interaction = self.dialogs.current
        if interaction is None:
            return
        if interaction.kind.startswith("secret_"):
            if self.finish_secrets is not None:
                self.finish_secrets(interaction, cancelled=cancelled)
            else:
                interaction.clear_secrets()
                self._clear_secret_buffer()
                self.dialogs.current = None
            return
        if interaction.source == "runtime_auth":
            callback = interaction.auth_callback
            secret = None if cancelled else interaction.runtime_auth_secret()
            interaction.clear_secrets()
            self._clear_secret_buffer()
            self.dialogs.current = None
            deferred = self.dialogs.deferred
            if deferred is not None:
                self.dialogs.current = deferred
                self.dialogs.deferred = None
            if callable(callback):
                callback(secret)
            self.set_status("Authentication cancelled" if cancelled else "Authentication submitted")
            self.invalidate()
            return
        if interaction.kind == "process_attach":
            self.dialogs.current = None
            if cancelled:
                self.set_status("Process attach cancelled")
                self.invalidate()
                return
            exec_id = str(interaction.values.get("execution_id") or "")
            match = next(
                (item for item in self.runner.list_active_executions() if item.get("execution_id") == exec_id),
                None,
            )
            if match is None:
                self.state.add_system(f"Process {exec_id or '<unknown>'} is no longer running.", error=True)
            elif self._attach_process is not None:
                self._attach_process(str(match["execution_id"]), str(match.get("stdin_mode") or "pipe"))
            self.invalidate()
            return
        if interaction.kind.startswith("mcp_"):
            self.finish_mcp(interaction, cancelled=cancelled)
            return
        if interaction.kind == "approval":
            self._restore_approval_draft()
        if interaction.kind == "skill":
            self.skills().finish_interaction(interaction, cancelled=cancelled)
            return
        if interaction.kind == "resume":
            if cancelled:
                self.dialogs.current = None
                self.set_status("Resume cancelled")
                return
            session_id = str(interaction.values.get("session") or "")
            self.dialogs.current = None
            if not session_id:
                self.state.add_system("No session selected", error=True)
                return
            self.sessions().begin_switch(session_id)
            return
        if interaction.kind in {"model_source", "model", "model_reasoning"}:
            self.model_selection().finish_interaction(interaction, cancelled=cancelled)
            return
        if interaction.kind in {"permission", "sandbox", "permission_confirm"}:
            self.access_selection().finish_interaction(interaction, cancelled=cancelled)
            return
        if cancelled and interaction.kind in {"pause", "approval", "human"}:
            if interaction.kind == "human":
                self._restore_approval_draft()
            interaction.clear_secrets()
            self._clear_secret_buffer()
            self._dismiss_pending_interrupt(interaction.kind)
            return
        if interaction.kind == "pause":
            if interaction.values.get("continue") == "continue":
                self.dialogs.current = None
                self.run_dispatch()._start_run("", resume_call=partial(
                    self.runner.continue_run, on_event=self._on_event_thread,
                ))
                return
            self._dismiss_pending_interrupt("pause")
            return
        if interaction.kind == "approval":
            ids = [item for item in interaction.tool_call_ids if item]
            approved = str(interaction.values.get("approved") or "") == "approve"
            self.dialogs.current = None
            if approved and len(ids) == 1:
                resume_call = partial(
                    self.runner.approve_tool, ids[0], on_event=self._on_event_thread,
                )
            elif approved:
                resume_call = partial(
                    self.runner._decide_listed_tools, ids, approved=True,
                    on_event=self._on_event_thread,
                )
            elif len(ids) == 1:
                resume_call = partial(
                    self.runner.reject_tool, ids[0], on_event=self._on_event_thread,
                )
            else:
                resume_call = partial(
                    self.runner._decide_listed_tools, ids, approved=False,
                    on_event=self._on_event_thread,
                )
            self.run_dispatch()._start_run("", resume_call=resume_call)
            return
        # Agent human_input: shared security confirmation flow (persistence decline & overwrite check).
        status = interaction.prepare_secret_submission(self.runner.check_secret_overwrites)
        if status == "declined":
            self._restore_approval_draft()
            interaction.clear_secrets()
            self._clear_secret_buffer()
            self._dismiss_pending_interrupt("human")
            return
        if status == "needs_overwrite":
            self.set_status("Credential exists · confirm overwrite", transient=False)
            self.invalidate()
            return
        values = interaction.plain_values()
        secret_subs = interaction.secret_submissions()
        overwrite = interaction.is_overwrite_authorized()
        interaction.clear_secrets()
        self._clear_secret_buffer()
        self.dialogs.current = None
        self._restore_approval_draft()
        if secret_subs:
            resume_call = partial(
                self.runner.submit_human_input,
                values,
                secrets=secret_subs,
                overwrite_secrets=overwrite,
                on_event=self._on_event_thread,
            )
        else:
            resume_call = partial(
                self.runner.submit_human_input,
                values,
                on_event=self._on_event_thread,
            )
        self.run_dispatch()._start_run("", resume_call=resume_call)

    def _restore_approval_draft(self) -> None:
        if self.dialogs.approval_draft is None:
            return
        document, pasted, skill = self.dialogs.approval_draft
        self.dialogs.approval_draft = None
        self.drafts.pasted = pasted
        self.drafts.skill = skill
        self.buffer.set_document(document)

    def _dismiss_pending_interrupt(self, kind: str) -> None:
        self.dialogs.current = None
        self.state.running = False
        deferred = self.dialogs.deferred
        if deferred is not None:
            self.dialogs.current = deferred
            self.dialogs.deferred = None
            if deferred.kind == "permission_confirm":
                self.access_selection()._show_allow_confirm_status()
            self.invalidate()
            return
        label = {"approval": "Approval", "human": "Input", "pause": "Pause"}.get(kind, "Input")
        self.set_status(f"{label} still pending · F2 to decide", transient=False)

    def _reopen_pending_interaction_preserving_allow(self) -> bool:
        """F2: show the pending approval, and return to an open ALLOW prompt on Esc."""
        confirm = self.dialogs.current if self.access_selection()._allow_confirm_open() else None
        if confirm is not None:
            self.dialogs.deferred = confirm
            self.dialogs.current = None
        opened = self._reopen_pending_interaction()
        if confirm is not None and not opened:
            self.dialogs.deferred = None
            self.dialogs.current = confirm
            self.access_selection()._show_allow_confirm_status()
        return opened

    def _approval_execution_scope(self) -> str:
        mode = self.runner.prepared.execution_mode.value.lower()
        if mode == "sandboxed":
            return f"Sandbox ({self.runner.sandbox_mode()})"
        return "Custom backend"

    def _approval_mcp_tool_lookup(self) -> Callable[[str], tuple[str, str] | None] | None:
        manager = getattr(self.runner, "mcp", None)
        if manager is None or not hasattr(manager, "list_servers"):
            return None
        mapping: dict[str, tuple[str, str]] = {}
        try:
            for server_state in manager.list_servers():
                for tool in getattr(server_state, "tools", ()) or ():
                    tool_name = str(getattr(tool, "name", "") or "")
                    if not tool_name:
                        continue
                    server_label = str(getattr(tool, "mcp_server_name", "") or "")
                    remote_name = str(getattr(tool, "mcp_remote_name", "") or "")
                    if server_label or remote_name:
                        mapping[tool_name] = (server_label, remote_name)
        except Exception:  # noqa: BLE001
            return None
        return mapping.get if mapping else None

    def _trusted_secret_consumers(self, payload: dict[str, Any]) -> dict[str, tuple[str, ...]]:
        """Derive trusted global MCP consumers from host MCPManager config (never from model text)."""
        manager = getattr(self.runner, "mcp", None)
        if manager is None or not hasattr(manager, "list_servers"):
            return {}
        from agent.mcp.adapter import mcp_secret_names

        try:
            servers = list(manager.list_servers())
        except Exception:  # noqa: BLE001
            return {}
        by_secret: dict[str, list[str]] = {}
        for server_state in servers:
            cfg = getattr(server_state, "config", None)
            if cfg is None:
                continue
            for cred_name in mcp_secret_names(cfg):
                by_secret.setdefault(cred_name, [])
                if cfg.name not in by_secret[cred_name]:
                    by_secret[cred_name].append(cfg.name)
        matched: dict[str, tuple[str, ...]] = {}
        for item in payload.get("fields") or []:
            if not isinstance(item, dict) or item.get("type") != "secret":
                continue
            if str(item.get("secret_scope") or "").strip().lower() != "host":
                continue
            name = str(item.get("secret_name") or "").strip()
            if name and name in by_secret:
                matched[name] = tuple(by_secret[name])
        return matched

    def _save_editor_draft_if_present(self) -> None:
        if self.dialogs.current is not None or self.dialogs.deferred is not None or self.dialogs.approval_draft is not None:
            return
        if self.buffer.text or self.drafts.pasted.has_blocks or self.drafts.skill.has_blocks:
            self.dialogs.approval_draft = (self.buffer.document, self.drafts.pasted, self.drafts.skill)
            self.drafts.pasted = PastedContentDraft()
            self.drafts.skill = SkillDraft()
            self.buffer.reset()

    def _reopen_pending_interaction(self, *, notify_missing: bool = True) -> bool:
        try:
            interrupt = self.runner.current_interrupt()
        except UnknownInterruptError as exc:
            self.state.add_system(str(exc), error=True)
            self.set_status("Unknown interrupt")
            return False
        if interrupt is None:
            if notify_missing:
                self.state.add_system("No pending interaction")
                self.set_status("No pending interaction")
            return False
        if interrupt.kind is InterruptKind.RUNTIME_CONFIG_BOUNDARY:
            self.state.add_system("Completing a pending runtime config switch before the next model call.")
            self.run_dispatch()._start_run("", resume_call=partial(
                self.runner.resume_runtime_config, on_event=self._on_event_thread,
            ))
            return True
        if interrupt.kind is InterruptKind.WAITING_CONFIRMATION:
            if self.dialogs.current is None and self.dialogs.deferred is None and self.dialogs.approval_draft is None:
                self.dialogs.approval_draft = (self.buffer.document, self.drafts.pasted, self.drafts.skill)
                self.drafts.pasted = PastedContentDraft()
                self.drafts.skill = SkillDraft()
                self.buffer.reset()
            self.dialogs.current = InteractionController.approval(
                list(interrupt.pending_tools),
                secrets=self.runner.summary_secrets(),
                execution_scope=self._approval_execution_scope(),
                mcp_tool_lookup=self._approval_mcp_tool_lookup(),
            )
            self.set_status("Waiting for input", transient=False)
        elif interrupt.kind is InterruptKind.WAITING_HUMAN:
            self._save_editor_draft_if_present()
            self.dialogs.current = InteractionController.human(
                interrupt.payload,
                secret_consumers=self._trusted_secret_consumers(interrupt.payload),
            )
            self.set_status("Waiting for input", transient=False)
        elif interrupt.kind is InterruptKind.PAUSED:
            self.dialogs.current = InteractionController.pause()
            self.set_status("Paused", transient=False)
        else:
            self.state.add_system(f"Unsupported interrupt: {interrupt.kind}", error=True)
            return False
        self.invalidate()
        return True
