"""工具名称与基础分类的唯一来源。

审批策略、CLI 预览分组、MCP 保留名都从此派生；不负责工具注册或渲染。
"""

from __future__ import annotations

EXECUTE_TOOL = "execute"

WEB_TOOLS = frozenset({
    "web_search",
    "web_fetch",
})

FILE_MUTATION_TOOLS = frozenset({
    "write",
    "edit",
    "delete",
})

EXPLORATION_TOOLS = frozenset({
    "ls",
    "read",
    "grep",
    "glob",
})

# Ask / Auto 静态规则关心的副作用工具（审批策略口径）。
SIDE_EFFECT_TOOLS = frozenset({EXECUTE_TOOL}) | WEB_TOOLS | FILE_MUTATION_TOOLS

# 文件系统中间件对外暴露的本地名（含只读与变更）。
FILESYSTEM_TOOLS = frozenset({"ls", "read", "write", "edit", "delete"})

# 模型可见但通常不走 Ask 闸门的内部/会话工具。
INTERNAL_TOOLS = frozenset({
    "request_human_input",
    "compact_conversation",
    "start_task",
    "start_background_task",
    "check_background_task",
    "cancel_background_task",
    "read_task_log",
})

# MCP 别名不得占用的全部原生名（比 SIDE_EFFECT_TOOLS 更宽）。
NATIVE_TOOL_NAMES = (
    FILESYSTEM_TOOLS
    | EXPLORATION_TOOLS
    | WEB_TOOLS
    | {EXECUTE_TOOL}
    | INTERNAL_TOOLS
)


def is_web_tool(name: str) -> bool:
    return name in WEB_TOOLS


def is_mutation_tool(name: str) -> bool:
    return name in FILE_MUTATION_TOOLS


def is_exploration_tool(name: str) -> bool:
    return name in EXPLORATION_TOOLS


def is_side_effect_tool(name: str) -> bool:
    return name in SIDE_EFFECT_TOOLS


def tool_preview_group(name: str) -> str:
    """CLI 预览分组：explore / mutation / command / web / other。"""
    if name in EXPLORATION_TOOLS:
        return "explore"
    if name in FILE_MUTATION_TOOLS:
        return "mutation"
    if name == EXECUTE_TOOL:
        return "command"
    if name in WEB_TOOLS:
        return "web"
    return "other"
