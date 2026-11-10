"""Current and requested run configuration, published at model safe points."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock

from agent.models.profile import ModelProfile
from agent.execution.policy import SandboxMode, parse_sandbox_mode
from agent.execution.types import ExecutionMode
from agent.runtime.permissions import (
    PermissionMode,
    allow_mode_available,
    allow_mode_unavailable_reason,
    parse_permission_mode,
)


@dataclass
class RuntimeConfiguration:
    current_model_id: str = ""
    permission_mode: PermissionMode = PermissionMode.ASK
    reasoning_effort: str = "default"
    sandbox_mode: SandboxMode = "restricted"
    pending_model_id: str | None = None
    pending_permission_mode: PermissionMode | None = None
    pending_reasoning_effort: str | None = None
    pending_sandbox_mode: SandboxMode | None = None
    lock: RLock = field(default_factory=RLock, repr=False, compare=False)

    def has_pending(self) -> bool:
        with self.lock:
            return any(value is not None for value in (
                self.pending_model_id, self.pending_permission_mode,
                self.pending_reasoning_effort, self.pending_sandbox_mode,
            ))

    def request_model(self, profile: ModelProfile, reasoning_effort: str | None = None) -> None:
        with self.lock:
            effort = reasoning_effort if reasoning_effort is not None else (
                self.reasoning_effort if profile.id == self.current_model_id else "default"
            )
            if effort != "default" and effort not in profile.reasoning_efforts:
                raise ValueError(f"Unsupported reasoning effort for {profile.id}: {effort}")
            self.pending_model_id = profile.id if profile.id != self.current_model_id else None
            self.pending_reasoning_effort = effort if (
                self.pending_model_id is not None or effort != self.reasoning_effort
            ) else None

    def request_permission(self, mode: PermissionMode | str, execution: ExecutionMode) -> PermissionMode:
        if isinstance(mode, str):
            parsed = parse_permission_mode(mode)
            if parsed is None:
                raise ValueError(f"Unknown permission mode: {mode}")
            mode = parsed
        if mode is PermissionMode.ALLOW and not allow_mode_available(execution):
            raise ValueError(allow_mode_unavailable_reason(execution))
        with self.lock:
            self.pending_permission_mode = mode if mode is not self.permission_mode else None
        return mode

    def request_sandbox(self, mode: str) -> SandboxMode:
        parsed = parse_sandbox_mode(mode)
        if parsed is None:
            raise ValueError("sandbox mode must be restricted or full-access")
        with self.lock:
            self.pending_sandbox_mode = None if parsed == self.sandbox_mode else parsed
        return parsed
