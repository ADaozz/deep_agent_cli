"""Names that keep MCP tools out of the native tool and approval sets."""
from __future__ import annotations

from collections.abc import Iterable

MCP_ADD = "mcp_add"
MCP_LIST = "mcp_list"
MCP_REMOVE = "mcp_remove"
MANAGEMENT_TOOLS = frozenset({MCP_ADD, MCP_LIST, MCP_REMOVE})
EXTERNAL_PREFIX = "mcp__"

HOST_APPROVAL: dict[str, list[str]] = {"allowed_decisions": ["approve", "reject"]}


def external_tool_name(server: str, remote: str) -> str:
    return f"{EXTERNAL_PREFIX}{server}__{remote}"


def is_external_mcp_tool(name: str) -> bool:
    return name.startswith(EXTERNAL_PREFIX)


def is_managed_mcp_name(name: str) -> bool:
    """True for host MCP tools and the built-in MCP management tools."""
    return name in MANAGEMENT_TOOLS or is_external_mcp_tool(name)


def host_interrupt_on(tool_names: Iterable[str]) -> dict[str, dict[str, list[str]]]:
    """Approval rules for host MCP operations.

    Every external tool requires approval. Server-supplied read-only hints are
    not a trust decision: the process runs on the host, outside Bubblewrap.
    ``allow`` does not inherit these entries away.
    """
    entries = {
        MCP_ADD: dict(HOST_APPROVAL),
        MCP_REMOVE: dict(HOST_APPROVAL),
    }
    for name in tool_names:
        entries[name] = dict(HOST_APPROVAL)
    return entries
