"""Runtime tool-approval policy (ask vs sandboxed allow-all)."""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from agent.sandbox import ExecutionMode


class PermissionMode(StrEnum):
    """Two-state tool permission policy."""

    ASK = "ask"  # Side-effect tools and every execute pause for approval.
    ALLOW = "allow"  # Auto-approve declared capabilities (SANDBOXED only).


ASK_INTERRUPT_ON: dict[str, Any] = {
    "send_email": {"allowed_decisions": ["approve", "reject"]},
    "execute": {"allowed_decisions": ["approve", "reject"]},
    "write_file": True,
    "edit_file": True,
    "delete": True,
}

PERMISSION_ALLOW_WARNING = (
    "HIGH RISK: Permission mode ALLOW auto-approves every tool call and any "
    "capabilities it declares (including execute with network=true, file writes, "
    "deletes, and send_email). There is no per-call confirmation. Only enable "
    "this if the agent is running in a SANDBOXED backend."
)


def allow_mode_available(execution_mode: ExecutionMode) -> bool:
    """ALLOW is only valid when the backend is a known isolated sandbox."""
    return execution_mode is ExecutionMode.SANDBOXED


def allow_mode_unavailable_reason(execution_mode: ExecutionMode) -> str:
    return (
        "ALLOW is only available when execution mode is SANDBOXED "
        f"(current: {execution_mode.value})"
    )


def interrupt_on_for_mode(mode: PermissionMode) -> dict[str, Any] | None:
    """Return create_agent interrupt_on for the mode.

    Empty mapping disables HumanInTheLoopMiddleware tool gates when the factory
    treats an explicit mapping as a full replacement for the default CONFIRM set.
    """
    if mode is PermissionMode.ALLOW:
        return {}
    return dict(ASK_INTERRUPT_ON)


def permission_mode_from_interrupt_on(interrupt_on: dict[str, Any] | None) -> PermissionMode:
    return PermissionMode.ALLOW if not interrupt_on else PermissionMode.ASK


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
        return "allow (auto-approve declared capabilities — SANDBOXED only, HIGH RISK)"
    return "ask (side-effect tools and every execute require approval)"
