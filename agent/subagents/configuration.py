"""Subagent execution policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class SubAgentConfig:
    enabled: bool = True
    max_concurrent: int = 2
    model: str | None = None
    reasoning_effort: str | None = "default"
    permission: str = "allow"

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "SubAgentConfig":
        unknown = set(values) - cls.__dataclass_fields__.keys()
        if unknown:
            raise ValueError(f"Unknown subagents settings: {', '.join(sorted(unknown))}")
        return cls(**values)

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("subagents.enabled must be a boolean")
        if type(self.max_concurrent) is not int or self.max_concurrent < 1:
            raise ValueError("subagents.max_concurrent must be a positive integer")
        if self.permission != "allow":
            raise ValueError("subagents.permission must be allow")
        for name in ("model", "reasoning_effort"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"subagents.{name} must be a non-empty string or null")
        if self.reasoning_effort is None:
            object.__setattr__(self, "reasoning_effort", "default")
        if self.reasoning_effort == "auto":
            raise ValueError("subagents.reasoning_effort auto is not supported yet")
