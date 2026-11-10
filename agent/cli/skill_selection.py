"""Terminal skill selection use case."""

from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from agent.runtime.runner import AgentRunner
    from agent.cli.session_controller import SessionController
    from agent.cli.view_state import DialogState, InputDrafts, RunPresentation
    from agent.cli.state import CliState
    from prompt_toolkit.buffer import Buffer

from typing import Any

from prompt_toolkit.document import Document

from agent.cli.interactions import InteractionController
from agent.cli.skills import load_skill_catalog


class SkillSelection:
    def __init__(self, *, runner: AgentRunner, sessions: Callable[[], SessionController], dialogs: DialogState, drafts: InputDrafts, buffer: Buffer, run_view: RunPresentation, is_exiting: Callable[[], bool], run_blocking: Callable[..., Awaitable[Any]], set_status: Callable[..., None], state: CliState, invalidate: Callable[[], None]) -> None:
        self.runner = runner
        self.sessions = sessions
        self.dialogs = dialogs
        self.drafts = drafts
        self.buffer = buffer
        self.run_view = run_view
        self.is_exiting = is_exiting
        self.run_blocking = run_blocking
        self.set_status = set_status
        self.state = state
        self.invalidate = invalidate

    async def select_skill(self, arg: str = "") -> None:
        if self.sessions().waiting or self.dialogs.current is not None or self.run_view.compacting:
            self.set_status("Finish the current interaction before selecting a skill")
            return
        if arg:
            self.state.add_system("Usage: /skill", error=True)
            return
        prepared = self.runner.prepared
        try:
            catalog = await self.run_blocking(load_skill_catalog, prepared)
        except (OSError, ValueError, RuntimeError) as exc:
            self.state.add_system(f"Cannot load skills: {exc}", error=True)
            return
        if (self.sessions().waiting or self.dialogs.current is not None or self.run_view.compacting
                or self.is_exiting() or self.runner.prepared is not prepared):
            return
        for warning in catalog.get("skills_load_errors", []):
            self.state.add_system(str(warning), error=True)
        skills = sorted(catalog.get("skills_metadata", []), key=lambda item: item["name"])
        if not skills:
            self.state.add_system("No valid skills in ~/.deep-agent/skills/ (SKILL.md requires name and description)")
            self.set_status("No skills available")
            return
        self.dialogs.skill_picker_draft = (self.buffer.text, self.buffer.cursor_position)
        self.buffer.reset()
        self.dialogs.current = InteractionController(
            kind="skill", title="Select skill", question="Choose a skill for the next message",
            fields=[{"id": "skill", "type": "single_select", "label": "Skill", "required": True,
                     "options": [{"value": item["path"], "label": item["name"],
                                  "description": item["description"]} for item in skills]}],
        )
        self.set_status("Select a skill · Enter confirm · Esc cancel", transient=False)
        self.invalidate()


    def _clear_skill_draft(self) -> None:
        self.buffer.text = self.drafts.skill.expand(self.buffer.text)
        self.drafts.skill.clear()
        self.dialogs.skill_picker_draft = None


    def finish_interaction(self, interaction: InteractionController, *, cancelled: bool) -> None:
        self.dialogs.current = None
        draft, cursor = self.dialogs.skill_picker_draft or ("", 0)
        self.dialogs.skill_picker_draft = None
        self.buffer.text = draft
        self.buffer.cursor_position = cursor
        if cancelled:
            self.set_status("Skill selection cancelled")
            return
        path = str(interaction.values.get("skill") or "")
        option = next((item for item in interaction.fields[0]["options"] if item["value"] == path), None)
        if option is None:
            return
        for label in self.drafts.skill.labels:
            cursor -= len(label) * draft[:cursor].count(label)
            draft = draft.replace(label, "")
        label = self.drafts.skill.display(option["label"], path)
        self.buffer.document = Document(
            draft[:cursor] + label + " " + draft[cursor:], cursor_position=cursor + len(label) + 1,
        )
        self.set_status("Skill selected · Enter submit or add instructions")
        self.invalidate()
        return
