"""Session navigation outcomes shared with presentation adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.sessions.contracts import SessionInfo, TranscriptBlock
from agent.runtime.contracts import InterruptKind


@dataclass
class SessionSnapshot:
    info: SessionInfo
    transcript: list[TranscriptBlock]
    interrupt_kind: InterruptKind | None = None
    human_input: dict[str, Any] = field(default_factory=dict)
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    model_metrics_history: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class SessionSwitchResult:
    """Phase one of a session switch.

    ``busy`` means the target thread is owned elsewhere: the runner has already
    released its own lease and refuses operations until the wait completes or
    another session is selected.
    """

    status: str  # "switched" | "busy"
    target_id: str = ""
    snapshot: SessionSnapshot | None = None

    @property
    def busy(self) -> bool:
        return self.status == "busy"
