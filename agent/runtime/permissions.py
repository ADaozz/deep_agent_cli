"""运行时工具审批策略（ask / auto / allow）。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agent.execution.types import ExecutionMode
from agent.tools.catalog import (
    EXECUTE_TOOL,
    FILE_MUTATION_TOOLS,
    WEB_TOOLS,
    WRITE_STDIN_TOOL,
)


class PermissionMode(StrEnum):
    """工具权限策略。"""

    ASK = "ask"  # 有副作用的工具、每次 execute 和网页工具调用 都暂停等待审批。
    AUTO = "auto"
    ALLOW = "allow"  # 自动批准所有工具（含宿主 MCP）；沙箱网络开放（仅 SANDBOXED）。


# 审批口径由工具目录派生：Ask 的 interrupt_on 和 Auto 的静态规则共用。
# 显式审批形状（approve/reject）与文件变更的布尔闸门保持既有 Deep Agents 约定。


def host_decision_rule() -> dict[str, list[str]]:
    """每次返回独立的 approve/reject 规则字典（含新的 allowed_decisions 列表）。"""
    return {"allowed_decisions": ["approve", "reject"]}


def write_stdin_requires_approval(request: Any) -> bool:
    """Ask/HITL ``when`` predicate: empty output polls skip approval."""
    args = getattr(request, "tool_call", None) or {}
    if isinstance(args, dict):
        payload = args.get("args") or {}
    else:
        payload = {}
    action = str(payload.get("action") or "write")
    chars = str(payload.get("chars") or "")
    return not (action == "write" and chars == "")


def ask_interrupt_on() -> dict[str, Any]:
    """Ask 模式 interrupt_on：每次新建，避免共享可变 allowed_decisions。"""
    write_stdin_rule = {
        **host_decision_rule(),
        "when": write_stdin_requires_approval,
    }
    return {
        **{name: host_decision_rule() for name in (EXECUTE_TOOL, *WEB_TOOLS)},
        WRITE_STDIN_TOOL: write_stdin_rule,
        **{name: True for name in FILE_MUTATION_TOOLS},
    }

PERMISSION_ALLOW_WARNING = (
    "HIGH RISK. Auto-approves every tool, including writes, deletes, and host MCP. "
    "execute runs with the sandbox network open. MCP runs on the host, outside the sandbox. "
    "Switching back to ask does not stop work already running. SANDBOXED only."
)

# Auto 不可用时的统一降级文案（工厂、Runner、CLI、会话恢复共用）。
AUTO_FALLBACK_REASON = (
    "Auto requires SANDBOXED execution and llm.models.deepseek.api_key; using ask."
)
SAVED_AUTO_FALLBACK_NOTICE = (
    "Saved auto permission requires SANDBOXED execution and llm.models.deepseek.api_key; using ask."
)
SAVED_ALLOW_FALLBACK_NOTICE = "Saved allow permission is unavailable here; using ask."


@dataclass(frozen=True)
class PermissionModeDisplay:
    """CLI 选项、补全与帮助共用的展示元数据。"""

    value: str
    completion: str
    option: str
    summary: str


PERMISSION_MODE_DISPLAY: dict[PermissionMode, PermissionModeDisplay] = {
    PermissionMode.ASK: PermissionModeDisplay(
        value="ask",
        completion="Require approval for tools",
        option="ask · require approval for every execute and side-effect tool",
        summary="ask (side-effect tools, execute, web search and host MCP require approval)",
    ),
    PermissionMode.AUTO: PermissionModeDisplay(
        value="auto",
        completion="Risk-based approval; per-command network",
        option="auto · risk-based approval, per-command network",
        summary="auto (risk-based approval; per-command network)",
    ),
    PermissionMode.ALLOW: PermissionModeDisplay(
        value="allow",
        completion="Auto-approve tools (SANDBOXED, HIGH RISK)",
        option="allow · auto-approve all tools; sandbox network open (SANDBOXED, HIGH RISK)",
        summary="allow (auto-approve all tools; sandbox network OPEN — SANDBOXED only, HIGH RISK)",
    ),
}


def allow_mode_available(execution_mode: ExecutionMode) -> bool:
    """ALLOW 仅在 backend 为已知隔离沙箱时可用。"""
    return permission_unavailable_reason(
        PermissionMode.ALLOW,
        execution_mode=execution_mode,
        has_deepseek_key=True,
    ) is None


def allow_mode_unavailable_reason(execution_mode: ExecutionMode) -> str:
    return (
        "ALLOW is only available when execution mode is SANDBOXED "
        f"(current: {execution_mode.value})"
    )


def permission_unavailable_reason(
    mode: PermissionMode,
    *,
    execution_mode: ExecutionMode,
    has_deepseek_key: bool,
) -> str | None:
    """返回该权限模式当前不可用的原因；可用时返回 None。

    调用方自行决定拒绝还是降级：Auto 通常降级为 Ask，Allow 通常拒绝切换。
    """
    if mode is PermissionMode.ASK:
        return None
    if execution_mode is not ExecutionMode.SANDBOXED:
        if mode is PermissionMode.ALLOW:
            return allow_mode_unavailable_reason(execution_mode)
        return AUTO_FALLBACK_REASON
    if mode is PermissionMode.AUTO and not has_deepseek_key:
        return AUTO_FALLBACK_REASON
    return None


def permission_available(
    mode: PermissionMode,
    *,
    execution_mode: ExecutionMode,
    has_deepseek_key: bool,
) -> bool:
    return permission_unavailable_reason(
        mode,
        execution_mode=execution_mode,
        has_deepseek_key=has_deepseek_key,
    ) is None


def interrupt_on_for_mode(mode: PermissionMode) -> dict[str, Any] | None:
    """返回该模式下 create_agent 使用的 interrupt_on。

    空映射会关掉 HumanInTheLoopMiddleware 的工具闸门：工厂把显式传入的映射
    视为对默认 CONFIRM 集合的完整替换。
    """
    if mode is PermissionMode.ALLOW:
        return {}
    return ask_interrupt_on()


def permission_mode_from_interrupt_on(interrupt_on: dict[str, Any] | None) -> PermissionMode:
    """旧接口兼容：仅从有无 interrupt_on 推断 ASK↔ALLOW。

    不能用来还原三值权限模式；AUTO 与 ASK 在 interrupt_on 形状上相同，
    必须以显式 PermissionMode 为准。
    """
    return PermissionMode.ASK if interrupt_on else PermissionMode.ALLOW


def parse_permission_mode(value: str) -> PermissionMode | None:
    try:
        return PermissionMode(value.strip().lower())
    except ValueError:
        return None


def permission_mode_label(mode: PermissionMode) -> str:
    return PERMISSION_MODE_DISPLAY[mode].summary


def permission_mode_option_label(mode: PermissionMode, *, current: bool = False) -> str:
    label = PERMISSION_MODE_DISPLAY[mode].option
    return f"{label} · current" if current else label


def permission_completion_description(mode: str) -> str:
    """斜杠补全用描述；未知 mode 不抛 KeyError。"""
    parsed = parse_permission_mode(mode)
    if parsed is None:
        return "Permission mode"
    return PERMISSION_MODE_DISPLAY[parsed].completion


__all__ = [
    "AUTO_FALLBACK_REASON",
    "PERMISSION_ALLOW_WARNING",
    "PERMISSION_MODE_DISPLAY",
    "PermissionMode",
    "PermissionModeDisplay",
    "SAVED_ALLOW_FALLBACK_NOTICE",
    "SAVED_AUTO_FALLBACK_NOTICE",
    "allow_mode_available",
    "allow_mode_unavailable_reason",
    "ask_interrupt_on",
    "host_decision_rule",
    "interrupt_on_for_mode",
    "parse_permission_mode",
    "permission_available",
    "permission_completion_description",
    "permission_mode_from_interrupt_on",
    "permission_mode_label",
    "permission_mode_option_label",
    "write_stdin_requires_approval",
    "permission_unavailable_reason",
]
