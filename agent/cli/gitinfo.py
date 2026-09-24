"""Workspace git summary for the status footer.

The footer repaints several times a second, so git is probed on a timer in the
background and the UI only ever reads the last known summary.
"""
from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

REFRESH_SECONDS = 5.0
TIMEOUT_SECONDS = 2.0

_NO_BRANCH_PREFIX = "No commits yet on "


@dataclass(frozen=True)
class GitSummary:
    """Branch name plus changed-path count; empty when this is not a repository."""

    branch: str = ""
    changed: int = 0
    available: bool = False

    def label(self) -> str:
        if not self.available:
            return ""
        state = f"{self.changed} changed" if self.changed else "clean"
        return f"⎇ {self.branch} · {state}"


def parse_status(output: str) -> GitSummary:
    """Parse `git status --porcelain --branch` output."""
    lines = output.splitlines()
    branch = ""
    if lines and lines[0].startswith("## "):
        head = lines[0][3:].strip()
        lines = lines[1:]
        if head.startswith(_NO_BRANCH_PREFIX):
            branch = head.removeprefix(_NO_BRANCH_PREFIX).strip()
        elif "no branch" in head or head.startswith("HEAD"):
            branch = "detached"
        else:
            branch = head.split("...", 1)[0].strip()
    return GitSummary(
        branch=branch or "unknown",
        changed=sum(1 for line in lines if line.strip()),
        available=True,
    )


class GitProbe:
    """Background `git status` whose last result is safe to read from a render pass."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.summary = GitSummary()

    async def refresh(self) -> GitSummary:
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain", "--branch",
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT_SECONDS)
            self.summary = (
                parse_status(stdout.decode("utf-8", "replace"))
                if process.returncode == 0 else GitSummary()
            )
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
            self.summary = GitSummary()
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        return self.summary
