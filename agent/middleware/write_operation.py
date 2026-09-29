"""Attach the pre-write file state to successful write_file results."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from deepagents.backends.protocol import BackendProtocol, LsResult
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from agent.file_mutation import WriteOperation


def _operation(result: LsResult) -> WriteOperation | None:
    error = result.error or ""
    if error.endswith(": path_not_found"):
        return "create"
    if error.endswith(": not_a_directory"):
        return "overwrite"
    return None


def _write_path(request: Any) -> str | None:
    call = getattr(request, "tool_call", None) or {}
    if call.get("name") != "write_file":
        return None
    args = call.get("args")
    if not isinstance(args, dict) or not isinstance(args.get("file_path"), str):
        return None
    return args["file_path"]


def _annotate(result: Any, operation: WriteOperation | None) -> Any:
    if operation is None or not isinstance(result, ToolMessage) or result.status == "error":
        return result
    artifact = result.artifact if isinstance(result.artifact, dict) else {}
    return result.model_copy(update={"artifact": {**artifact, "operation": operation}})


class WriteOperationMiddleware(AgentMiddleware):
    """Classify a write from the backend state immediately before execution."""

    def __init__(self, backend: BackendProtocol) -> None:
        super().__init__()
        self._backend = backend

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        path = _write_path(request)
        try:
            operation = _operation(self._backend.ls(path)) if path is not None else None
        except (OSError, RuntimeError, NotImplementedError):
            operation = None
        return _annotate(handler(request), operation)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        path = _write_path(request)
        try:
            operation = _operation(self._backend.ls(path)) if path is not None else None
        except (OSError, RuntimeError, NotImplementedError):
            operation = None
        return _annotate(await handler(request), operation)
