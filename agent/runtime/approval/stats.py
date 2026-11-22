"""Lightweight aggregate counters for auto-approval (no secrets or raw prompts)."""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock


@dataclass
class ApprovalStats:
    static_allow: int = 0
    static_ask: int = 0
    static_deny: int = 0
    flash_reviews: int = 0
    flash_allow: int = 0
    flash_ask: int = 0
    flash_deny: int = 0
    flash_timeouts: int = 0
    fingerprint_stale: int = 0
    risk_levels: dict[str, int] = field(default_factory=dict)
    review_ms_total: float = 0.0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def record_static(self, decision: str) -> None:
        with self._lock:
            if decision == "allow":
                self.static_allow += 1
            elif decision == "deny":
                self.static_deny += 1
            else:
                self.static_ask += 1

    def record_flash(self, decision: str, elapsed_seconds: float) -> None:
        with self._lock:
            self.flash_reviews += 1
            self.review_ms_total += max(elapsed_seconds, 0) * 1000
            if decision == "allow":
                self.flash_allow += 1
            elif decision == "deny":
                self.flash_deny += 1
            else:
                self.flash_ask += 1

    def record_timeout(self) -> None:
        with self._lock:
            self.flash_timeouts += 1
            self.flash_ask += 1
            self.flash_reviews += 1

    def record_risk(self, level: str) -> None:
        with self._lock:
            self.risk_levels[level] = self.risk_levels.get(level, 0) + 1

    def record_stale(self) -> None:
        with self._lock:
            self.fingerprint_stale += 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "static_allow": self.static_allow,
                "static_ask": self.static_ask,
                "static_deny": self.static_deny,
                "flash_reviews": self.flash_reviews,
                "flash_allow": self.flash_allow,
                "flash_ask": self.flash_ask,
                "flash_deny": self.flash_deny,
                "flash_timeouts": self.flash_timeouts,
                "fingerprint_stale": self.fingerprint_stale,
                "risk_levels": dict(self.risk_levels),
                "review_ms_total": self.review_ms_total,
            }


approval_stats = ApprovalStats()
