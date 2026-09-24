# Semantic Human Interaction Schema — Agent declares what information it needs, not UI widgets.
from __future__ import annotations

from typing import Any, Literal, get_args

FieldType = Literal["text", "textarea", "single_select", "multi_select", "boolean"]
InteractionType = Literal["clarification", "decision", "confirmation", "review"]

FIELD_TYPES: tuple[str, ...] = tuple(get_args(FieldType))
INTERACTION_TYPES: tuple[str, ...] = tuple(get_args(InteractionType))


def normalize_interaction_request(
    reason: str,
    question: str,
    interaction_type: str | None = None,
    title: str | None = None,
    fields: list | None = None,
    recommendation: dict | None = None,
    impact: list | None = None,
    interaction_id: str | None = None,
) -> dict[str, Any]:
    kind = (interaction_type or "clarification").strip().lower()
    if kind not in INTERACTION_TYPES:
        kind = "clarification"
    normalized_fields = _normalize_fields(fields)
    payload: dict[str, Any] = {
        "type": "human_input",
        "interactionType": kind,
        "title": (title or "").strip(),
        "reason": reason or "",
        "question": question or "",
        "fields": normalized_fields,
        "blocking": True,
        "impact": [str(item) for item in impact] if isinstance(impact, list) else [],
    }
    if interaction_id:
        payload["interactionId"] = interaction_id
    if isinstance(recommendation, dict) and recommendation:
        payload["recommendation"] = {
            "value": str(recommendation.get("value") or ""),
            "reason": str(recommendation.get("reason") or ""),
        }
    return payload


def resume_values(answer: Any) -> dict[str, Any]:
    if not isinstance(answer, dict) or answer.get("type") != "human_input":
        raise ValueError("Human input response must have type human_input")
    values = answer.get("values") if isinstance(answer, dict) else None
    if not isinstance(values, dict):
        raise ValueError("Human input response must contain a values object")
    return dict(values)


def _normalize_fields(fields: list | None) -> list[dict[str, Any]]:
    if isinstance(fields, list) and fields:
        return [_normalize_field(item, index) for index, item in enumerate(fields)]
    return [{
        "id": "text",
        "type": "text",
        "label": "回复",
        "required": True,
        "placeholder": "输入回复…",
        "options": [],
    }]


def _normalize_field(item: Any, index: int) -> dict[str, Any]:
    raw = item if isinstance(item, dict) else {"label": str(item)}
    field_type = str(raw.get("type") or "text").strip().lower()
    if field_type not in FIELD_TYPES:
        field_type = "text"
    field_id = str(raw.get("id") or f"field_{index}").strip() or f"field_{index}"
    return {
        "id": field_id,
        "type": field_type,
        "label": str(raw.get("label") or field_id),
        "required": bool(raw.get("required")),
        "placeholder": str(raw.get("placeholder") or ""),
        "options": _normalize_options(raw.get("options")),
    }


def _normalize_options(options: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if not isinstance(options, list):
        return result
    for item in options:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or item.get("id") or "").strip()
        label = str(item.get("label") or value).strip()
        if not value or not label:
            continue
        result.append({
            "value": value,
            "label": label,
            "description": str(item.get("description") or ""),
        })
    return result
