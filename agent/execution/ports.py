"""Capability required to publish a live sandbox filesystem policy."""

from abc import ABC, abstractmethod
from pathlib import Path

from agent.execution.policy import SandboxConfig


class ManagedExecutor(ABC):
    @abstractmethod
    def reconfigure(self, config: SandboxConfig, skills: Path | None, environment: dict[str, str]) -> None:
        """Stop old workers, then publish execution settings; raise before publication on failure."""
