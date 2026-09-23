from __future__ import annotations

from prompt_toolkit import ANSI
from prompt_toolkit.data_structures import Point
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

from agent.cli import clipboard
from agent.cli.selection import SelectableFormattedTextControl


def _mouse(event_type: MouseEventType, x: int, y: int) -> MouseEvent:
    return MouseEvent(
        position=Point(x=x, y=y),
        event_type=event_type,
        button=MouseButton.LEFT,
        modifiers=frozenset(),
    )


def test_selectable_control_copies_multiline_unicode_and_trims_render_padding() -> None:
    copied: list[str] = []
    control = SelectableFormattedTextControl(
        lambda: ANSI("\x1b[36m你好 world   \x1b[0m\n  code()   "),
        on_copy=copied.append,
    )
    control._styled_text()  # Prime the rendered plain-text lines.
    control.mouse_handler(_mouse(MouseEventType.MOUSE_DOWN, 1, 0))
    control.mouse_handler(_mouse(MouseEventType.MOUSE_MOVE, 7, 1))
    control.mouse_handler(_mouse(MouseEventType.MOUSE_UP, 7, 1))
    assert copied == ["好 world\n  code()"]
    assert control.has_selection
    assert "class:selection" in " ".join(style for style, _text, *_ in control._styled_text())


def test_selectable_control_supports_reverse_selection_and_ignores_click() -> None:
    copied: list[str] = []
    control = SelectableFormattedTextControl(lambda: "abcdef", on_copy=copied.append)
    control._styled_text()
    control.mouse_handler(_mouse(MouseEventType.MOUSE_DOWN, 4, 0))
    control.mouse_handler(_mouse(MouseEventType.MOUSE_UP, 1, 0))
    assert copied == ["bcde"]
    control.mouse_handler(_mouse(MouseEventType.MOUSE_DOWN, 2, 0))
    control.mouse_handler(_mouse(MouseEventType.MOUSE_UP, 2, 0))
    assert copied == ["bcde"]


def test_wsl_clipboard_uses_utf16le(monkeypatch) -> None:
    calls: list[tuple[list[str], bytes]] = []
    monkeypatch.setattr(clipboard, "_wsl_clipboard", lambda: "/mnt/c/Windows/System32/clip.exe")
    monkeypatch.setattr(clipboard, "_run", lambda command, payload: calls.append((command, payload)))
    assert clipboard.copy_to_clipboard("中文") == "clip.exe"
    assert calls == [(["/mnt/c/Windows/System32/clip.exe"], "中文".encode("utf-16le"))]


def test_clipboard_falls_back_to_osc52(monkeypatch) -> None:
    class Output:
        value = ""
        flushed = False

        def write_raw(self, value: str) -> None:
            self.value += value

        def flush(self) -> None:
            self.flushed = True

    monkeypatch.setattr(clipboard, "_wsl_clipboard", lambda: None)
    monkeypatch.setattr(clipboard.shutil, "which", lambda _name: None)
    output = Output()
    assert clipboard.copy_to_clipboard("copy me", output=output) == "OSC 52"
    assert output.value.startswith("\x1b]52;c;")
    assert output.value.endswith("\x07")
    assert output.flushed
