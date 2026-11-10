"""Slash command completion and model labels."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from prompt_toolkit.completion import Completer, Completion

from agent.cli.commands import Command
from agent.models.profile import ModelProfile


def model_display_name(profile: ModelProfile) -> str:
    """Include the configured source when two providers expose the same model."""
    return f"{profile.source} · {profile.model}" if profile.source else profile.model


class SlashCompleter(Completer):
    def __init__(
        self,
        commands: tuple[Command, ...],
        *,
        model_profiles: Callable[[], list[ModelProfile]] | None = None,
        permission_modes: Callable[[], list[str]] | None = None,
        sandbox_modes: Callable[[], list[str]] | None = None,
    ) -> None:
        self.commands = commands
        self.model_profiles = model_profiles or (lambda: [])
        self.permission_modes = permission_modes or (lambda: [])
        self.sandbox_modes = sandbox_modes or (lambda: [])
        self.accepted_text: str | None = None

    @staticmethod
    def _rank(name: str, query: str) -> tuple[int, int, int] | None:
        if name == query:
            return (0, 0, len(name))
        if name.startswith(query):
            return (1, 0, len(name))
        offset = name.find(query)
        return (2, offset, len(name)) if offset >= 0 else None

    def get_completions(self, document, complete_event):  # type: ignore[no-untyped-def]
        before = document.text_before_cursor
        if "\n" in before or not before.startswith("/"):
            return
        if before == self.accepted_text:
            return
        if " " in before:
            name, arg = before[1:].split(" ", 1)
            if " " in arg:
                return
            yield from self._argument_completions(name, arg)
            return
        prefix = before[1:].lower()
        candidates = []
        for index, command in enumerate(self.commands):
            rank = self._rank(command.name.lower(), prefix)
            if rank is not None:
                candidates.append((rank, index, command))
        for _, _, command in sorted(candidates):
            yield Completion(
                f"/{command.name}",
                start_position=-len(before),
                display_meta=command.description,
            )

    def _argument_completions(self, name: str, arg: str) -> Iterator[Completion]:
        candidates: list[tuple[str, str]] = []
        if name == "model":
            profiles = self.model_profiles()
            sources = list(dict.fromkeys(profile.id.split("/", 1)[0]
                                         for profile in profiles if "/" in profile.id))
            candidates.extend((source, "Model source") for source in sources)
            candidates.extend((profile.id, f"Model: {model_display_name(profile)}") for profile in profiles)
        elif name == "permission":
            descriptions = {"ask": "Require approval for tools", "allow": "Auto-approve tools (SANDBOXED, HIGH RISK)"}
            candidates.extend((mode, descriptions[mode]) for mode in self.permission_modes())
        elif name == "sandbox":
            descriptions = {
                "restricted": "Workspace, skills, and declared mounts; system directories stay read-only",
                "full-access": "Write the real filesystem except the agent install and configuration (HIGH RISK)",
            }
            candidates.extend((mode, descriptions[mode]) for mode in self.sandbox_modes())
        elif name == "mcp":
            candidates.extend((
                ("enable", "Enable MCP and connect configured servers"),
                ("disable", "Disable MCP without deleting servers or tokens"),
                ("add", "Add a host MCP server"),
                ("remove", "Remove a configured server"),
                ("test", "Connect and list tools"),
                ("tools", "Show one server's tools"),
            ))
        ranked = []
        for index, (value, description) in enumerate(candidates):
            rank = self._rank(value.lower(), arg.lower())
            if rank is not None:
                ranked.append((rank, index, value, description))
        for _, _, value, description in sorted(ranked):
            yield Completion(value, start_position=-len(arg), display_meta=description)
