"""工具名称与基础分类的唯一来源。

审批策略、CLI 预览分组、MCP 保留名，以及各节点能注册的工具集合都从此派生。
本模块不创建工具，也不渲染。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

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
})

# Ask / Auto 静态规则关心的副作用工具（审批策略口径）。
SIDE_EFFECT_TOOLS = frozenset({EXECUTE_TOOL}) | WEB_TOOLS | FILE_MUTATION_TOOLS

# 文件系统中间件对外暴露的本地名（有序；新增工具只改这一处）。
FILESYSTEM_TOOL_NAMES: tuple[str, ...] = ("ls", "read", "write", "edit", "delete")
FILESYSTEM_TOOLS = frozenset(FILESYSTEM_TOOL_NAMES)

# 每个节点都从这里起步。沙箱、密钥和 hidden_tools 再决定其中哪些真正注册。
COMMON_TOOLS = frozenset({
    *FILESYSTEM_TOOL_NAMES,
    EXECUTE_TOOL,
    *WEB_TOOLS,
    "compact_conversation",
})

ROOT_ONLY_TOOLS = frozenset({
    "request_human_input",
})

ROOT_TASK_TOOLS = frozenset({
    "start_task",
    "read_task",
    "cancel_task",
})

SYNC_TASK_TOOLS = frozenset({
    "start_task",
    "read_task",
})

ASYNC_TASK_TOOLS = frozenset()

ROOT_TOOLS = COMMON_TOOLS | ROOT_ONLY_TOOLS | ROOT_TASK_TOOLS
SYNC_TOOLS = COMMON_TOOLS | SYNC_TASK_TOOLS
ASYNC_TOOLS = COMMON_TOOLS

NodeKind = Literal["root", "sync", "async", "max_depth"]

TASK_TOOL = "start_task"
# The only tool that launches a delegated task.
DELEGATION_TOOLS = frozenset({TASK_TOOL})
# Usage tables skip task tools. The names are the root task set.
METRICS_EXCLUDED_TASK_TOOLS = ROOT_TASK_TOOLS

# 模型可见但通常不走 Ask 闸门的内部/会话工具。
INTERNAL_TOOLS = (
    ROOT_ONLY_TOOLS
    | {"compact_conversation"}
    | ROOT_TASK_TOOLS
)

# MCP 别名不得占用的全部原生名（比 SIDE_EFFECT_TOOLS 更宽）。
NATIVE_TOOL_NAMES = (
    FILESYSTEM_TOOLS
    | EXPLORATION_TOOLS
    | WEB_TOOLS
    | {EXECUTE_TOOL}
    | INTERNAL_TOOLS
)


def node_tools(kind: NodeKind) -> frozenset[str]:
    """Ceiling for one node. Runtime limits are applied separately."""
    if kind == "root":
        return ROOT_TOOLS
    if kind == "sync":
        return SYNC_TOOLS
    return COMMON_TOOLS


def apply_runtime_limits(
    names: frozenset[str],
    *,
    subagents_enabled: bool,
    can_launch: bool,
    hidden: Iterable[str] = (),
) -> frozenset[str]:
    """Drop tools the current config cannot offer. The node ceiling stays intact."""
    allowed = set(names)
    if not subagents_enabled:
        allowed -= ROOT_TASK_TOOLS | SYNC_TASK_TOOLS | ASYNC_TASK_TOOLS
    if not can_launch:
        allowed.discard("start_task")
    allowed.difference_update(hidden)
    return frozenset(allowed)


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
