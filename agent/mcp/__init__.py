"""Host-side MCP integration with lazy loading of network adapters."""
from importlib import import_module

__all__ = ["MCPManager", "MCPResult", "ServerStatus"]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    module = "agent.mcp.manager" if name == "MCPManager" else "agent.mcp.contracts"
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value
