"""本地 execute 工具，可声明 NETWORK 能力。"""
from __future__ import annotations

from typing import Annotated
from pydantic import Field

from deepagents.backends.protocol import SandboxBackendProtocol
from langchain_core.tools import BaseTool, StructuredTool
from agent.network import reset_execute_network, set_execute_network

_PERSISTENCE = (
    "每次调用启动新的 Shell，cd 和 Shell 变量不会保留；沙箱环境在会话内复用。"
    "/tmp、/home 中的文件和后台进程（例如 nohup cmd > log 2>&1 &）在调用之间及沙箱重启后保留。"
    "restricted 模式下系统目录只读；full-access 模式可写入真实文件系统，但智能体安装目录和配置目录受保护。"
)
_ASK_DESCRIPTION = (
    "在沙箱当前工作目录（{workspace_root}）执行 Shell 命令。默认无网络；ask 模式下每次 execute 都需审批。"
    "network=true 使用有网沙箱，可访问互联网、宿主 localhost 和局域网；启动的后台进程在后续调用之间保留网络能力。"
    + _PERSISTENCE
    + "有网与无网命令在两个独立沙箱中运行，共享工作区（{workspace_root}）和声明的额外挂载。"
    "依赖彼此 /tmp 文件或后台进程的命令应使用相同的 network 值。"
)
_ALLOW_DESCRIPTION = (
    "在沙箱当前工作目录（{workspace_root}）执行 Shell 命令。"
    "allow 模式下无需审批，每次调用均可访问互联网、宿主 localhost 和局域网。" + _PERSISTENCE
)
_SEARCH_GUIDANCE = (
    "使用 rg -n 'pattern' path 搜索文件内容，使用 rg --files -g 'pattern' path 查找文件，并尽量缩小范围。"
    "已知文件用 read_file 阅读，文件修改使用 edit_file 或 write_file。"
    "沙箱 PATH 已包含 rg，无需安装或下载。"
)
_SEARCH_UNAVAILABLE = "宿主未安装 ripgrep（rg），无法使用 rg 搜索；不要声称搜索可用，也不要安装或下载 rg。"


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
        command: Annotated[str, Field(description="要执行的 Shell 命令。")],
        timeout: Annotated[int | None, Field(description="本次执行超时秒数；省略时使用后端默认值。")] = None,
        network: Annotated[bool, Field(description="是否使用有网沙箱；allow 模式下始终有网。")] = False,
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
