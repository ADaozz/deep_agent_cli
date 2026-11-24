"""Auto approval: static rules, review evidence, and the independent Flash reviewer."""

from agent.runtime.approval.classify import classify
from agent.runtime.approval.policy import MANUAL, REASONS, parse_verdict
from agent.runtime.approval.rules import static_rule
from agent.runtime.approval.types import Decision

__all__ = [
    "Decision",
    "MANUAL",
    "REASONS",
    "classify",
    "parse_verdict",
    "static_rule",
]
