"""Per-execute NETWORK capability (physical gate via Bubblewrap netns)."""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

_EXECUTE_NETWORK: ContextVar[bool] = ContextVar("deep_agent_execute_network", default=False)


def network_requested(args: dict[str, Any] | None) -> bool:
    """True when a tool call declares the NETWORK capability."""
    if not args:
        return False
    value = args.get("network", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def get_execute_network() -> bool:
    return bool(_EXECUTE_NETWORK.get())


def set_execute_network(enabled: bool):
    """Set NETWORK for the current tool-call scope. Returns a reset token."""
    return _EXECUTE_NETWORK.set(bool(enabled))


def reset_execute_network(token) -> None:
    _EXECUTE_NETWORK.reset(token)
