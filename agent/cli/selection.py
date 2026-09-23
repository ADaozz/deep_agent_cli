"""Mouse-selectable formatted text control used by the transcript."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.utils import explode_text_fragments
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType


class SelectableFormattedTextControl(FormattedTextControl):
    """Formatted text with pi-like drag selection and copy-on-release."""

    def __init__(
        self,
        text: Callable[[], Any],
        *,
        on_copy: Callable[[str], None],
        on_selection_start: Callable[[], None] | None = None,
        get_cursor_position: Callable[[], Point | None] | None = None,
    ) -> None:
        self._source = text
        self._on_copy = on_copy
        self._on_selection_start = on_selection_start
        self._anchor: Point | None = None
        self._focus: Point | None = None
        self._dragging = False
        self._plain_lines: list[str] = []
        super().__init__(
            text=self._styled_text,
            focusable=False,
            show_cursor=False,
            get_cursor_position=get_cursor_position,
        )

    @property
    def has_selection(self) -> bool:
        return self._anchor is not None and self._focus is not None and self._anchor != self._focus

    def clear_selection(self) -> None:
        self._anchor = None
        self._focus = None
        self._dragging = False

    def selected_text(self) -> str:
        if not self.has_selection:
            return ""
        assert self._anchor is not None and self._focus is not None
        start, end = self._selection_bounds()
        if not self._plain_lines:
            return ""
        start = self._clamp(start)
        end = self._clamp(end)
        if start.y == end.y:
            return self._plain_lines[start.y][start.x:end.x].rstrip()
        selected = [self._plain_lines[start.y][start.x:].rstrip()]
        selected.extend(line.rstrip() for line in self._plain_lines[start.y + 1:end.y])
        selected.append(self._plain_lines[end.y][:end.x].rstrip())
        return "\n".join(selected).strip("\n")

    def mouse_handler(self, mouse_event: MouseEvent):  # type: ignore[no-untyped-def]
        if mouse_event.button is not MouseButton.LEFT:
            return NotImplemented
        point = mouse_event.position
        if mouse_event.event_type is MouseEventType.MOUSE_DOWN:
            self._anchor = point
            self._focus = point
            self._dragging = True
            if self._on_selection_start is not None:
                self._on_selection_start()
            return None
        if mouse_event.event_type is MouseEventType.MOUSE_MOVE and self._dragging:
            self._focus = point
            return None
        if mouse_event.event_type is MouseEventType.MOUSE_UP and self._dragging:
            self._focus = point
            self._dragging = False
            selected = self.selected_text()
            if selected:
                self._on_copy(selected)
            return None
        return NotImplemented

    def _styled_text(self) -> StyleAndTextTuples:
        fragments = list(to_formatted_text(self._source()))
        lines = [list(line) for line in split_lines(fragments)]
        self._plain_lines = [
            "".join(text for style, text, *_ in line if "[ZeroWidthEscape]" not in style)
            for line in lines
        ] or [""]
        if not self.has_selection:
            return fragments

        assert self._anchor is not None and self._focus is not None
        start, end = self._selection_bounds()
        styled: StyleAndTextTuples = []
        for row, line in enumerate(lines):
            column = 0
            for fragment in explode_text_fragments(line):
                style, text, *handler = fragment
                selected = _contains(row, column, start, end)
                applied = f"{style} class:selection" if selected else style
                styled.append((applied, text, *handler))
                column += len(text)
            if row + 1 < len(lines):
                styled.append(("", "\n"))
        return styled

    def _clamp(self, point: Point) -> Point:
        row = max(0, min(point.y, len(self._plain_lines) - 1))
        column = max(0, min(point.x, len(self._plain_lines[row])))
        return Point(x=column, y=row)

    def _selection_bounds(self) -> tuple[Point, Point]:
        assert self._anchor is not None and self._focus is not None
        start, end = sorted((self._anchor, self._focus), key=lambda point: (point.y, point.x))
        return start, Point(x=end.x + 1, y=end.y)


def _contains(row: int, column: int, start: Point, end: Point) -> bool:
    position = (row, column)
    return (start.y, start.x) <= position < (end.y, end.x)
