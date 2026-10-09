"""运行时工具审批策略（ask 与沙箱内的 allow-all）。"""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from agent.sandbox import ExecutionMode


class PermissionMode(StrEnum):
    """两态工具权限策略。"""

    ASK = "ask"  # 有副作用的工具、每次 execute 和 web_search 都暂停等待审批。
    ALLOW = "allow"  # 自动批准全部工具；沙箱网络开放（仅 SANDBOXED）。


ASK_INTERRUPT_ON: dict[str, Any] = {
    "execute": {"allowed_decisions": ["approve", "reject"]},
    "web_search": {"allowed_decisions": ["approve", "reject"]},
    "write_file": True,
    "edit_file": True,
    "delete": True,
}

PERMISSION_ALLOW_WARNING = (
    "HIGH RISK: Permission mode ALLOW auto-approves every tool call and any "
    "capabilities it declares (including file writes and deletes), and runs "
    "every execute with the sandbox network OPEN: full host network access "
    "(internet, localhost, LAN). There is no per-call confirmation. Switching "
    "back to ask only restores approval for later commands; already running "
    "sandbox processes keep going. Only enable this if the agent is running "
    "in a SANDBOXED backend."
)

def allow_mode_available(execution_mode: ExecutionMode) -> bool:
    """ALLOW 仅在 backend 为已知隔离沙箱时可用。"""
    return execution_mode is ExecutionMode.SANDBOXED


def allow_mode_unavailable_reason(execution_mode: ExecutionMode) -> str:
    return (
        "ALLOW is only available when execution mode is SANDBOXED "
        f"(current: {execution_mode.value})"
    )


def interrupt_on_for_mode(mode: PermissionMode) -> dict[str, Any] | None:
    """返回该模式下 create_agent 使用的 interrupt_on。

    空映射会关掉 HumanInTheLoopMiddleware 的工具闸门：工厂把显式传入的映射
    视为对默认 CONFIRM 集合的完整替换。
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
        return "allow (auto-approve all tools; sandbox network OPEN — SANDBOXED only, HIGH RISK)"
    return "ask (side-effect tools, every execute and web search require approval)"
