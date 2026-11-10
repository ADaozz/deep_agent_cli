"""Execution capabilities and failures shared by policies and adapters."""

from enum import StrEnum


SANDBOX_ROOT = "/workspace"


SKILLS_ROOT = "/skills"


class ExecutionMode(StrEnum):
    SANDBOXED = "SANDBOXED"
    CUSTOM = "CUSTOM"


class SandboxUnavailableError(RuntimeError):
    """沙箱不可用，并带有可机读的诊断类别。"""

    def __init__(self, message: str, *, kind: str = "preflight_failed") -> None:
        super().__init__(message)
        self.kind = kind
