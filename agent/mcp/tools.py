"""Built-in MCP catalog tools. These are not tools from an external MCP server."""
from __future__ import annotations

from langchain_core.tools import BaseTool, StructuredTool

from agent.mcp.manager import MCPManager
from agent.mcp.names import MCP_ADD, MCP_LIST, MCP_REMOVE

_NOT_YET = (
    "Do not claim the server's tools are usable before this result. "
    "They are registered only at the next safe point, after the current tool call finishes."
)


def build_management_tools(manager: MCPManager) -> list[BaseTool]:
    def mcp_list() -> str:
        """List configured MCP servers, their status, and how many tools each one exposes."""
        return manager.describe()

    def mcp_add(
        name: str,
        transport: str,
        url: str = "",
        command: str = "",
        argv: list[str] | None = None,
        auth_type: str = "none",
        token_env: str = "",
        env_passthrough: list[str] | None = None,
    ) -> str:
        """Add a host MCP server. Requires user approval before anything is saved.

        transport is streamable_http or stdio. For bearer auth pass token_env, the
        environment variable name, never the token. stdio command runs on the host,
        outside the sandbox, and argv is its argument list. A successful add does
        not make the new tools callable in this same step.
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

    def mcp_remove(name: str) -> str:
        """Remove one configured MCP server. Requires user approval. Missing names do not change the file."""
        result = manager.remove(name)
        return result.message

    list_tool = StructuredTool.from_function(mcp_list, name=MCP_LIST)
    add_tool = StructuredTool.from_function(mcp_add, name=MCP_ADD)
    remove_tool = StructuredTool.from_function(mcp_remove, name=MCP_REMOVE)
    return [list_tool, add_tool, remove_tool]
