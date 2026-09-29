"""Filesystem middleware capability check for the workspace shell wrapper."""
from __future__ import annotations

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware

from agent.sandbox import WorkspaceCompositeBackend


class WorkspaceFilesystemMiddleware(FilesystemMiddleware):
    """Keep the separately registered execute tool visible to the model.

    Deep Agents checks only a CompositeBackend's default route for shell
    support. Our default route denies file access outside /workspace, while
    WorkspaceCompositeBackend.execute delegates to the sandbox executor.
    """

    @property
    def name(self) -> str:
        # Deep Agents replaces its built-in middleware by matching this name.
        return "FilesystemMiddleware"

    def _unsupported_tools_and_execution_state(
        self, tool_names: set[str | None],
    ) -> tuple[set[str | None], bool, BackendProtocol | None]:
        unsupported, execution_active, backend = super()._unsupported_tools_and_execution_state(tool_names)
        if "execute" in tool_names and isinstance(self.backend, WorkspaceCompositeBackend):
            unsupported.discard("execute")
            execution_active = True
        return unsupported, execution_active, backend
