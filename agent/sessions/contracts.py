"""Session metadata and transcript values, independent of SQLite."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from agent.sessions.images import ImageAttachmentRef


SessionStatus = Literal[
    "running", "completed", "waiting", "cancelled", "failed",
]


class StopReason(StrEnum):
    PENDING = "pending"
    STOP = "stop"
    ERROR = "error"
    ABORTED = "aborted"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class SessionInfo:
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    status: SessionStatus
    model_id: str | None = None
    permission_mode: str | None = None
    pending_model_id: str | None = None
    pending_permission_mode: str | None = None
    reasoning_effort: str = "default"
    pending_reasoning_effort: str | None = None
    last_run_status: StopReason = StopReason.PENDING
    summary: str | None = None


@dataclass
class TranscriptBlock:
    kind: str
    content: str = ""
    thinking: str = ""
    tool_call_id: str = ""
    name: str = ""
    arguments: dict[str, Any] | None = None
    is_error: bool = False
    status: str = ""
    attachments: tuple[ImageAttachmentRef, ...] = ()
    exit_code: int | None = None
    # Raw ToolMessage.artifact restored from the checkpoint; UI previews are
    # re-derived from it instead of being persisted.
    artifact: Any = None
    elapsed_seconds: float = 0.0
    finished_at: datetime | None = None
