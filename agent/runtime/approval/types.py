"""Shared approval decision types."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    decision: str
    reason: str
