"""Shared file mutation operation vocabulary and edit position capture."""

from __future__ import annotations

from collections.abc import Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Literal


FileMutationOperation = Literal["create", "modify", "overwrite", "delete"]
WriteOperation = Literal["create", "overwrite"]


@dataclass
class EditPositionCapture:
    """Actual match positions recorded while one edit tool call executes.

    The backend reads the file exactly once; this mutable holder is shared with
    the tool wrapper through a ContextVar so the success ToolMessage can carry
    every match's start line as structured artifact metadata. It is not session
    state.
    """

    start_lines: list[int] = field(default_factory=list)
    occurrences: int | None = None


_EDIT_CAPTURE: ContextVar[EditPositionCapture | None] = ContextVar(
    "deep_agent_edit_position_capture", default=None,
)


def set_edit_capture(capture: EditPositionCapture) -> Token[EditPositionCapture | None]:
    return _EDIT_CAPTURE.set(capture)


def reset_edit_capture(token: Token[EditPositionCapture | None]) -> None:
    _EDIT_CAPTURE.reset(token)


def record_edit_position(*, start_lines: Sequence[int], occurrences: int | None) -> None:
    """Attach every real match start line to the active edit call, if any."""
    capture = _EDIT_CAPTURE.get()
    if capture is None:
        return
    capture.start_lines = list(start_lines)
    capture.occurrences = occurrences
