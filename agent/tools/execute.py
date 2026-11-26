"""本地 execute 工具，可声明 NETWORK 能力。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal
from pydantic import Field

from deepagents.backends.protocol import SandboxBackendProtocol
from langchain_core.tools import BaseTool, StructuredTool

from agent.execution.network import reset_execute_network, set_execute_network
from agent.execution.ports import interactive_backend
from agent.execution.process import execution_artifact_from_response, execution_metadata_dict

if TYPE_CHECKING:
    from agent.runtime.permissions import PermissionMode


_SYNC_COMMON = (
    "Execute a shell command in the sandbox at {workspace_root}. "
    "Each call starts a new shell; cwd changes and shell variables do not persist. "
    "Files persist across calls, but running processes do not survive sandbox restarts. "
    "In restricted mode system directories are read-only. In full-access mode the real filesystem is writable, "
    "except the agent install and configuration directories. "
    "Networked and offline commands run in separate sandboxes that share the workspace ({workspace_root}) "
    "and declared extra mounts. Commands that depend on each other's /tmp files or running processes "
    "must use the same network value."
)
_INTERACTIVE_GUIDANCE = (
    " Use yield_time for long-running or interactive commands. "
    "If execution continues, the result includes an execution_id for subsequent write_stdin calls."
)
_ASK_POLICY = (
    " Ask mode requires approval before each execute. "
    "The sandbox has no network unless network=true, which can reach the internet, host localhost, and the LAN."
)
_AUTO_POLICY = (
    " Auto mode reviews each execute under the current approval policy. "
    "It does not ask a person to approve every command. "
    "The sandbox has no network unless network=true, which can reach the internet, host localhost, and the LAN."
)
_ALLOW_POLICY = (
    " Allow mode runs execute without approval. "
    "Every call can reach the internet, host localhost, and the LAN."
)
_SEARCH_GUIDANCE = (
    " Use rg to locate files and search source code. "
    "Use read to inspect known files, edit to modify existing files, and write to create or replace files. "
    "rg is already on the sandbox PATH. Do not install or download it."
)
_SEARCH_UNAVAILABLE = (
    " ripgrep (rg) is not installed on the host. Do not claim that rg search is available, "
    "and do not install or download rg. "
    "Use read to inspect known files, edit to modify existing files, and write to create or replace files."
)


def _execute_description(
    *,
    permission: PermissionMode,
    workspace_root: str = "/workspace",
    interactive_capable: bool = True,
) -> str:
    from agent.runtime.permissions import PermissionMode
    from agent.execution.ripgrep import resolve_rg

    if permission is PermissionMode.ALLOW:
        policy = _ALLOW_POLICY
    elif permission is PermissionMode.AUTO:
        policy = _AUTO_POLICY
    else:
        policy = _ASK_POLICY
    guidance = _SEARCH_GUIDANCE if resolve_rg() else _SEARCH_UNAVAILABLE
    common = _SYNC_COMMON + (_INTERACTIVE_GUIDANCE if interactive_capable else "")
    return (common + policy + guidance).replace("{workspace_root}", workspace_root)


def _stdin_mode(*, interactive: bool, tty: bool) -> str:
    if tty:
        return "pty"
    if interactive:
        return "pipe"
    return "none"


def _artifact_from_response(response) -> dict[str, object]:
    return execution_artifact_from_response(response)


def build_execute_tool(
    backend: SandboxBackendProtocol,
    *,
    permission: PermissionMode | None = None,
) -> BaseTool:
    """绑定到沙箱 backend 的 shell execute。

    ``network=True`` 使本条命令共享宿主网络命名空间。
    ``permission`` 为 allow 时每次调用都开放宿主网络；
    省略时按 ask 处理。单次调用的 ``network=false`` 无法关掉。
    """
    from agent.runtime.permissions import PermissionMode

    if permission is None:
        permission = PermissionMode.ASK
    network_by_default = permission is PermissionMode.ALLOW
    interactive_exec = interactive_backend(backend)

    def execute_interactive(
        command: Annotated[str, Field(description="Shell command to execute.")],
        timeout: Annotated[int | None, Field(description="Maximum process lifetime in seconds. Omit to use the configured timeout.")] = None,
        network: Annotated[bool, Field(description="Enable network access for this command. Always enabled in Allow mode.")] = False,
        yield_time: Annotated[float | None, Field(description="Seconds to wait before returning. Omit to wait until completion.")] = None,
        tty: Annotated[bool, Field(description="Allocate a PTY. Implies interactive input.")] = False,
        interactive: Annotated[bool, Field(description="Keep stdin open for subsequent input.")] = False,
    ) -> tuple[str, dict[str, object]]:
        assert interactive_exec is not None
        token = set_execute_network(network or network_by_default)
        mode = _stdin_mode(interactive=interactive, tty=tty)
        try:
            response = interactive_exec.execute(
                command,
                timeout=timeout,
                yield_time=yield_time,
                stdin_mode=mode,
            )
        finally:
            reset_execute_network(token)
        output = response.output or ""
        return output, _artifact_from_response(response)

    def execute_sync(
        command: Annotated[str, Field(description="Shell command to execute.")],
        timeout: Annotated[int | None, Field(description="Maximum process lifetime in seconds. Omit to use the configured timeout.")] = None,
        network: Annotated[bool, Field(description="Enable network access for this command. Always enabled in Allow mode.")] = False,
    ) -> tuple[str, dict[str, object]]:
        token = set_execute_network(network or network_by_default)
        try:
            response = backend.execute(command, timeout=timeout)
        finally:
            reset_execute_network(token)
        output = response.output or ""
        return output, _artifact_from_response(response)

    return StructuredTool.from_function(
        func=execute_interactive if interactive_exec is not None else execute_sync,
        name="execute",
        response_format="content_and_artifact",
        description=_execute_description(
            permission=permission,
            workspace_root=getattr(backend, "workspace_root", "/workspace"),
            interactive_capable=interactive_exec is not None,
        ),
    )


def build_write_stdin_tool(backend: SandboxBackendProtocol) -> BaseTool:
    """Interact with a yielded execution owned by the current session/agent."""
    interactive = interactive_backend(backend)
    if interactive is None:
        raise ValueError("Backend does not support interactive execution")

    def write_stdin(
        execution_id: Annotated[str, Field(description="Execution ID returned by execute.")],
        chars: Annotated[
            str,
            Field(
                description=(
                    "Input text. Empty polls output; include \\n to submit a line. "
                    "Send \\u0003 for PTY Ctrl+C."
                ),
            ),
        ] = "",
        yield_time: Annotated[float, Field(description="Seconds to wait for new output after the action.")] = 1.0,
        action: Annotated[
            Literal["write", "close", "cancel"],
            Field(
                description=(
                    "write: send input; close: close PIPE stdin; "
                    "cancel: terminate execution. Leave chars empty for close and cancel."
                ),
            ),
        ] = "write",
    ) -> tuple[str, dict[str, object]]:
        from agent.execution.pool import StdinWriteUncertainError

        if yield_time < 0:
            raise ValueError("yield_time must be non-negative")
        if action in {"close", "cancel"} and chars:
            raise ValueError(f"{action} cannot include chars")
        try:
            response = interactive.write_stdin(
                execution_id, chars=chars, yield_time=yield_time, action=action,
            )
        except (KeyError, PermissionError):
            raise
        except StdinWriteUncertainError as exc:
            meta = interactive.execution_metadata(execution_id) or execution_metadata_dict(
                execution_id=execution_id,
            )
            return (
                f"Error: Uncertain stdin write outcome: {exc}",
                {**meta, "stdin_error": "uncertain"},
            )
        except (OSError, TimeoutError, ValueError) as exc:
            meta = interactive.execution_metadata(execution_id) or execution_metadata_dict(
                execution_id=execution_id,
            )
            return (
                f"Error: Failed writing stdin: {exc}",
                {**meta, "stdin_error": "error"},
            )
        output = response.output or ""
        return output, _artifact_from_response(response)

    return StructuredTool.from_function(
        func=write_stdin,
        name="write_stdin",
        response_format="content_and_artifact",
        description=(
            "Interact with an execution returned by execute. "
            "Returns new output and the current execution state. "
            "Use the same execution_id until completion. "
            "Never send credentials through this tool. "
            "Do not retry input automatically if delivery is uncertain."
        ),
    )
