"""本地 execute 工具，可声明 NETWORK 能力。"""
from __future__ import annotations

from deepagents.backends.protocol import SandboxBackendProtocol
from langchain_core.tools import BaseTool, StructuredTool
from agent.network import reset_execute_network, set_execute_network

_PERSISTENCE = (
    "Each call starts a fresh shell (cd and shell variables do not carry over), "
    "but the sandbox environment is reused for the session: files under /tmp "
    "and /home and background processes (for example `nohup cmd > log 2>&1 &`) "
    "survive between calls and across sandbox restarts. "
    "restricted mode keeps system directories read-only. "
    "full-access mode can write the real filesystem except the agent installation "
    "and configuration directory. "
)
_ASK_DESCRIPTION = (
    "Run a shell command in the current working directory ({workspace_root}) inside the sandbox. "
    "Defaults to no network. Under permission ask every execute needs "
    "approval. network=true runs the command in the networked sandbox with host "
    "network access, including localhost and LAN addresses; background processes "
    "it starts keep that access between calls. " + _PERSISTENCE
    + "network=true and offline commands run in two separate sandboxes that share "
    "the workspace ({workspace_root}) and declared extra mounts. Use the same network value for commands that depend on "
    "each other's /tmp files or background processes. "
)
_ALLOW_DESCRIPTION = (
    "Run a shell command in the current working directory ({workspace_root}) inside the sandbox. "
    "Permission allow mode: commands run without approval and host network "
    "(internet, localhost, LAN) is enabled by default for every call. " + _PERSISTENCE
)
_SEARCH_GUIDANCE = (
    "Use `rg -n 'pattern' path` to search file contents "
    "and `rg --files -g 'pattern' path` to find files. "
    "Narrow the search scope when possible. "
    "Use read_file to inspect known files and "
    "edit_file/write_file for file modifications. "
    "rg is already on PATH inside the sandbox; do not install or download it."
)
_SEARCH_UNAVAILABLE = (
    "ripgrep (rg) is not installed on the host. "
    "Search with rg is unavailable. Do not claim that search works, and do not install or download rg."
)


def _execute_description(
    *, network_by_default: bool, workspace_root: str = "/workspace",
) -> str:
    from agent.tools.grep import resolve_rg

    base = _ALLOW_DESCRIPTION if network_by_default else _ASK_DESCRIPTION
    guidance = _SEARCH_GUIDANCE if resolve_rg() else _SEARCH_UNAVAILABLE
    return base.replace("{workspace_root}", workspace_root) + guidance


def build_execute_tool(
    backend: SandboxBackendProtocol,
    *,
    network_by_default: bool = False,
) -> BaseTool:
    """绑定到沙箱 backend 的 shell execute。

    ``network=True`` 使本条命令共享宿主网络命名空间。
    ``network_by_default=True``（权限模式 allow）为每次调用开放宿主网络；
    单次调用的 ``network=false`` 无法关掉。
    """

    def execute(
        command: str,
        timeout: int | None = None,
        network: bool = False,
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
            network_by_default=network_by_default,
            workspace_root=getattr(backend, "workspace_root", "/workspace"),
        ),
    )
