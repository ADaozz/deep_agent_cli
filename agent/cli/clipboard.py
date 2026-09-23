"""Small, dependency-free system clipboard adapter for the TUI."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any


class ClipboardError(RuntimeError):
    """Raised when no usable system clipboard transport is available."""


@dataclass(frozen=True)
class ClipboardText:
    text: str


@dataclass(frozen=True)
class ClipboardImage:
    """Clipboard contains a bitmap; export is deliberately deferred."""


@dataclass(frozen=True)
class ClipboardFiles:
    paths: tuple[str, ...]


@dataclass(frozen=True)
class ClipboardUnavailable:
    reason: str


ClipboardContent = ClipboardText | ClipboardImage | ClipboardFiles | ClipboardUnavailable


class ClipboardAdapter:
    """Inspect first, then export an image only after the caller's capability gate."""

    def inspect(self) -> ClipboardContent:
        powershell = _wsl_powershell()
        if powershell is None:
            return ClipboardUnavailable("Image clipboard paste is currently supported on WSL; use /image <path>")
        script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
if ([Windows.Forms.Clipboard]::ContainsImage()) {
  @{kind='image'} | ConvertTo-Json -Compress
} elseif ([Windows.Forms.Clipboard]::ContainsFileDropList()) {
  $items = @([Windows.Forms.Clipboard]::GetFileDropList() | ForEach-Object { [string]$_ })
  @{kind='files'; paths=$items} | ConvertTo-Json -Compress
} elseif ([Windows.Forms.Clipboard]::ContainsText()) {
  @{kind='text'; text=[Windows.Forms.Clipboard]::GetText()} | ConvertTo-Json -Compress
} else {
  @{kind='unavailable'; reason='Clipboard has no text, image, or files'} | ConvertTo-Json -Compress
}
"""
        try:
            raw = _run_capture([powershell, "-NoProfile", "-STA", "-Command", script], timeout=5)
            payload = json.loads(raw)
        except (ClipboardError, json.JSONDecodeError) as exc:
            return ClipboardUnavailable(str(exc))
        kind = payload.get("kind")
        if kind == "image":
            return ClipboardImage()
        if kind == "files":
            paths = payload.get("paths") or []
            if isinstance(paths, str):
                paths = [paths]
            return ClipboardFiles(tuple(str(path) for path in paths))
        if kind == "text":
            return ClipboardText(str(payload.get("text") or ""))
        return ClipboardUnavailable(str(payload.get("reason") or "Clipboard content is unavailable"))

    def export_image(self) -> Path:
        powershell = _wsl_powershell()
        if powershell is None:
            raise ClipboardError("WSL PowerShell clipboard is unavailable")
        fd, name = tempfile.mkstemp(prefix="deep-agent-clipboard-", suffix=".png")
        os.close(fd)
        path = Path(name)
        path.unlink(missing_ok=True)
        windows_path = _wslpath(path, to_windows=True)
        escaped = windows_path.replace("'", "''")
        script = rf"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$image = [Windows.Forms.Clipboard]::GetImage()
if ($null -eq $image) {{ throw 'Clipboard image is no longer available' }}
try {{ $image.Save('{escaped}', [Drawing.Imaging.ImageFormat]::Png) }} finally {{ $image.Dispose() }}
"""
        try:
            _run_capture([powershell, "-NoProfile", "-STA", "-Command", script], timeout=10)
            if not path.is_file():
                raise ClipboardError("PowerShell did not export the clipboard image")
            return path
        except Exception:
            path.unlink(missing_ok=True)
            raise


def copy_to_clipboard(text: str, *, output: Any | None = None) -> str:
    """Copy text and return the backend name used.

    WSL's ``clip.exe`` expects UTF-16LE on stdin. Other native helpers accept
    UTF-8. OSC 52 is the final fallback for terminals without a local helper.
    """
    if not text:
        raise ClipboardError("Nothing selected")

    clip = _wsl_clipboard()
    if clip is not None:
        _run([clip], text.encode("utf-16le"))
        return "clip.exe"

    candidates = (
        (["pbcopy"], "pbcopy"),
        (["wl-copy"], "wl-copy"),
        (["xclip", "-selection", "clipboard"], "xclip"),
        (["xsel", "--clipboard", "--input"], "xsel"),
    )
    for command, name in candidates:
        executable = shutil.which(command[0])
        if executable is None:
            continue
        _run([executable, *command[1:]], text.encode("utf-8"))
        return name

    if output is not None and hasattr(output, "write_raw"):
        try:
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            output.write_raw(f"\x1b]52;c;{encoded}\x07")
            output.flush()
            return "OSC 52"
        except (OSError, ValueError) as exc:
            raise ClipboardError(f"OSC 52 clipboard failed: {exc}") from exc

    raise ClipboardError("No system clipboard helper is available")


def _wsl_clipboard() -> str | None:
    if not _is_wsl():
        return None
    executable = shutil.which("clip.exe")
    if executable:
        return executable
    fallback = Path("/mnt/c/Windows/System32/clip.exe")
    return str(fallback) if fallback.is_file() else None


def _wsl_powershell() -> str | None:
    if not _is_wsl():
        return None
    executable = shutil.which("powershell.exe")
    if executable:
        return executable
    fallback = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    return str(fallback) if fallback.is_file() else None


def windows_path_to_wsl(value: str) -> Path:
    if not _is_wsl() or not re_windows_path(value):
        return Path(value).expanduser()
    return Path(_wslpath(Path(value), to_windows=False))


def re_windows_path(value: str) -> bool:
    return len(value) >= 3 and value[1] == ":" and value[2] in {"\\", "/"}


def _wslpath(path: Path, *, to_windows: bool) -> str:
    command = ["wslpath", "-w" if to_windows else "-u", str(path)]
    return _run_capture(command, timeout=3).strip()


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def _run(command: list[str], payload: bytes) -> None:
    try:
        subprocess.run(
            command,
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", b"")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise ClipboardError(f"Clipboard command failed{suffix}") from exc


def _run_capture(command: list[str], *, timeout: int) -> str:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", b"")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace").strip()
        raise ClipboardError(f"Clipboard command failed{f': {detail}' if detail else ''}") from exc
    return result.stdout.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
