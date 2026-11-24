"""Recovery policy for a previously interrupted session."""

from __future__ import annotations

from dataclasses import dataclass

from agent.sessions.contracts import StopReason


@dataclass
class RecoveryContext:
    text: str | None
    armed: bool = False

    @classmethod
    def for_stop_reason(cls, reason: StopReason) -> "RecoveryContext | None":
        descriptions = {
            StopReason.PENDING: "The previous run may have ended before completion.",
            StopReason.ABORTED: "The previous run was stopped by the user.",
            StopReason.ERROR: "The previous run ended because of an execution error.",
        }
        description = descriptions.get(reason)
        if description is None:
            return None
        return cls(
            text=(
                "## Recovery\n"
                f"{description}\n"
                "Some operations may already have produced side effects.\n"
                "Inspect the relevant checkpoint and workspace state before continuing.\n"
                "Do not assume that unfinished operations succeeded or failed.\n"
                "Follow the user's current instructions."
            )
        )
