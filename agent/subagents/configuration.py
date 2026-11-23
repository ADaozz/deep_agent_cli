"""Subagent execution policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# Host ceilings: effective value is min(user_or_default, HARD_MAX_*).
# max_concurrent = direct children per parent Agent; max_total = whole tree.
# Depth is how many SubAgent layers may nest under root.
DEFAULT_MAX_CONCURRENT = 4
HARD_MAX_PER_PARENT = 4
DEFAULT_MAX_TOTAL = 10
HARD_MAX_TOTAL = 10
DEFAULT_MAX_SUBAGENT_DEPTH = 2
HARD_MAX_SUBAGENT_DEPTH = 2


@dataclass(frozen=True)
class SubAgentConfig:
    enabled: bool = True
    # Simultaneous direct-child cap for each parent Agent (not a per-depth pool).
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    # Whole-tree simultaneous SubAgent cap (excluding Root).
    max_total: int = DEFAULT_MAX_TOTAL
    # How many SubAgent layers may nest under the root (depth 0).
    # ``2`` allows depths 1 and 2; an agent at depth == max cannot create more.
    max_subagent_depth: int = DEFAULT_MAX_SUBAGENT_DEPTH
    model: str | None = None
    reasoning_effort: str | None = "default"
    # Also offer background delegation next to the synchronous task tool.
    background: bool = True

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "SubAgentConfig":
        unknown = set(values) - cls.__dataclass_fields__.keys()
        if unknown:
            raise ValueError(f"Unknown subagents settings: {', '.join(sorted(unknown))}")
        return cls(**values)

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("subagents.enabled must be a boolean")
        if not isinstance(self.background, bool):
            raise ValueError("subagents.background must be a boolean")
        for name, label in (
            ("max_concurrent", "subagents.max_concurrent"),
            ("max_total", "subagents.max_total"),
            ("max_subagent_depth", "subagents.max_subagent_depth"),
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        # Clamp to host ceilings: user config can only tighten, never exceed.
        object.__setattr__(self, "max_concurrent", min(self.max_concurrent, HARD_MAX_PER_PARENT))
        object.__setattr__(self, "max_total", min(self.max_total, HARD_MAX_TOTAL))
        object.__setattr__(
            self, "max_subagent_depth", min(self.max_subagent_depth, HARD_MAX_SUBAGENT_DEPTH),
        )
        for name in ("model", "reasoning_effort"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"subagents.{name} must be a non-empty string or null")
        if self.reasoning_effort is None:
            object.__setattr__(self, "reasoning_effort", "default")
        if self.reasoning_effort == "auto":
            raise ValueError("subagents.reasoning_effort auto is not supported yet")

    def may_create_subagent(self, depth: int) -> bool:
        """True when an agent at ``depth`` is allowed to spawn another SubAgent."""
        return self.enabled and depth < self.max_subagent_depth
