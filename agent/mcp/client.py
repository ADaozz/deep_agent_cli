"""Tool discovery through the official MCPAdapter only."""
from __future__ import annotations

import re
from dataclasses import dataclass

from langchain_core.tools import BaseTool

from agent.mcp.bridge import AsyncBridge, SyncMCPTool
from agent.mcp.config import MCPProfile, redacted_url


async def discover_mcp_tools(profile: MCPProfile) -> list[BaseTool]:
    from fastmcp.client import Client
    from fastmcp.client.transports import SSETransport, StreamableHttpTransport
    from langchain.mcp import MCPAdapter

    async def decline_elicitation(*args, **kwargs):
        # The official adapter otherwise enables a second interrupt protocol.
        # First release supports tools only; no user interaction capability.
        from mcp.types import ElicitResult
        return ElicitResult(action="decline")

    transport_type = SSETransport if profile.transport == "sse" else StreamableHttpTransport
    client = Client(transport_type(profile.url), timeout=30, elicitation_handler=decline_elicitation)
    async with MCPAdapter(client) as adapter:
        tools = await adapter.list_tools()
    names = set()
    from jsonschema.validators import validator_for
    for tool in tools:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool.name) or tool.name in names:
            raise ValueError("MCP returned an invalid or duplicate tool name")
        names.add(tool.name)
        schema = tool.tool_call_schema
        if not isinstance(schema, dict):
            schema = schema.model_json_schema()
        validator_for(schema).check_schema(schema)
        if schema.get("type") != "object":
            raise ValueError("MCP tool parameters must be an object schema")
    return tools


@dataclass
class MCPToolSet:
    bridge: AsyncBridge
    tools: tuple[BaseTool, ...]

    @classmethod
    def prepare(cls, profile: MCPProfile) -> MCPToolSet:
        bridge = AsyncBridge()
        try:
            originals = bridge.run(discover_mcp_tools(profile), timeout=30)
            tools = tuple(SyncMCPTool(tool, bridge, profile=profile.name, url=redacted_url(profile.url))
                          for tool in originals)
            return cls(bridge, tools)
        except BaseException:
            bridge.close()
            raise

    def close(self) -> None:
        self.bridge.close()
