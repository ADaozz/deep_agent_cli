"""Guardian-style auto-approval: evidence, policy, classification, and static rules."""

from agent.runtime.approval.classify import classify
from agent.runtime.approval.policy import REASONS, MANUAL, RiskAssessment, validate_assessment
from agent.runtime.approval.types import Decision

__all__ = [
    "Decision",
    "REASONS",
    "MANUAL",
    "RiskAssessment",
    "classify",
    "validate_assessment",
]
