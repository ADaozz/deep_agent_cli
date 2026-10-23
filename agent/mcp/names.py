"""Names that keep MCP tools out of the native tool and approval sets."""
from __future__ import annotations

from collections.abc import Iterable
import hashlib
import re

MCP_ADD = "mcp_add"
MCP_LIST = "mcp_list"
MCP_REMOVE = "mcp_remove"
MANAGEMENT_TOOLS = frozenset({MCP_ADD, MCP_LIST, MCP_REMOVE})
EXTERNAL_PREFIX = "mcp__"

HOST_APPROVAL: dict[str, list[str]] = {"allowed_decisions": ["approve", "reject"]}


def external_tool_name(server: str, remote: str) -> str:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,32}", server):
        raise ValueError("invalid MCP server name")
    if not isinstance(remote, str) or not remote:
        raise ValueError("remote tool name must be a non-empty string")
    prefix = f"{EXTERNAL_PREFIX}{server}__"
    budget = 80 - len(prefix)
    if len(remote) <= budget and re.fullmatch(r"[A-Za-z0-9_-]+", remote):
        return prefix + remote
    try:
        digest = hashlib.sha256(remote.encode("utf-8")).hexdigest()[:12]
    except UnicodeError:
        raise ValueError("remote tool name is not valid UTF-8") from None
    slug = re.sub(r"[^A-Za-z0-9_-]", "_", remote)
    return prefix + slug[:budget - len(digest) - 1] + "_" + digest


def is_external_mcp_tool(name: str) -> bool:
    return name.startswith(EXTERNAL_PREFIX)


def is_managed_mcp_name(name: str) -> bool:
    """True for host MCP tools and the built-in MCP management tools."""
    return name in MANAGEMENT_TOOLS or is_external_mcp_tool(name)


def host_interrupt_on(tool_names: Iterable[str]) -> dict[str, dict[str, list[str]]]:
    """Approval rules for host MCP operations.

    Applied by the factory in permission ask mode only. Every external tool
    requires approval in ask, regardless of server-supplied read-only hints.
    Permission allow skips these rules along with the native tool rules.
    """
    entries = {
        MCP_ADD: dict(HOST_APPROVAL),
        MCP_REMOVE: dict(HOST_APPROVAL),
    }
    for name in tool_names:
        entries[name] = dict(HOST_APPROVAL)
    return entries
