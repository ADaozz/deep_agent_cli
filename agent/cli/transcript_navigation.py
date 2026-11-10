"""Transcript viewport navigation and selection behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable
import asyncio
from collections.abc import Iterator
from typing import Any

from prompt_toolkit.data_structures import Point
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.utils import get_cwidth

from agent.cli.lifecycle import TaskStop


if TYPE_CHECKING:
    from agent.cli.lifecycle import AppLifecycle
    from prompt_toolkit.output import Output
    from prompt_toolkit.layout import Window


def _iter_selection_marks(fragments: Any, left: float, right: float) -> Iterator[tuple[Any, str, bool]]:
    """Yield ``(fragment, char, marked)`` for cells overlapping ``[left, right)``.

    Zero-width characters (combining marks, ZWJ, variation selectors) share the
    cell of the preceding character, so they follow its marked state instead of
    being judged on their own empty span.
    """
    column = 0
    previous = False
    for fragment in fragments:
        if "[ZeroWidthEscape]" in fragment[0]:
            yield fragment, fragment[1], False
            continue
        for char in fragment[1]:
            width = max(0, get_cwidth(char))
            marked = previous if width == 0 else column < right and column + width > left
            yield fragment, char, marked
            previous = marked
            column += width


class TranscriptNavigation:
    def __init__(
        self, *,
        _copy_selection: Callable[..., Any],
        _transcript_document: Callable[..., Any],
        lifecycle: Callable[[], AppLifecycle],
        transcript_window: Callable[[], Window],
        invalidate: Callable[[], None],
        output: Callable[[], Output],
    ) -> None:
        self._copy_selection = _copy_selection
        self._transcript_document = _transcript_document
        self.lifecycle = lifecycle
        self.transcript_window = transcript_window
        self.invalidate = invalidate
        self.output = output
        self.transcript_line_count = 1
        self.transcript_anchor: int | None = None
        self.selection_start: tuple[int, int] | None = None
        self.selection_end: tuple[int, int] | None = None
        self.selection_dragging = False
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
        if anchor is None or anchor >= maximum:
            self.transcript_anchor = None
            return maximum
        return max(0, anchor)

    def transcript_away_from_bottom(self) -> bool:
        """Only offer the return action while the transcript tail is off screen."""
        return self.transcript_anchor is not None and self.transcript_top() < self.transcript_max_scroll()

    def follow_transcript(self) -> None:
        self.transcript_anchor = None
        self.invalidate()

    def scroll_transcript(self, delta: int) -> None:
        self.scroll_transcript_to(self.transcript_top() + delta)

    def scroll_transcript_to(self, row: int) -> None:
        maximum = self.transcript_max_scroll()
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
        position = (row, column)
        if event_type is MouseEventType.MOUSE_DOWN and button is MouseButton.LEFT:
            self._stop_selection_scroll()
            self.selection_start = self.selection_end = position
            self.selection_dragging = True
            # Reinforce mouse reporting so IDE terminals drop their native
            # selection overlay instead of stacking it on ours.
            try:
                self.output().enable_mouse_support()
            except Exception:  # noqa: BLE001
                pass
        elif event_type is MouseEventType.MOUSE_MOVE and self.selection_dragging:
            if button is MouseButton.NONE:
                # The MOUSE_UP never arrived (released outside the terminal window
                # or over a control without a selection handler). Plain motion must
                # not keep dragging, or the 1003 any-motion events spin the UI.
                self.selection_dragging = False
                self._stop_selection_scroll()
            else:
                self.selection_end = position
                top = self.transcript_top()
                bottom = top + self.transcript_viewport_rows() - 1
                edge = -1 if position[0] <= top else (1 if position[0] >= bottom else 0)
                self.selection_edge_x = position[1]
                self._set_selection_edge(edge)
        elif event_type is MouseEventType.MOUSE_UP and self.selection_dragging:
            self.selection_end = position
            self.selection_dragging = False
            self._stop_selection_scroll()
            selected = self._selected_transcript_text()
            if selected:
                self.lifecycle().spawn(self._copy_selection(selected), stop=TaskStop.CANCEL, name="clipboard-copy")
        else:
            return
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
        self.selection_dragging = False
        self.selection_start = self.selection_end = None
        self.invalidate()

    def _selection_bounds(self) -> tuple[tuple[int, int], tuple[int, int]] | None:
        if self.selection_start is None or self.selection_end is None:
            return None
        start, end = sorted((self.selection_start, self.selection_end))
        return (start, end) if start != end else None

    def _selection_span(self, row: int, bounds: tuple[tuple[int, int], tuple[int, int]]) -> tuple[float, float]:
        # The cell under the pointer is part of the selection: clicks right of a
        # line's text resolve to its last character, which must still be copied.
        left = bounds[0][1] if row == bounds[0][0] else 0
        right = bounds[1][1] + 1 if row == bounds[1][0] else float("inf")
        return left, right

    def _selected_transcript_line(self, row: int, fragments: Any) -> Any:
        bounds = self._selection_bounds()
        if bounds is None or not bounds[0][0] <= row <= bounds[1][0]:
            return fragments
        left, right = self._selection_span(row, bounds)
        return [
            (f"{style} class:transcript-selection" if marked else style, char, *rest)
            for (style, _text, *rest), char, marked in _iter_selection_marks(fragments, left, right)
        ]

    def _selected_transcript_text(self) -> str:
        bounds = self._selection_bounds()
        if bounds is None:
            return ""
        document = self._transcript_document()
        lines = []
        for row in range(bounds[0][0], min(bounds[1][0], document.line_count - 1) + 1):
            left, right = self._selection_span(row, bounds)
            chars = [char for _, char, marked in _iter_selection_marks(document.get_line(row), left, right) if marked]
            lines.append("".join(chars).rstrip())
        return "\n".join(lines).strip("\n")
