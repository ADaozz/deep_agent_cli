"""Locate the ripgrep binary the sandbox mounts for execute."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sysconfig


def resolve_rg() -> str | None:
    """Prefer rg installed alongside this Python interpreter, then system PATH."""
    candidate = Path(sysconfig.get_path("scripts")) / "rg"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which("rg")
