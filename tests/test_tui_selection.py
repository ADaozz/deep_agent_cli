from __future__ import annotations

from agent.cli import clipboard


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
