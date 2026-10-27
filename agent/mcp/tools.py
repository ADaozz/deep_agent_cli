"""Built-in MCP catalog tools. These are not tools from an external MCP server."""
from __future__ import annotations

from typing import Annotated
from pydantic import Field

from langchain_core.tools import BaseTool, StructuredTool

from agent.mcp.manager import MCPManager
from agent.mcp.names import MCP_ADD, MCP_LIST, MCP_REMOVE

_NOT_YET = (
    "在当前结果返回前，不要声称服务工具已经可用。"
    "新工具只会在当前调用结束后的下一安全点注册。"
)


def build_management_tools(manager: MCPManager) -> list[BaseTool]:
    def mcp_list() -> str:
        """列出已配置的 MCP 服务、连接状态和各服务提供的工具数量。"""
        return manager.describe()

    def mcp_add(
        name: Annotated[str, Field(description="服务名称。")],
        transport: Annotated[str, Field(description="传输方式：streamable_http 或 stdio。")],
        url: Annotated[str, Field(description="HTTP 服务地址；stdio 留空。")] = "",
        command: Annotated[str, Field(description="在宿主执行的 stdio 命令。")] = "",
        argv: Annotated[list[str] | None, Field(description="stdio 命令的参数数组。")] = None,
        auth_type: Annotated[str, Field(description="鉴权方式：none、bearer 或 oauth。")] = "none",
        token_env: Annotated[str, Field(description="保存 Bearer 令牌的环境变量名，不是令牌本身。")] = "",
        env_passthrough: Annotated[list[str] | None, Field(description="传递给 stdio 子进程的宿主环境变量名。")] = None,
    ) -> str:
        """添加宿主 MCP 服务。ask 模式需审批，allow 模式自动执行。

        transport 为 streamable_http 或 stdio。Bearer 鉴权只传环境变量名 token_env，
        不传密钥本身。stdio 的 command 在宿主执行，不受沙箱限制，argv 为命令参数；
        向用户说明完整命令。新增服务的工具在当前调用结束后的下一安全点才会注册。
        OAuth 优先复用缓存并刷新；无法刷新时自动打开浏览器，由用户完成授权。
        不要代替用户确认授权，也不要反复添加或删除重添服务来触发授权。
        """
        result = manager.add_from_fields(
            name=name,
            transport=transport,
            url=url,
            command=command,
            args=argv,
            auth_type=auth_type,
            token_env=token_env,
            env_passthrough=env_passthrough,
        )
        if result.ok and result.pending:
            return result.message + " " + _NOT_YET
        if result.ok:
            return result.message
        return result.message + " The new tools are not available."

    def mcp_remove(name: Annotated[str, Field(description="服务名称。")]) -> str:
        """移除已配置的 MCP 服务。ask 模式需审批，allow 模式自动执行；名称不存在时不修改文件。"""
        result = manager.remove(name)
        return result.message

    list_tool = StructuredTool.from_function(mcp_list, name=MCP_LIST)
    add_tool = StructuredTool.from_function(mcp_add, name=MCP_ADD)
    remove_tool = StructuredTool.from_function(mcp_remove, name=MCP_REMOVE)
    return [list_tool, add_tool, remove_tool]
