"""Runtime tool-approval policy (pi-style ask vs allow-all)."""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from agent.network import network_requested


class PermissionMode(StrEnum):
    """Two-state tool permission policy."""

    ASK = "ask"  # Side-effect tools / declared NETWORK pause for approval.
    ALLOW = "allow"  # Auto-approve declared capabilities (high risk).


def _execute_requests_network(request: Any) -> bool:
    tool_call = getattr(request, "tool_call", None) or {}
    args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    return network_requested(args)


# Under ASK: FS writes / email always pause; execute only when NETWORK is declared.
ASK_INTERRUPT_ON: dict[str, Any] = {
    "send_email": {"allowed_decisions": ["approve", "reject"]},
    "execute": {
        "allowed_decisions": ["approve", "reject"],
        "when": _execute_requests_network,
    },
    "write_file": True,
    "edit_file": True,
    "delete": True,
}

PERMISSION_ALLOW_WARNING = (
    "HIGH RISK: Permission mode ALLOW auto-approves every tool call and any "
    "capabilities it declares (including execute with network=true, file writes, "
    "deletes, and send_email). There is no per-call confirmation. Only enable "
    "this if you trust the agent and the workspace."
)


def interrupt_on_for_mode(mode: PermissionMode) -> dict[str, Any] | None:
    """Return create_agent interrupt_on for the mode.

    Empty mapping disables HumanInTheLoopMiddleware tool gates when the factory
    treats an explicit mapping as a full replacement for the default CONFIRM set.
    """
    if mode is PermissionMode.ALLOW:
        return {}
    return dict(ASK_INTERRUPT_ON)


def parse_permission_mode(value: str) -> PermissionMode | None:
    key = value.strip().lower().replace("_", "-")
    aliases = {
        "ask": PermissionMode.ASK,
        "confirm": PermissionMode.ASK,
        "approve": PermissionMode.ASK,
        "all-approve": PermissionMode.ASK,
        "allapprove": PermissionMode.ASK,
        "allow": PermissionMode.ALLOW,
        "bypass": PermissionMode.ALLOW,
        "yolo": PermissionMode.ALLOW,
        "auto": PermissionMode.ALLOW,
        "all-allow": PermissionMode.ALLOW,
    }
    return aliases.get(key)


def permission_mode_label(mode: PermissionMode) -> str:
    if mode is PermissionMode.ALLOW:
        return "allow (auto-approve declared capabilities — HIGH RISK)"
    return "ask (side-effect tools and NETWORK require approval)"
