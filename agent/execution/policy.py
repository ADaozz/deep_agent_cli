"""Execution policy values, independent of configuration I/O and backend adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


SAFE_INHERITED_ENV = (
    "LANG",
    "TERM",
    "COLORTERM",
    "NO_COLOR",
    "TZ",
)


@dataclass(frozen=True)
class BindMount:
    """An explicit host-to-sandbox mount.

    ``modes`` is ``None`` when the mount applies in both filesystem modes.
    That is the historical behavior for entries that omit the field.
    """

    source: Path
    destination: str
    optional: bool = False
    dev_bind: bool = False
    modes: tuple["SandboxMode", ...] | None = None


SandboxMode = Literal["restricted", "full-access"]


_SANDBOX_MODES = frozenset({"restricted", "full-access"})


def parse_sandbox_mode(value: str) -> SandboxMode | None:
    key = value.strip().lower()
    if key == "restricted":
        return "restricted"
    if key == "full-access":
        return "full-access"
    return None


@dataclass(frozen=True)
class SandboxConfig:
    """Bubblewrap policy. ``restricted`` is the default filesystem boundary."""

    workspace: Path = field(default_factory=Path.cwd)
    bwrap_path: str = "bwrap"
    mode: SandboxMode = "restricted"
    timeout_seconds: int | None = None
    max_output_bytes: int = 100_000
    env_allowlist: tuple[str, ...] = ()
    env_set: dict[str, str] = field(default_factory=dict)
    extra_read_only_mounts: tuple[BindMount, ...] = ()
    extra_read_write_mounts: tuple[BindMount, ...] = ()
    protected_config_dir: Path | None = None
    protected_files: tuple[Path, ...] = ()
    trust_env: bool = False

    def __post_init__(self) -> None:
        if self.mode not in _SANDBOX_MODES:
            raise ValueError(
                f"sandbox.mode must be restricted or full-access, got {self.mode!r}"
            )


def sandbox_workspace_root(workspace: Path) -> str:
    """Path of the workspace inside the sandbox: its real absolute path.

    Both restricted and full-access use this path as the working directory.
    A project at ``/mnt/d/test`` is ``/mnt/d/test`` in either mode.
    """
    return workspace.expanduser().resolve().as_posix()


def default_skills_dir() -> Path:
    return Path.home() / ".deep-agent" / "skills"
