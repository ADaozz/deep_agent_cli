"""Terminal controls used to assemble the CLI view."""

from __future__ import annotations

from typing import Any

from prompt_toolkit import ANSI
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.layout import BufferControl, Window
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput
from prompt_toolkit.mouse_events import MouseButton, MouseEventType

from agent.cli.pasted_content import PastedContentDraft
from agent.cli.skills import SkillDraft
from agent.cli.rendering import render_interaction_viewport


class _ScrollableTextControl(FormattedTextControl):
    """Formatted text that consumes clicks and routes wheel events to the transcript."""

    def __init__(self, *args: Any, on_scroll: Any = None, on_select_outside: Any = None, **kwargs: Any) -> None:
        self._on_scroll = on_scroll
        self._on_select_outside = on_select_outside
        super().__init__(*args, **kwargs)

    def mouse_handler(self, mouse_event):  # type: ignore[no-untyped-def]
        if self._on_select_outside is not None and self._on_select_outside(mouse_event):
            return None
        if self._on_scroll is not None:
            if mouse_event.event_type is MouseEventType.SCROLL_UP:
                self._on_scroll(-3)
                return None
            if mouse_event.event_type is MouseEventType.SCROLL_DOWN:
                self._on_scroll(3)
                return None
        return None


class _InteractionControl(_ScrollableTextControl):
    """A fixed heading and footer around a selection-following option list."""

    def __init__(self, *args: Any, controller: Any, **kwargs: Any) -> None:
        self._controller = controller
        self._option_offset = 0
        self._selection_context: Any = None
        super().__init__(*args, **kwargs)

    def create_content(self, width: int, height: int | None = None) -> UIContent:
        controller = self._controller()
        context = (controller, controller.index if controller else None, width)
        if context != self._selection_context:
            self._option_offset = 0
            self._selection_context = context
        rendered, self._option_offset = render_interaction_viewport(
            controller, width, height or 1, self._option_offset,
        )
        self.text = to_formatted_text(ANSI(rendered))
        return super().create_content(width, height)


class _BackToBottomControl(_ScrollableTextControl):
    """Make the whole return-to-bottom hint clickable without stealing wheel events."""

    def __init__(self, *args: Any, on_click: Any, **kwargs: Any) -> None:
        self._on_click = on_click
        super().__init__(*args, **kwargs)

    def mouse_handler(self, mouse_event):  # type: ignore[no-untyped-def]
        if mouse_event.event_type is MouseEventType.MOUSE_DOWN and mouse_event.button is MouseButton.LEFT:
            self._on_click()
            return None
        return super().mouse_handler(mouse_event)


class _TranscriptControl(UIControl):
    """Expose cached transcript lines to prompt_toolkit without splitting them again."""

    def __init__(self, document: Any, cursor: Any, on_scroll: Any, on_select: Any, selected_line: Any) -> None:
        self._document = document
        self._cursor = cursor
        self._on_scroll = on_scroll
        self._on_select = on_select
        self._selected_line = selected_line

    def create_content(self, width: int, height: int) -> UIContent:
        document = self._document()
        return UIContent(
            get_line=lambda row: self._selected_line(row, document.get_line(row)),
            line_count=document.line_count,
            cursor_position=self._cursor(),
            show_cursor=False,
        )

    def preferred_height(self, width: int, max_available_height: int, wrap_lines: bool, get_line_prefix: Any) -> int:
        return min(max_available_height, self._document().line_count)

    def mouse_handler(self, mouse_event):  # type: ignore[no-untyped-def]
        if mouse_event.event_type is MouseEventType.SCROLL_UP:
            self._on_scroll(-3)
        elif mouse_event.event_type is MouseEventType.SCROLL_DOWN:
            self._on_scroll(3)
        elif mouse_event.event_type in {MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_MOVE, MouseEventType.MOUSE_UP}:
            self._on_select(mouse_event)
        return None


class _EditorScrollControl(BufferControl):
    """Input box: wheel scrolls the transcript instead of the empty editor."""

    def __init__(self, *args: Any, on_scroll: Any = None, on_select_outside: Any = None, **kwargs: Any) -> None:
        self._on_scroll = on_scroll
        self._on_select_outside = on_select_outside
        super().__init__(*args, **kwargs)

    def mouse_handler(self, mouse_event):  # type: ignore[no-untyped-def]
        if self._on_select_outside is not None and self._on_select_outside(mouse_event):
            return None
        if self._on_scroll is not None and mouse_event.event_type in {
            MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN,
        }:
            self._on_scroll(-3 if mouse_event.event_type is MouseEventType.SCROLL_UP else 3)
            return None
        return super().mouse_handler(mouse_event)


class _PastedContentProcessor(Processor):
    """为输入块标记添加样式，保持编辑文本和光标位置不变。"""

    def __init__(self, draft: PastedContentDraft | SkillDraft, style: str = "pasted-content") -> None:
        self._draft = draft
        self._style = style

    def apply_transformation(self, transformation_input: TransformationInput) -> Transformation:
        fragments = transformation_input.fragments
        plain = "".join(fragment[1] for fragment in fragments)
        ranges = [
            (index, index + len(label))
            for label in self._draft.labels
            if (index := plain.find(label)) >= 0
        ]
        if not ranges:
            return Transformation(fragments)
        styled = []
        index = 0
        for fragment in fragments:
            style, text, *rest = fragment
            for char in text:
                char_style = style + " class:" + self._style if any(
                    start <= index < end for start, end in ranges
                ) else style
                styled.append((char_style, char, *rest))
                index += 1
        return Transformation(styled)


class _TranscriptWindow(Window):
    """Transcript viewport whose scroll offset is authoritative, not cursor-derived.

    prompt_toolkit's scrollers only ever move ``vertical_scroll`` far enough to keep
    a cursor visible. Disguising the scroll anchor as a cursor therefore left the
    viewport stuck: ``max(previous_scroll, ...)`` blocked downward movement and
    ``min(..., get_max_vertical_scroll())`` blocked upward movement, so the
    viewport barely responded in either direction. This window computes the offset
    outright and skips the cursor-chasing arithmetic entirely.
    """

    def __init__(self, *args: Any, viewport: Any = None, scroll_offset: Any = None, **kwargs: Any) -> None:
        self._viewport = viewport
        self._scroll_offset = scroll_offset
        super().__init__(*args, **kwargs)

    def _scroll(self, ui_content: Any, width: int, height: int) -> None:
        self.horizontal_scroll = 0
        self.vertical_scroll_2 = 0
        maximum = max(0, ui_content.line_count - height)
        if self._scroll_offset is not None:
            self.vertical_scroll = self._scroll_offset(maximum)
        else:
            self.vertical_scroll = self._viewport.transcript_offset(maximum) if self._viewport is not None else maximum
