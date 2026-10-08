"""Workspace git summary for the status footer.

The footer repaints several times a second, so git is probed on a timer in the
background and the UI only ever reads the last known summary.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

REFRESH_SECONDS = 5.0
TIMEOUT_SECONDS = 10.0

_NO_BRANCH_PREFIX = "No commits yet on "


@dataclass(frozen=True)
class GitSummary:
    """Branch name plus changed-path count; empty when this is not a repository."""

    branch: str = ""
    changed: int = 0
    available: bool = False
    error: str = ""

    def label(self) -> str:
        if self.error:
            detail = f"{self.branch} · {self.error}" if self.available else self.error
            return f"⎇ {detail}"
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
        self.summary = GitSummary(error="checking git")

    async def refresh(self) -> GitSummary:
        process: asyncio.subprocess.Process | None = None
        spawn: asyncio.Task[asyncio.subprocess.Process] | None = None
        try:
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                "git", "status", "--porcelain", "--branch",
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
            ))
            # Cancellation during spawn must not lose ownership of the child.
            process = await asyncio.shield(spawn)
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT_SECONDS)
            if process.returncode == 0:
                self.summary = parse_status(stdout.decode("utf-8", "replace"))
            elif "not a git repository" in stderr.decode("utf-8", "replace").lower():
                self.summary = GitSummary()
            else:
                self.summary = replace(self.summary, error="git error")
        except TimeoutError:
            self.summary = replace(self.summary, error="git timeout")
        except FileNotFoundError:
            self.summary = replace(self.summary, error="git unavailable")
        except (OSError, ValueError, subprocess.SubprocessError):
            self.summary = replace(self.summary, error="git error")
        finally:
            if process is None and spawn is not None:
                try:
                    process = await spawn
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
            if process is not None:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                # wait() alone does not drain/close subprocess pipe transports.
                await process.communicate()
        return self.summary
