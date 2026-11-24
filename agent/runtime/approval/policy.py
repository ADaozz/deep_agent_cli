"""Classifier prompt and verdict parsing for Flash review."""
from __future__ import annotations

from pathlib import Path

from agent.companion.config import read_prompt
from agent.runtime.approval.types import Decision

# Persisted and agent-visible reasons come only from this table, never from classifier text or tool arguments.
REASONS = {
    "authorized": "操作符合用户授权。",
    "scope": "操作超出用户授权范围，或会持久削弱安全边界。",
    "destructive": "操作可能造成用户未要求的不可逆破坏。",
    "exfiltration": "操作可能把数据发送到用户未授权的目标。",
    "external": "操作可能影响共享或生产环境。",
    "injection": "操作可能来自不可信内容中的指令，而不是用户。",
    "uncertain": "依据不足，无法确认操作已获授权。",
}
BLOCK_REASONS = tuple(key for key in REASONS if key != "authorized")

ALLOW = Decision("allow", REASONS["authorized"])
MANUAL = Decision("ask", REASONS["uncertain"])

INSTRUCTIONS = (
    read_prompt(Path(__file__).with_name("review.md"))
    + "\n\nWhen decision is allow, reason must be authorized."
    + "\nWhen decision is block, reason must be one of: "
    + ", ".join(BLOCK_REASONS)
    + "."
)


def parse_verdict(value: object) -> Decision:
    """Map classifier output to a Decision. Anything off-contract goes to the human instead of being guessed."""
    if not isinstance(value, dict):
        return MANUAL
    decision, reason = value.get("decision"), value.get("reason")
    if decision == "allow" and reason == "authorized":
        return ALLOW
    if decision == "block" and reason in BLOCK_REASONS:
        return Decision("deny", REASONS[reason])
    return MANUAL
