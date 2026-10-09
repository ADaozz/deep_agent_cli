"""本地 execute 工具，可声明 NETWORK 能力。"""
from __future__ import annotations

from deepagents.backends.protocol import SandboxBackendProtocol
from langchain_core.tools import BaseTool, StructuredTool
from agent.network import reset_execute_network, set_execute_network

_PERSISTENCE = (
    "Each call starts a fresh shell (cd and shell variables do not carry over), "
    "but the sandbox environment is reused for the session: files under /tmp "
    "and /home and background processes (for example `nohup cmd > log 2>&1 &`) "
    "survive between calls. "
)
_ASK_DESCRIPTION = (
    "Run a shell command inside the sandbox workspace (/workspace). "
    "Defaults to no network. Under permission ask every execute needs "
    "approval. network=true runs the command in the networked sandbox with host "
    "network access, including localhost and LAN addresses; background processes "
    "it starts keep that access between calls. " + _PERSISTENCE
    + "network=true and offline commands run in two separate sandboxes that share "
    "only /workspace, so use the same network value for commands that depend on "
    "each other's /tmp files or background processes. "
    "Prefer ls/read_file/glob/grep/write_file for filesystem work."
)
_ALLOW_DESCRIPTION = (
    "Run a shell command inside the sandbox workspace (/workspace). "
    "Permission allow mode: commands run without approval and host network "
    "(internet, localhost, LAN) is enabled by default for every call. " + _PERSISTENCE
    + "Prefer ls/read_file/glob/grep/write_file for filesystem work."
)


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
        description=_ALLOW_DESCRIPTION if network_by_default else _ASK_DESCRIPTION,
    )
