"""Capabilities exposed by execution backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Protocol, runtime_checkable

from deepagents.backends.protocol import ExecuteResponse

from agent.execution.policy import SandboxConfig


class ManagedExecutor(ABC):
    @abstractmethod
    def reconfigure(self, config: SandboxConfig, skills: Path | None, environment: dict[str, str]) -> None:
        """Stop old workers, then publish execution settings; raise before publication on failure."""


@runtime_checkable
class InteractiveExecutionBackend(Protocol):
    """Unified contract for backends that support yielded/interactive command sessions."""

    supports_interactive_execution: bool

    def write_stdin(
        self,
        execution_id: str,
        *,
        chars: str = "",
        yield_time: float = 1.0,
        action: str = "write",
        write_timeout: float = 15.0,
        thread_id: str | None = None,
    ) -> ExecuteResponse: ...

    def execution_info(
        self,
        execution_id: str,
        *,
        thread_id: str | None = None,
    ) -> dict[str, object] | None: ...

    def execution_metadata(
        self,
        execution_id: str,
        *,
        thread_id: str | None = None,
    ) -> dict[str, object] | None: ...

    def execution_snapshots(self, thread_id: str) -> dict[str, dict[str, object]]: ...

    def list_active_executions(self, thread_id: str) -> list[dict[str, object]]: ...

    def send_execution_input(
        self,
        execution_id: str,
        *,
        chars: str = "",
        action: str = "write",
        write_timeout: float = 15.0,
        thread_id: str | None = None,
    ) -> None: ...

    def resize_execution(
        self,
        execution_id: str,
        rows: int,
        cols: int,
        *,
        thread_id: str | None = None,
    ) -> None: ...

    def cleanup_executions(self, thread_id: str, *, join_timeout: float = 2.0) -> int: ...


def supports_interactive_execution(backend: object) -> bool:
    """True when ``backend`` declares interactive execution and ``write_stdin`` support."""
    return bool(getattr(backend, "supports_interactive_execution", False))


def interactive_backend(backend: object) -> InteractiveExecutionBackend | None:
    """Return ``backend`` narrowed to ``InteractiveExecutionBackend`` when supported."""
    if supports_interactive_execution(backend) and isinstance(backend, InteractiveExecutionBackend):
        return backend
    return None
