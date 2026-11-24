"""本地 execute 工具，可声明 NETWORK 能力。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated
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
    "must use the same network value."
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
    from agent.tools.grep import resolve_rg

    if permission is PermissionMode.ALLOW:
        policy = _ALLOW_POLICY
    elif permission is PermissionMode.AUTO:
        policy = _AUTO_POLICY
    else:
        policy = _ASK_POLICY
    guidance = _SEARCH_GUIDANCE if resolve_rg() else _SEARCH_UNAVAILABLE
    return (_COMMON + policy + guidance).replace("{workspace_root}", workspace_root)


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
        timeout: Annotated[int | None, Field(description="Timeout in seconds for this call. Overrides sandbox.timeout_seconds when set.")] = None,
        network: Annotated[bool, Field(description="Use the networked sandbox. Allow mode is always networked.")] = False,
    ) -> tuple[str, dict[str, object]]:
        token = set_execute_network(network or network_by_default)
        try:
            if timeout is not None:
                response = backend.execute(command, timeout=timeout)
            else:
                response = backend.execute(command)
        finally:
            reset_execute_network(token)
        output = response.output or ""
        if response.exit_code not in (0, None):
            suffix = f"\n\nExit code: {response.exit_code}"
            if suffix.strip() not in output:
                output = f"{output.rstrip()}{suffix}"
        if response.truncated:
            marker = "\n\n[output truncated]"
            if "[Output truncated: showing " not in output and marker not in output:
                output = f"{output.rstrip()}{marker}"
        artifact: dict[str, object] = {
            "exit_code": response.exit_code,
            "truncated": response.truncated,
            "host_log_path": getattr(response, "host_log_path", None),
            "agent_log_path": getattr(response, "agent_log_path", None),
            "log_error": getattr(response, "log_error", None),
            "termination_reason": getattr(response, "termination_reason", None),
            "max_output_bytes": getattr(response, "max_output_bytes", None),
        }
        return output, artifact

    return StructuredTool.from_function(
        func=execute,
        name="execute",
        response_format="content_and_artifact",
        description=_execute_description(
            permission=permission,
            workspace_root=getattr(backend, "workspace_root", "/workspace"),
        ),
    )
