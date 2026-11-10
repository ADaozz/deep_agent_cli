"""Terminal display preferences."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


DEFAULT_UI_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class UiDisplayLimits:
    """Content folding limits, loaded from the main configuration's ui section."""

    head_lines: int = 4
    tail_lines: int = 4
    explore_preview_items: int = 5
    explore_failure_items: int = 3
    create_preview_items: int = 5
    editor_max_lines: int = 10
    completion_menu_lines: int = 8

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"ui.{name} must be a positive integer")

    @classmethod
    def from_mapping(cls, ui: Mapping[str, Any]) -> "UiDisplayLimits":
        removed = (
            "thinking_tail_lines", "execute_tail_lines", "tool_tail_lines", "expanded_tool_lines",
            "edit_preview_changed_lines", "write_preview_lines", "failure_preview_lines",
            "command_failure_tail_lines",
        )
        obsolete = [name for name in removed if name in ui]
        if obsolete:
            raise ValueError(f"Removed ui settings: {', '.join(obsolete)}; use ui.head_lines and ui.tail_lines")
        return cls(**{name: ui[name] for name in cls.__dataclass_fields__ if name in ui})


DEFAULT_UI_DISPLAY_LIMITS = UiDisplayLimits()
