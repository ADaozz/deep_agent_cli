"""Transcript viewport navigation and selection behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable
import asyncio
from typing import Any

from prompt_toolkit.data_structures import Point
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.utils import get_cwidth

from agent.cli.text_selection import TextSelection


if TYPE_CHECKING:
    from prompt_toolkit.output import Output
    from prompt_toolkit.layout import Window


class TranscriptNavigation(TextSelection):
    def __init__(
        self, *,
        _copy_selection: Callable[..., Any],
        _transcript_document: Callable[..., Any],
        transcript_window: Callable[[], Window],
        invalidate: Callable[[], None],
        output: Callable[[], Output],
    ) -> None:
        super().__init__()
        self._copy_selection = _copy_selection
        self._transcript_document = _transcript_document
        self.transcript_window = transcript_window
        self.invalidate = invalidate
        self.output = output
        self.jump_message_id: str | None = None
        self.transcript_line_count = 1
        self.transcript_anchor: int | None = None
        self.selection_edge = 0
        self.selection_edge_x = 0
        self.selection_scroll_step = 0
        self.selection_scroll_handle: asyncio.TimerHandle | None = None


    def _transcript_cursor(self) -> Point:
        last = max(0, self.transcript_line_count - 1)
        return Point(x=0, y=min(last, self.transcript_top()))

    def transcript_rows(self) -> int:
        """Total transcript rows as of the most recent paint."""
        return self.transcript_line_count

    def transcript_viewport_rows(self) -> int:
        """Visible transcript rows; from the viewport once it has painted."""
        info = self.transcript_window().render_info
        if info is not None:
            return max(1, info.window_height)
        try:
            rows = self.output().get_size().rows
        except Exception:  # noqa: BLE001
            rows = 24
        return max(1, rows)

    def transcript_max_scroll(self) -> int:
        return max(0, self.transcript_rows() - self.transcript_viewport_rows())

    def transcript_top(self) -> int:
        """Row currently sitting at the top of the viewport."""
        maximum = self.transcript_max_scroll()
        if self.transcript_anchor is None:
            return maximum
        return min(maximum, max(0, self.transcript_anchor))

    def transcript_offset(self, maximum: int) -> int:
        """Resolve a painted viewport's offset and restore following at its tail."""
        anchor = self.transcript_anchor
        if anchor is None or (anchor >= maximum and self.jump_message_id is None):
            self.transcript_anchor = None
            return maximum
        return min(maximum, max(0, anchor))

    def transcript_away_from_bottom(self) -> bool:
        """Only offer the return action while the transcript tail is off screen."""
        return self.transcript_anchor is not None and self.transcript_top() < self.transcript_max_scroll()

    def jump_turn(self, delta: int) -> None:
        document = self._transcript_document()
        turns = document.turns
        current = document.turn_at(self.transcript_top())
        if not turns:
            return
        if self.transcript_anchor is None:
            if current < 0 or self.transcript_top() < turns[current].end:
                current = len(turns) - 1
            if self.jump_message_id != turns[-1].message_id:
                self.jump_message_id = None
        if self.jump_message_id is not None:
            current = next((i for i, turn in enumerate(turns) if turn.message_id == self.jump_message_id), current)
        target = current + delta
        if 0 <= target < len(turns):
            self.scroll_transcript_to(turns[target].start)
            self.jump_message_id = turns[target].message_id
            if target < len(turns) - 1:
                self.transcript_anchor = self.transcript_top()

    def follow_transcript(self) -> None:
        self.jump_message_id = None
        self.transcript_anchor = None
        self.invalidate()

    def scroll_transcript(self, delta: int) -> None:
        self.scroll_transcript_to(self.transcript_top() + delta)

    def scroll_transcript_to(self, row: int) -> None:
        maximum = self.transcript_max_scroll()
        self.jump_message_id = None
        nxt = min(maximum, max(0, row))
        self.transcript_anchor = None if nxt >= maximum else nxt
        self.invalidate()

    def _select_transcript(self, event: Any) -> None:
        # prompt_toolkit's Window already maps the click to (document row,
        # character index); only the index still needs converting to cells.
        row = event.position.y
        self._update_selection(row, self._selection_column(row, event.position.x), event.event_type, event.button)

    def _selection_column(self, row: int, index: int) -> int:
        document = self._transcript_document()
        if not 0 <= row < document.line_count:
            return index
        column = 0
        remaining = index
        for fragment in document.get_line(row):
            if "[ZeroWidthEscape]" in fragment[0]:
                continue
            for char in fragment[1]:
                if remaining <= 0:
                    return column
                column += max(0, get_cwidth(char))
                remaining -= 1
        return column

    def _update_selection(self, row: int, column: int, event_type: Any, button: Any) -> None:
        if not self.update_pointer((row, column), event_type, button):
            return
        if event_type is MouseEventType.MOUSE_DOWN:
            self._stop_selection_scroll()
            # Reinforce mouse reporting so IDE terminals drop their native
            # selection overlay instead of stacking it on ours.
            try:
                self.output().enable_mouse_support()
            except Exception:  # noqa: BLE001
                pass
        elif event_type is MouseEventType.MOUSE_MOVE:
            if self.selection_dragging:
                top = self.transcript_top()
                bottom = top + self.transcript_viewport_rows() - 1
                edge = -1 if row <= top else (1 if row >= bottom else 0)
                self.selection_edge_x = column
                self._set_selection_edge(edge)
            else:
                self._stop_selection_scroll()
        elif event_type is MouseEventType.MOUSE_UP:
            self._stop_selection_scroll()
            selected = self._selected_transcript_text()
            if selected:
                self._copy_selection(selected)
        self.invalidate()

    def _select_below_transcript(self, event: Any) -> bool:
        if not self.selection_dragging or event.event_type not in {
            MouseEventType.MOUSE_MOVE, MouseEventType.MOUSE_UP,
        }:
            return False
        bottom = self.transcript_top() + self.transcript_viewport_rows() - 1
        self._update_selection(bottom, event.position.x, event.event_type, event.button)
        return True

    def _set_selection_edge(self, edge: int) -> None:
        if edge != self.selection_edge:
            self.selection_scroll_step = 0
        self.selection_edge = edge
        if edge == 0:
            self._stop_selection_scroll()
        elif self.selection_scroll_handle is None:
            # Scroll immediately, then keep accelerating while the pointer stays
            # pinned to the viewport edge.
            self._scroll_selection_edge()

    def _scroll_selection_edge(self) -> None:
        self.selection_scroll_handle = None
        if not self.selection_dragging or not self.selection_edge:
            return
        previous = self.transcript_top()
        self.selection_scroll_step = min(12, max(3, self.selection_scroll_step + 1))
        self.scroll_transcript(self.selection_edge * self.selection_scroll_step)
        current = self.transcript_top()
        if current == previous:
            return
        row = current if self.selection_edge < 0 else current + self.transcript_viewport_rows() - 1
        self.selection_end = (row, self.selection_edge_x)
        self.selection_scroll_handle = asyncio.get_running_loop().call_later(0.04, self._scroll_selection_edge)

    def _stop_selection_scroll(self) -> None:
        self.selection_edge = 0
        self.selection_scroll_step = 0
        if self.selection_scroll_handle is not None:
            self.selection_scroll_handle.cancel()
            self.selection_scroll_handle = None

    def clear_transcript_selection(self) -> None:
        self._stop_selection_scroll()
        self.clear_selection()
        self.invalidate()

    def _selected_transcript_line(self, row: int, fragments: Any) -> Any:
        return self.highlight_line(row, fragments)

    def _selected_transcript_text(self) -> str:
        if self.selection_bounds() is None:
            return ""
        document = self._transcript_document()
        return self.selected_text(document.get_line, document.line_count)
