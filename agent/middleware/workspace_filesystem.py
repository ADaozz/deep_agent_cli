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
            # Upstream builds its "virtual mounts vs. shell paths" prompt from
            # the returned backend, judging reachability by the composite's
            # default route. Our execute shell runs on the executor's
            # filesystem, where /workspace is the shell root, so returning the
            # composite would inject a bogus "/workspace is not accessible
            # from the shell" notice. The executor is not a CompositeBackend,
            # which makes that prompt empty.
            backend = self.backend.executor
        return unsupported, execution_active, backend
