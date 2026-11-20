"""Shared cell-based text selection for terminal views."""

from collections.abc import Callable, Iterator
from typing import Any

from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.utils import get_cwidth


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


class TextSelection:
    def __init__(self) -> None:
        self.selection_start: tuple[int, int] | None = None
        self.selection_end: tuple[int, int] | None = None
        self.selection_dragging = False

    def clear_selection(self) -> None:
        self.selection_start = self.selection_end = None
        self.selection_dragging = False

    def update_pointer(
        self, position: tuple[int, int], event_type: MouseEventType, button: MouseButton,
    ) -> bool:
        if event_type is MouseEventType.MOUSE_DOWN and button is MouseButton.LEFT:
            self.selection_start = self.selection_end = position
            self.selection_dragging = True
        elif event_type is MouseEventType.MOUSE_MOVE and self.selection_dragging:
            # Plain motion can follow a release that happened outside the view.
            if button is MouseButton.NONE:
                self.selection_dragging = False
            else:
                self.selection_end = position
        elif event_type is MouseEventType.MOUSE_UP and self.selection_dragging:
            self.selection_end = position
            self.selection_dragging = False
        else:
            return False
        return True

    def selection_bounds(self) -> tuple[tuple[int, int], tuple[int, int]] | None:
        if self.selection_start is None or self.selection_end is None:
            return None
        start, end = sorted((self.selection_start, self.selection_end))
        return (start, end) if start != end else None

    def selection_span(self, row: int, bounds: tuple[tuple[int, int], tuple[int, int]]) -> tuple[float, float]:
        # The cell under the pointer is part of the selection: clicks right of a
        # line's text resolve to its last character, which must still be copied.
        left = bounds[0][1] if row == bounds[0][0] else 0
        right = bounds[1][1] + 1 if row == bounds[1][0] else float("inf")
        return left, right

    def highlight_line(self, row: int, fragments: Any) -> Any:
        bounds = self.selection_bounds()
        if bounds is None or not bounds[0][0] <= row <= bounds[1][0]:
            return fragments
        left, right = self.selection_span(row, bounds)
        return [
            (f"{style} class:transcript-selection" if marked else style, char, *rest)
            for (style, _text, *rest), char, marked in _iter_selection_marks(fragments, left, right)
        ]

    def selected_text(self, get_line: Callable[[int], Any], line_count: int) -> str:
        bounds = self.selection_bounds()
        if bounds is None:
            return ""
        lines = []
        for row in range(bounds[0][0], min(bounds[1][0], line_count - 1) + 1):
            left, right = self.selection_span(row, bounds)
            chars = [char for _, char, marked in _iter_selection_marks(get_line(row), left, right) if marked]
            lines.append("".join(chars).rstrip())
        return "\n".join(lines).strip("\n")
