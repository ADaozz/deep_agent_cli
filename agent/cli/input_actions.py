"""Decide what a keyboard interrupt or exit key means.

Keymap owns the mapping from keys to actions. This router owns the policy:
which UI state wins, and when a second Ctrl+C is an exit. CliApplication
performs the chosen action. Run cancellation stays on RunController.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


EXIT_WINDOW_SECONDS = 1.0


class InputAction(Enum):
    NONE = "none"
    DISMISS_COMPLETION = "dismiss_completion"
    CLEAR_SLASH_LINE = "clear_slash_line"
    FOLLOW_TRANSCRIPT = "follow_transcript"
    CANCEL_COMPLETION = "cancel_completion"
    CANCEL_SESSION_WAIT = "cancel_session_wait"
    CLOSE_REVIEW = "close_review"
    EXIT_CUSTOM_ENTRY = "exit_custom_entry"
    CANCEL_INTERACTION = "cancel_interaction"
    WAIT_FOR_COMPACTION = "wait_for_compaction"
    CANCEL_RUN = "cancel_run"
    CANCEL_ALREADY_SENT = "cancel_already_sent"
    MCP_STILL_RUNNING = "mcp_still_running"
    CLEAR_INPUT = "clear_input"
    REQUEST_EXIT = "request_exit"
    DELETE_AFTER_CURSOR = "delete_after_cursor"


@dataclass(frozen=True)
class InputView:
    """Facts about the UI. The router, not the caller, orders them."""

    has_interaction: bool = False
    custom_entry: bool = False
    completion_open: bool = False
    slash_line: bool = False
    transcript_away: bool = False
    session_waiting: bool = False
    reviewing: bool = False
    compacting: bool = False
    running: bool = False
    cancel_sent: bool = False
    mcp_busy: bool = False
    buffer_empty: bool = True
    # Observed: exit in progress does not by itself suppress Esc.
    # exit() does not mark the run cancel as sent, so Esc can request it again.
    exiting: bool = False


class InputActionRouter:
    """Single policy for Ctrl+C, Esc, Ctrl+D, and /exit."""

    def __init__(self, *, exit_window_seconds: float = EXIT_WINDOW_SECONDS) -> None:
        self.exit_window_seconds = exit_window_seconds
        self.last_ctrl_c: float | None = None

    def decide_interrupt(self, view: InputView) -> InputAction:
        if not view.has_interaction and view.completion_open:
            return InputAction.DISMISS_COMPLETION
        if not view.has_interaction and view.slash_line:
            return InputAction.CLEAR_SLASH_LINE
        if view.transcript_away:
            return InputAction.FOLLOW_TRANSCRIPT
        if view.completion_open:
            return InputAction.CANCEL_COMPLETION
        if view.session_waiting:
            return InputAction.CANCEL_SESSION_WAIT
        if view.reviewing:
            return InputAction.CLOSE_REVIEW
        if view.has_interaction and view.custom_entry:
            return InputAction.EXIT_CUSTOM_ENTRY
        if view.has_interaction:
            return InputAction.CANCEL_INTERACTION
        if view.compacting:
            return InputAction.WAIT_FOR_COMPACTION
        if view.running:
            if view.cancel_sent:
                return InputAction.CANCEL_ALREADY_SENT
            return InputAction.CANCEL_RUN
        if view.mcp_busy:
            return InputAction.MCP_STILL_RUNNING
        return InputAction.NONE

    def decide_clear_or_exit(self, view: InputView, now: float) -> InputAction:
        if view.has_interaction:
            return InputAction.CANCEL_INTERACTION
        if self.last_ctrl_c is not None and now - self.last_ctrl_c < self.exit_window_seconds:
            return InputAction.REQUEST_EXIT
        return InputAction.CLEAR_INPUT

    def arm_clear(self, now: float) -> None:
        """Start the double-press window. Only a clear arms it."""
        self.last_ctrl_c = now

    def decide_ctrl_d(self, view: InputView) -> InputAction:
        if view.buffer_empty:
            return InputAction.REQUEST_EXIT
        return InputAction.DELETE_AFTER_CURSOR

    def decide_exit_command(self) -> InputAction:
        return InputAction.REQUEST_EXIT
