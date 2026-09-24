from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_BINDINGS: dict[str, list[str]] = {
    "submit": ["enter"],
    "newline": ["c-j"],
    "follow_up": ["alt+enter"],
    "interrupt": ["escape"],
    "clear_or_exit": ["c-c"],
    "exit": ["c-d"],
    "tools_expand": ["c-o"],
    "review_diff": ["c-r"],
    "reopen_interaction": ["f2"],
    "thinking_toggle": ["c-t"],
    "dequeue": ["alt+up"],
    # Windows Terminal may reserve Ctrl+V for terminal paste. Alt+V is the
    # reliable application-level image clipboard fallback.
    "image_paste": ["ctrl+v", "alt+v"],
    # prompt_toolkit has no ControlShift+letter; Alt+P is the backward cycle fallback.
    "model_cycle_forward": ["ctrl+p"],
    "model_cycle_backward": ["alt+p"],
}


@dataclass
class Keymap:
    bindings: dict[str, list[str]] = field(default_factory=lambda: {
        name: list(keys) for name, keys in DEFAULT_BINDINGS.items()
    })
    warning: str = ""

    @classmethod
    def load(cls, path: Path | None) -> "Keymap":
        result = cls()
        if path is None or not path.is_file():
            return result
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for action, value in raw.items():
                if action not in result.bindings:
                    continue
                keys = [value] if isinstance(value, str) else value
                if isinstance(keys, list) and keys and all(isinstance(item, str) for item in keys):
                    result.bindings[action] = keys
        except (OSError, ValueError, TypeError) as exc:
            result.warning = f"Could not load keybindings: {exc}"
        return result

    def sequences(self, action: str) -> list[tuple[str, ...]]:
        return [_prompt_toolkit_keys(value) for value in self.bindings[action]]


def _prompt_toolkit_keys(value: str) -> tuple[str, ...]:
    keys = value.strip().lower()
    if keys.startswith("alt+"):
        return ("escape", keys.removeprefix("alt+"))
    if keys.startswith("ctrl+"):
        return (f"c-{keys.removeprefix('ctrl+')}",)
    if keys == "shift+tab":
        return ("s-tab",)
    return tuple(part for part in keys.split() if part)
