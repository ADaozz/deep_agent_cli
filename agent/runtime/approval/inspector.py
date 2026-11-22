"""Host-side read-only inspection for approval evidence (no shell, no network)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class ReviewInspector:
    """Optional read-only probes that may change Allow/Ask/Deny conclusions."""

    def __init__(self, *, workspace: Path | str | None = None, protected: tuple[Path, ...] = ()):
        self.workspace = Path(workspace).resolve() if workspace else None
        self.protected = tuple(Path(p).expanduser().resolve() for p in protected)

    def _blocked(self, path: Path) -> bool:
        resolved = path.expanduser()
        try:
            host = resolved.resolve(strict=False)
        except OSError:
            return True
        for root in self.protected:
            try:
                if host == root or root in host.parents or host in root.parents:
                    return True
            except OSError:
                return True
        # Never inspect arbitrary secret-looking files for content.
        name = host.name.lower()
        if name in {".env", ".netrc", ".npmrc", ".pypirc"} or name.endswith((".pem", ".key")):
            return True
        return False

    def inspect_path(self, path: object) -> dict[str, Any] | None:
        if not isinstance(path, str) or not path.strip():
            return None
        target = Path(path)
        if not target.is_absolute() and self.workspace is not None:
            target = self.workspace / target
        if self._blocked(target):
            return {"path": path, "error": "protected_or_secret"}
        try:
            exists = target.exists()
            is_symlink = target.is_symlink()
            resolved = str(target.resolve(strict=False))
            info: dict[str, Any] = {
                "path": path,
                "exists": exists,
                "is_symlink": is_symlink,
                "resolved": resolved,
            }
            if not exists:
                return info
            if target.is_dir():
                try:
                    entries = list(os.scandir(target))
                except OSError as exc:
                    return {**info, "error": f"scandir_failed:{exc.__class__.__name__}"}
                info["is_dir"] = True
                info["empty"] = len(entries) == 0
                info["entry_count"] = len(entries)
            elif target.is_file():
                info["is_file"] = True
                try:
                    info["size"] = target.stat().st_size
                except OSError:
                    info["size"] = None
            return info
        except OSError as exc:
            return {"path": path, "error": exc.__class__.__name__}

    def git_workspace_summary(self) -> dict[str, Any] | None:
        """Best-effort read of .git HEAD only; never runs git commands."""
        if self.workspace is None:
            return None
        git = self.workspace / ".git"
        if self._blocked(git) or not git.exists():
            return None
        head = git / "HEAD"
        if not head.is_file():
            return {"git": True, "head": None}
        try:
            text = head.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return {"git": True, "error": "head_unreadable"}
        if text.startswith("ref:"):
            return {"git": True, "head_ref": text.split(" ", 1)[-1].strip()}
        return {"git": True, "head_sha": text[:40]}

    def inspect_call(self, call: dict[str, Any]) -> dict[str, Any] | None:
        """Inspect only when path existence/emptiness can change risk conclusions."""
        name = call.get("name")
        args = call.get("args") or {}
        if name == "delete":
            return self.inspect_path(args.get("file_path") or args.get("path"))
        if name == "execute":
            command = str(args.get("command") or "")
            # Lightweight: only when rm targets a single path-like token.
            if "rm " not in command and "rm\t" not in command:
                return None
            parts = command.replace("\t", " ").split()
            paths = [p for p in parts[1:] if p and not p.startswith("-")]
            if len(paths) != 1:
                return None
            info = self.inspect_path(paths[0])
            git = self.git_workspace_summary()
            if info is None and git is None:
                return None
            result = {}
            if info:
                result["target"] = info
            if git:
                result["git"] = git
            return result or None
        return None
