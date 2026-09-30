"""Keep long pasted input compact in the editor without changing submitted text."""
from __future__ import annotations

import re


PASTED_CONTENT_MIN_CHARS = 500
_LABEL = re.compile(r"\[Pasted Content \d+ chars(?: #\d+)?\]")
_OPEN = re.compile(r"\[Pasted Content")


class PastedContentDraft:
    def __init__(self) -> None:
        self._blocks: dict[str, str] = {}

    @property
    def has_blocks(self) -> bool:
        return bool(self._blocks)

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(self._blocks)

    def display(self, content: str) -> str:
        if len(content) < PASTED_CONTENT_MIN_CHARS:
            return content
        base = f"[Pasted Content {len(content)} chars"
        label = f"{base}]"
        number = 2
        while label in self._blocks:
            label = f"{base} #{number}]"
            number += 1
        self._blocks[label] = content
        return label

    def expand(self, text: str) -> str:
        return _LABEL.sub(lambda match: self._blocks.get(match.group(), match.group()), text)

    def has_invalid_marker(self, text: str) -> bool:
        """Catch edited labels before a truncated marker reaches the agent."""
        if not self._blocks:
            return False
        for match in _LABEL.finditer(text):
            if match.group() not in self._blocks:
                return True
        for match in _OPEN.finditer(text):
            if not any(text.startswith(label, match.start()) for label in self._blocks):
                return True
        return False

    def clear(self) -> None:
        self._blocks.clear()
