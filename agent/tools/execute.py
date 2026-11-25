"""本地 execute 工具，可声明 NETWORK 能力。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal
from pydantic import Field

from deepagents.backends.protocol import SandboxBackendProtocol
from langchain_core.tools import BaseTool, StructuredTool

from agent.execution.network import reset_execute_network, set_execute_network

if TYPE_CHECKING:
    from agent.runtime.permissions import PermissionMode


_COMMON = (
    "Execute a shell command in the sandbox working directory ({workspace_root}). "
    "Each call starts a new shell. Working-directory changes and shell variables do not persist between calls. "
    "The sandbox environment is reused for the session. Files in /tmp and /home, and background processes, "
    "persist across calls and sandbox restarts. "
    "In restricted mode system directories are read-only. In full-access mode the real filesystem is writable, "
    "except the agent install and configuration directories. "
    "Networked and offline commands run in separate sandboxes that share the workspace ({workspace_root}) "
    "and declared extra mounts. Commands that depend on each other's /tmp files or background processes "
    "must use the same network value. "
    "Use yield_time to return control while a process is running. "
    "Enable interactive when stdin is required. "
    "Enable tty only for programs that require a terminal. "
    "Use write_stdin to interact with an active execution."
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
    *, permission: PermissionMode, workspace_root: str = "/workspace",
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
    return (_COMMON + policy + guidance).replace("{workspace_root}", workspace_root)


def _stdin_mode(*, interactive: bool, tty: bool) -> str:
    if tty:
        return "pty"
    if interactive:
        return "pipe"
    return "none"


def _artifact_from_response(response) -> dict[str, object]:
    return {
        "exit_code": response.exit_code,
        "truncated": response.truncated,
        "host_log_path": getattr(response, "host_log_path", None),
        "agent_log_path": getattr(response, "agent_log_path", None),
        "log_error": getattr(response, "log_error", None),
        "termination_reason": getattr(response, "termination_reason", None),
        "max_output_bytes": getattr(response, "max_output_bytes", None),
        "execution_id": getattr(response, "execution_id", None),
        "running": bool(getattr(response, "running", False)),
    }


def _execution_pool(backend: SandboxBackendProtocol):
    executor = getattr(backend, "executor", backend)
    pool = getattr(executor, "pool", None)
    if pool is None:
        raise RuntimeError("write_stdin requires a sandboxed execution pool")
    return pool


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

    def execute(
        command: Annotated[str, Field(description="Shell command to run.")],
        timeout: Annotated[int | None, Field(description="Timeout in seconds for the whole process lifetime. Overrides sandbox.timeout_seconds when set.")] = None,
        network: Annotated[bool, Field(description="Use the networked sandbox. Allow mode is always networked.")] = False,
        yield_time: Annotated[float | None, Field(description="Seconds to wait before returning while the process keeps running. Null keeps the previous synchronous behavior.")] = None,
        tty: Annotated[bool, Field(description="Allocate a PTY with correct controlling-terminal semantics. Implies interactive input.")] = False,
        interactive: Annotated[bool, Field(description="Keep a writable stdin PIPE for write_stdin.")] = False,
    ) -> tuple[str, dict[str, object]]:
        token = set_execute_network(network or network_by_default)
        try:
            response = backend.execute(
                command,
                timeout=timeout,
                yield_time=yield_time,
                stdin_mode=_stdin_mode(interactive=interactive, tty=tty),
            )
        finally:
            reset_execute_network(token)
        output = response.output or ""
        running = bool(getattr(response, "running", False))
        if not running and response.exit_code not in (0, None):
            suffix = f"\n\nExit code: {response.exit_code}"
            if suffix.strip() not in output:
                output = f"{output.rstrip()}{suffix}"
        if response.truncated:
            marker = "\n\n[output truncated]"
            if "[Output truncated: showing " not in output and marker not in output:
                output = f"{output.rstrip()}{marker}"
        return output, _artifact_from_response(response)

    return StructuredTool.from_function(
        func=execute,
        name="execute",
        response_format="content_and_artifact",
        description=_execute_description(
            permission=permission,
            workspace_root=getattr(backend, "workspace_root", "/workspace"),
        ),
    )


def build_write_stdin_tool(backend: SandboxBackendProtocol) -> BaseTool:
    """Interact with a yielded execution owned by the current session/agent."""

    def write_stdin(
        execution_id: Annotated[str, Field(description="Active execution id returned by execute.")],
        chars: Annotated[str, Field(description="Bytes to write. Empty polls for new output.")] = "",
        yield_time: Annotated[float, Field(description="Seconds to wait for new output after the action.")] = 1.0,
        action: Annotated[
            Literal["write", "close", "cancel"],
            Field(description="write input, close PIPE stdin, or cancel the execution."),
        ] = "write",
    ) -> tuple[str, dict[str, object]]:
        from agent.execution.lifecycle import interact

        if yield_time < 0:
            raise ValueError("yield_time must be non-negative")
        if action in {"close", "cancel"} and chars:
            raise ValueError(f"{action} cannot include chars")
        registry = _execution_pool(backend).executions
        live = registry.require(execution_id)
        try:
            response = interact(live, chars=chars, yield_time=yield_time, action=action)
        except (OSError, TimeoutError, ValueError) as exc:
            return (
                f"Error writing stdin: {exc}",
                {
                    "exit_code": None,
                    "truncated": False,
                    "execution_id": execution_id,
                    "running": True,
                    "termination_reason": "stdin_error",
                },
            )
        if not response.running:
            registry.discard(execution_id)
        output = response.output or ""
        if not response.running and response.exit_code not in (0, None):
            suffix = f"\n\nExit code: {response.exit_code}"
            if suffix.strip() not in output:
                output = f"{output.rstrip()}{suffix}"
        return output, _artifact_from_response(response)

    return StructuredTool.from_function(
        func=write_stdin,
        name="write_stdin",
        response_format="content_and_artifact",
        description=(
            "Interact with an active execution. "
            "Empty chars polls new output. "
            "Non-empty chars writes input to the process. "
            "Never pass sensitive credentials through this tool."
        ),
    )


def lookup_execution_network(backend: Any, execution_id: str) -> bool | None:
    """Return the yielded execution's network flag, or None when unknown."""
    try:
        session = _execution_pool(backend).executions.get(execution_id)
    except Exception:  # noqa: BLE001
        return None
    return None if session is None else bool(session.network)
