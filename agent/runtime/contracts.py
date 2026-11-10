"""Presentation-neutral run and interrupt contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable


@dataclass(frozen=True)
class RunEvent:
    """Presentation-neutral event emitted while a graph run is progressing."""

    type: str
    content: str = ""
    message_id: str = ""
    tool_call_id: str = ""
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    is_error: bool = False
    stream: str = ""
    # Structured tool result payload (ToolMessage.artifact), forwarded as-is
    # for every tool, not just execute.
    artifact: Any = None


@dataclass(frozen=True)
class TurnTiming:
    elapsed_seconds: float
    finished_at: datetime


RunEventHandler = Callable[[RunEvent], None]


@dataclass
class RunResult:
    status: str
    output: str = ""
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    human_input: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class CompactResult:
    status: str
    used_tokens: int | None = None
    window_tokens: int = 0
    message: str = ""

    @property
    def percent(self) -> float | None:
        return self.used_tokens / self.window_tokens * 100 if self.window_tokens > 0 and self.used_tokens is not None else None


class InterruptKind(StrEnum):
    PAUSED = "paused"
    WAITING_HUMAN = "waiting_human"
    WAITING_CONFIRMATION = "waiting_confirmation"
    RUNTIME_CONFIG_BOUNDARY = "runtime_config_boundary"


class UnknownInterruptError(ValueError):
    """Checkpoint or stream has an interrupt that is not a known protocol."""


@dataclass(frozen=True)
class InterruptState:
    kind: InterruptKind
    payload: dict[str, Any]
    pending_tools: tuple[dict[str, Any], ...] = ()
