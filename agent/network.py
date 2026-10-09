"""单次 execute 的 NETWORK 能力（由 Bubblewrap 网络命名空间物理门控）。"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

_EXECUTE_NETWORK: ContextVar[bool] = ContextVar("deep_agent_execute_network", default=False)


def network_requested(args: dict[str, Any] | None) -> bool:
    """工具调用声明了 NETWORK 能力时为 True。"""
    if not args:
        return False
    value = args.get("network", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def get_execute_network() -> bool:
    return bool(_EXECUTE_NETWORK.get())


def set_execute_network(enabled: bool):
    """为当前工具调用设置 NETWORK。返回用于复位的 token。"""
    return _EXECUTE_NETWORK.set(bool(enabled))


def reset_execute_network(token) -> None:
    _EXECUTE_NETWORK.reset(token)
