"""Host-side MCP client. Connections do not enter the Bubblewrap sandbox."""
from agent.mcp.manager import MCPManager, MCPResult, ServerStatus

__all__ = ["MCPManager", "MCPResult", "ServerStatus"]
