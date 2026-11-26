"""Shared file mutation operation vocabulary and edit position capture."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Literal


FileMutationOperation = Literal["create", "modify", "overwrite", "delete"]
WriteOperation = Literal["create", "overwrite"]


@dataclass
class EditPositionCapture:
    """Actual match position recorded while one edit tool call executes.

    The backend reads the file exactly once; this mutable holder is shared with
    the tool wrapper through a ContextVar so the success ToolMessage can carry
    the position as structured artifact metadata. It is not session state.
    """

    start_line: int | None = None
    occurrences: int | None = None


_EDIT_CAPTURE: ContextVar[EditPositionCapture | None] = ContextVar(
    "deep_agent_edit_position_capture", default=None,
)


def set_edit_capture(capture: EditPositionCapture) -> Token[EditPositionCapture | None]:
    return _EDIT_CAPTURE.set(capture)


def reset_edit_capture(token: Token[EditPositionCapture | None]) -> None:
    _EDIT_CAPTURE.reset(token)


def record_edit_position(*, start_line: int | None, occurrences: int | None) -> None:
    """Attach the backend's real match position to the active edit call, if any."""
    capture = _EDIT_CAPTURE.get()
    if capture is None:
        return
    capture.start_line = start_line
    capture.occurrences = occurrences
