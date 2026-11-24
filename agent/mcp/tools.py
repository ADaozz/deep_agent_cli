"""Built-in MCP catalog tools. These are not tools from an external MCP server."""

from __future__ import annotations

from typing import Annotated
from pydantic import Field

from langchain_core.tools import BaseTool, StructuredTool

from agent.mcp.manager import MCPManager
from agent.mcp.names import MCP_ADD, MCP_LIST, MCP_REMOVE


_NOT_YET = (
    "Do not claim the server tools are available before this result returns. "
    "New tools register at the next safe point after this call ends."
)


def build_management_tools(manager: MCPManager) -> list[BaseTool]:
    def mcp_list() -> str:
        """List configured MCP servers, connection state, and how many tools each server provides."""
        return manager.describe()

    def mcp_add(
        name: Annotated[str, Field(description="Server name.")],
        transport: Annotated[str, Field(description="Transport: streamable_http or stdio.")],
        url: Annotated[str, Field(description="HTTP server URL. Leave empty for stdio.")] = "",
        command: Annotated[str, Field(description="stdio command executed on the host.")] = "",
        argv: Annotated[list[str] | None, Field(description="Argument array for the stdio command.")] = None,
        auth_type: Annotated[str, Field(description="Authentication: none, bearer, or oauth.")] = "none",
        token_env: Annotated[str, Field(description="Environment variable name that holds the bearer token, not the token.")] = "",
        env_passthrough: Annotated[list[str] | None, Field(description="Host environment variable names passed to the stdio process.")] = None,
    ) -> str:
        """Add a host MCP server. This operation is subject to the current permission and approval policy.

        transport is streamable_http or stdio. Bearer auth takes the environment variable name token_env, not the secret.
        A stdio command runs on the host, outside the sandbox. Tell the user the full command.
        Tools from a new server register at the next safe point after this call ends.
        OAuth reuses and refreshes a cached grant. When refresh fails, a browser opens for the user.
        Do not confirm authorization for the user, and do not add or remove the server repeatedly to force it.
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

    def mcp_remove(name: Annotated[str, Field(description="Server name.")]) -> str:
        """Remove a configured MCP server. This operation is subject to the current permission and approval policy. A missing name does not change the file."""
        result = manager.remove(name)
        return result.message

    list_tool = StructuredTool.from_function(mcp_list, name=MCP_LIST)
    add_tool = StructuredTool.from_function(mcp_add, name=MCP_ADD)
    remove_tool = StructuredTool.from_function(mcp_remove, name=MCP_REMOVE)
    return [list_tool, add_tool, remove_tool]
