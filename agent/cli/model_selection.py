"""CLI model selection workflow."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from agent.cli.completion import model_display_name
from agent.cli.interactions import InteractionController
from agent.models.profile import ModelProfile
from agent.runtime.contracts import InterruptKind, RunEvent


if TYPE_CHECKING:
    from agent.runtime.runner import AgentRunner
    from agent.cli.state import CliState
    from agent.cli.view_state import DialogState
    from agent.cli.run_events_controller import RunEventsController
    from agent.cli.session_controller import SessionController


class ModelSelection:
    def __init__(
        self, *,
        dialogs: DialogState,
        run_events: Callable[[], RunEventsController],
        runner: AgentRunner,
        sessions: Callable[[], SessionController],
        set_status: Callable[..., None],
        state: CliState,
        invalidate: Callable[[], None],
    ) -> None:
        self.dialogs = dialogs
        self.run_events = run_events
        self.runner = runner
        self.sessions = sessions
        self.set_status = set_status
        self.state = state
        self.invalidate = invalidate


    def _switch_model(self, id_or_prefix: str, *, reasoning_effort: str | None = None) -> ModelProfile:
        profile = self.runner.request_model_change(id_or_prefix, reasoning_effort=reasoning_effort)
        interrupt = self.runner.current_interrupt()
        if not self.state.running and interrupt is None:
            events: list[RunEvent] = []
            if not self.runner.apply_pending_config_if_idle(events.append):
                events = []
            for event in events:
                self.run_events()._apply_event(event, announce=False)
            if any(event.type == "runtime_config_failed" for event in events):
                self.set_status("Model switch failed")
                return profile
        if self.runner.pending_model() is None and self.runner.pending_reasoning_effort() is None:
            notice = f"Model: {model_display_name(profile)} · {self.runner.reasoning_effort()}"
        else:
            notice = f"Model queued: {model_display_name(profile)} · {self.runner.pending_reasoning_effort() or self.runner.reasoning_effort()}"
        if interrupt is not None and interrupt.kind is InterruptKind.WAITING_CONFIRMATION:
            notice += " · F2: pending approval"
        self.set_status(notice)
        return profile

    async def select_model(self, arg: str = "") -> None:
        if self.sessions().waiting:
            self.set_status(self.sessions().wait_status(), transient=False)
            return
        if self.dialogs.current is not None:
            self.set_status("Finish the current interaction before switching models")
            return
        if self.state.attachments:
            self.set_status("Clear pending images before switching models")
            return
        profiles = self.runner.list_models()
        if not profiles:
            self.state.add_system(
                "/model is unavailable: configure llm.models in the agent config",
                error=True,
            )
            return
        try:
            if self.state.running:
                self.set_status("Finish the current run before selecting a model")
                return
            if arg.strip():
                groups = {item.id.split("/", 1)[0] for item in profiles if "/" in item.id}
                if arg.strip() in groups:
                    self._show_model_choices(arg.strip(), profiles)
                    return
                profile = self.runner.settings.get_profile(arg.strip())
                self._show_reasoning_choices(profile, profile.source)
                return
            if any("/" in item.id for item in profiles):
                self._show_model_sources(profiles)
                return
            self._show_model_choices("", profiles)
        except (KeyError, RuntimeError, ValueError) as exc:
            self.state.add_system(str(exc), error=True)

    def _show_model_sources(self, profiles: list[ModelProfile]) -> None:
        current = self.runner.current_model()
        current_source = current.id.split("/", 1)[0] if current and "/" in current.id else ""
        sources = list(dict.fromkeys(item.id.split("/", 1)[0] for item in profiles))
        options = [
            {"value": source, "label": source + (" · current" if source == current_source else "")}
            for source in sources
        ]
        self.dialogs.current = InteractionController(
            kind="model_source", title="Select model source", question="",
            fields=[{"id": "source", "type": "single_select", "label": "Source", "required": True, "options": options}],
        )
        if current_source in sources:
            self.dialogs.current.option_index = sources.index(current_source)
        self.set_status("Select a source · Enter confirm · Esc cancel", transient=False)
        self.invalidate()

    def _show_model_choices(self, source: str, profiles: list[ModelProfile]) -> None:
        if source:
            profiles = [item for item in profiles if item.id.startswith(f"{source}/")]
        if not profiles:
            raise KeyError(f"Unknown model source: {source}")
        current = self.runner.current_model()
        current_id = current.id if current else ""
        options = []
        for item in profiles:
            name = item.model if source else model_display_name(item)
            options.append({
                "value": item.id,
                "label": name + (" · current" if item.id == current_id else ""),
                "description": "",
            })
        self.dialogs.current = InteractionController(
            kind="model", title=f"Select model · {source}" if source else "Select model",
            question="", values={"source": source} if source else {},
            fields=[{"id": "model", "type": "single_select", "label": "Model", "required": True, "options": options}],
        )
        for index, option in enumerate(options):
            if option["value"] == current_id:
                self.dialogs.current.option_index = index
                break
        self.set_status("Select a model · Enter confirm · Esc back" if source else "Select a model · Enter confirm · Esc cancel", transient=False)
        self.invalidate()

    def _show_reasoning_choices(self, profile: ModelProfile, source: str = "") -> None:
        current = self.runner.current_model()
        current_effort = self.runner.reasoning_effort() if current and current.id == profile.id else None
        selected = current_effort or "default"
        pending = self.runner.pending_model() or current
        if pending and pending.id == profile.id and self.runner.pending_reasoning_effort() is not None:
            selected = self.runner.pending_reasoning_effort()
        efforts = ["default", *(effort for effort in profile.reasoning_efforts if effort != "default")]
        self.dialogs.current = InteractionController(
            kind="model_reasoning", title=f"Reasoning effort · {model_display_name(profile)}",
            question="",
            values={"model": profile.id, "source": source},
            fields=[{"id": "effort", "type": "single_select", "label": "Reasoning effort",
                     "required": True, "options": [
                         {"value": effort, "label": effort + (" · current" if effort == current_effort else ""),
                          "description": ""}
                         for effort in efforts
                     ]}],
        )
        self.dialogs.current.option_index = efforts.index(selected) if selected in efforts else 0
        self.set_status("Select reasoning effort · Enter confirm · Esc back", transient=False)
        self.invalidate()

    def cycle_model(self, *, delta: int = 1) -> None:
        if self.sessions().waiting:
            self.set_status(self.sessions().wait_status(), transient=False)
            return
        if self.dialogs.current is not None:
            self.set_status("Finish the current interaction before switching models")
            return
        if self.state.attachments:
            self.set_status("Clear pending images before switching models")
            return
        profiles = self.runner.list_models()
        if len(profiles) < 2:
            if not profiles:
                self.set_status("No models configured in the agent config")
            else:
                current = self.runner.current_model()
                label = model_display_name(current or profiles[0])
                self.set_status(f"Only one model configured: {label}")
            return
        current = self.runner.pending_model() or self.runner.current_model()
        current_id = current.id if current else profiles[0].id
        index = next((i for i, item in enumerate(profiles) if item.id == current_id), 0)
        nxt = profiles[(index + delta) % len(profiles)]
        try:
            self._switch_model(nxt.id)
        except (KeyError, RuntimeError) as exc:
            self.state.add_system(str(exc), error=True)
            return
        self.invalidate()


    def finish_interaction(self, interaction: InteractionController, *, cancelled: bool) -> None:
        if interaction.kind == "model_source":
            if cancelled:
                self.dialogs.current = None
                self.set_status("Model switch cancelled")
                return
            source = str(interaction.values.get("source") or "")
            try:
                self._show_model_choices(source, self.runner.list_models())
            except KeyError as exc:
                self.dialogs.current = None
                self.state.add_system(str(exc), error=True)
            return
        if interaction.kind == "model":
            if cancelled:
                source = str(interaction.values.get("source") or "")
                if source:
                    self._show_model_sources(self.runner.list_models())
                    return
                self.dialogs.current = None
                self.set_status("Model switch cancelled")
                return
            model_id = str(interaction.values.get("model") or "")
            self.dialogs.current = None
            if not model_id:
                self.state.add_system("No model selected", error=True)
                return
            try:
                self._show_reasoning_choices(
                    self.runner.settings.get_profile(model_id),
                    str(interaction.values.get("source") or ""),
                )
            except (KeyError, RuntimeError, ValueError) as exc:
                self.state.add_system(str(exc), error=True)
            return
        if interaction.kind == "model_reasoning":
            source = str(interaction.values.get("source") or "")
            if cancelled:
                self._show_model_choices(source, self.runner.list_models())
                model_id = interaction.values["model"]
                for index, option in enumerate(self.dialogs.current.fields[0]["options"]):
                    if option["value"] == model_id:
                        self.dialogs.current.option_index = index
                        break
                return
            self.dialogs.current = None
            try:
                self._switch_model(
                    str(interaction.values["model"]),
                    reasoning_effort=str(interaction.values.get("effort") or "default"),
                )
            except (KeyError, RuntimeError, ValueError) as exc:
                self.state.add_system(str(exc), error=True)
            return
