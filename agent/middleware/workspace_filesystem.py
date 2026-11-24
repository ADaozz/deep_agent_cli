"""Filesystem middleware capability check for the workspace shell wrapper."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool

from agent.execution.filesystem import WorkspaceCompositeBackend
from agent.tools.catalog import FILE_MUTATION_TOOLS


LS_DESCRIPTION = (
    "List files and directories at the specified absolute path.\n"
    "If the file path is already known, use read directly.\n"
    "Use rg --files to locate files by name when available."
)

READ_FILE_DESCRIPTION = (
    "Read the file at the specified absolute path.\n"
    "By default, read the first 100 lines.\n"
    "Use an appropriately larger limit when more context is needed.\n"
    "Read independent files concurrently when practical.\n"
    "Read an existing file before editing it.\n"
    "Do not include the read-result status header in edit arguments.\n"
    "Large tool results saved to a file are read from the path given in the message.\n"
    "Images, PDFs, and audio or video return multimodal content; pass only file_path. "
    "For video, offset and limit are in seconds."
)

WRITE_FILE_DESCRIPTION = (
    "Create a new file or replace the entire contents of an existing file.\n"
    "Confirm that existing content can be replaced before overwriting a file.\n"
    "Prefer edit for targeted modifications.\n"
    "Do not issue concurrent write, edit, or delete calls for the same file_path. "
    "Independent files may be modified concurrently."
)

EDIT_FILE_DESCRIPTION = (
    "Modify a file through exact string replacement.\n"
    "Read the file before editing it.\n"
    "Preserve the original indentation and whitespace.\n"
    "Apply related changes together when appropriate.\n"
    "Perform separate modifications to the same file sequentially.\n"
    "Independent files may be modified concurrently.\n"
    "Do not include the read-result status header in edit arguments."
)

DELETE_FILE_DESCRIPTION = (
    "Delete the specified file or directory.\n"
    "Deleting a directory recursively removes its contents.\n"
    "Only delete paths within the authorized task scope that are no longer needed.\n"
    "Do not issue concurrent write, edit, or delete calls for the same file_path. "
    "Independent files may be modified concurrently."
)

TOOL_DESCRIPTIONS = {
    "ls": LS_DESCRIPTION,
    "read": READ_FILE_DESCRIPTION,
    "write": WRITE_FILE_DESCRIPTION,
    "edit": EDIT_FILE_DESCRIPTION,
    "delete": DELETE_FILE_DESCRIPTION,
}

_NATIVE_FILE_NAMES = {"read": "read_file", "write": "write_file", "edit": "edit_file"}
_FILE_TOOL_NAMES = {native: name for name, native in _NATIVE_FILE_NAMES.items()}


class WorkspaceFilesystemMiddleware(FilesystemMiddleware):
    """Keep the separately registered execute tool visible to the model.

    Deep Agents checks only a CompositeBackend's default route for shell
    support. Our default route denies file access outside the project mount, while
    WorkspaceCompositeBackend.execute delegates to the sandbox executor.
    """

    def __init__(self, *, custom_tool_descriptions: Mapping[str, str] | None = None, **kwargs: Any) -> None:
        if (tools := kwargs.get("tools")) is not None:
            kwargs["tools"] = [_NATIVE_FILE_NAMES.get(name, name) for name in tools]
        descriptions = {**TOOL_DESCRIPTIONS, **(custom_tool_descriptions or {})}
        super().__init__(
            custom_tool_descriptions={_NATIVE_FILE_NAMES.get(name, name): text for name, text in descriptions.items()},
            **kwargs,
        )

    def _create_tools(self) -> list[BaseTool]:
        tools = super()._create_tools()
        for tool in tools:
            tool.name = _FILE_TOOL_NAMES.get(tool.name, tool.name)
            for native, name in _FILE_TOOL_NAMES.items():
                tool.description = tool.description.replace(native, name)
        return tools

    @staticmethod
    def _filesystem_request(request: Any) -> Any:
        """Adapt only the upstream filesystem hook's view of the current call."""
        name = request.tool_call["name"]
        if name not in _NATIVE_FILE_NAMES and name != "delete":
            return request
        call = {**request.tool_call, "name": _NATIVE_FILE_NAMES.get(name, name)}
        state = request.state
        if name in FILE_MUTATION_TOOLS and isinstance(state, Mapping):
            messages = list(state.get("messages", ()))
            for index in range(len(messages) - 1, -1, -1):
                message = messages[index]
                if isinstance(message, AIMessage):
                    calls = [
                        {**item, "name": _NATIVE_FILE_NAMES.get(item["name"], item["name"])}
                        for item in message.tool_calls
                    ]
                    messages[index] = message.model_copy(update={"tool_calls": calls})
                    state = {**state, "messages": messages}
                    break
        return request.override(tool_call=call, state=state)

    @staticmethod
    def _name_result(result: Any, name: str) -> Any:
        if isinstance(result, ToolMessage) and result.name in _FILE_TOOL_NAMES:
            return result.model_copy(update={"name": name})
        return result

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        result = super().wrap_tool_call(self._filesystem_request(request), lambda _: handler(request))
        return self._name_result(result, request.tool_call["name"])

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        result = await super().awrap_tool_call(self._filesystem_request(request), lambda _: handler(request))
        return self._name_result(result, request.tool_call["name"])

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
            # filesystem, where the project mount is the shell root, so returning the
            # composite would inject a bogus "workspace is not accessible
            # from the shell" notice. The executor is not a CompositeBackend,
            # which makes that prompt empty.
            backend = self.backend.executor
        return unsupported, execution_active, backend
