"""Capabilities exposed by execution backends."""

from abc import ABC, abstractmethod
from pathlib import Path

from agent.execution.policy import SandboxConfig


class ManagedExecutor(ABC):
    @abstractmethod
    def reconfigure(self, config: SandboxConfig, skills: Path | None, environment: dict[str, str]) -> None:
        """Stop old workers, then publish execution settings; raise before publication on failure."""


def supports_interactive_execution(backend: object) -> bool:
    """True when ``backend`` supports yielded executions and ``write_stdin``."""
    return bool(getattr(backend, "supports_interactive_execution", False))
