"""Names that keep MCP tools out of the native tool and approval sets."""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import re

from agent.runtime.permissions import host_decision_rule

MCP_ADD = "mcp_add"
MCP_LIST = "mcp_list"
MCP_REMOVE = "mcp_remove"
MANAGEMENT_TOOLS = frozenset({MCP_ADD, MCP_LIST, MCP_REMOVE})
EXTERNAL_PREFIX = "mcp__"

_SERVER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,32}$")


def valid_server_name(value: str) -> bool:
    return isinstance(value, str) and _SERVER_NAME.fullmatch(value) is not None


def external_tool_name(server: str, remote: str) -> str:
    if not valid_server_name(server):
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


def unique_mcp_identity(name: str) -> tuple[str, str] | None:
    """Return server and remote tool only when one valid server boundary exists.

    Several ``__`` boundaries are left unresolved. Callers use
    ``mcp_display_identity`` for the server and remote name recorded at registration.
    """
    if not is_external_mcp_tool(name):
        return None
    rest = name[len(EXTERNAL_PREFIX):]
    matches: list[tuple[str, str]] = []
    start = 0
    while True:
        index = rest.find("__", start)
        if index <= 0:
            break
        server, remote = rest[:index], rest[index + 2:]
        if remote and valid_server_name(server):
            matches.append((server, remote))
        start = index + 2
    if len(matches) != 1:
        return None
    return matches[0]


# Display-only. The registered name stays the protocol identity.
_DISPLAY_IDENTITIES: dict[str, tuple[str, str]] = {}


def register_mcp_display_identity(local_name: str, server: str, remote_name: str) -> None:
    """Remember the server and remote tool already stored on the live tool."""
    if local_name and server and remote_name:
        _DISPLAY_IDENTITIES[local_name] = (server, remote_name)


def forget_mcp_display_identity(local_name: str) -> None:
    _DISPLAY_IDENTITIES.pop(local_name, None)


def mcp_display_identity(local_name: str) -> tuple[str, str] | None:
    return _DISPLAY_IDENTITIES.get(local_name)


def is_managed_mcp_name(name: str) -> bool:
    """True for host MCP tools and the built-in MCP management tools."""
    return name in MANAGEMENT_TOOLS or is_external_mcp_tool(name)


def host_interrupt_on(tool_names: Iterable[str]) -> dict[str, dict[str, list[str]]]:
    """Approval rules for host MCP operations.

    Applied by the factory in permission ask mode only. Every external tool
    requires approval in ask, regardless of server-supplied read-only hints.
    Permission allow skips these rules along with the native tool rules.
    Each entry gets a fresh allowed_decisions list.
    """
    entries = {
        MCP_ADD: host_decision_rule(),
        MCP_REMOVE: host_decision_rule(),
    }
    for name in tool_names:
        entries[name] = host_decision_rule()
    return entries
