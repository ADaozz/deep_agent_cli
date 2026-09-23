# Semantic Human Interaction Schema — Agent declares what information it needs, not UI widgets.
from __future__ import annotations

from typing import Any

FIELD_TYPES = ("text", "textarea", "single_select", "multi_select", "boolean")
INTERACTION_TYPES = ("clarification", "decision", "confirmation", "review")

def normalize_interaction_request(
    reason: str,
    question: str,
    required_input: str = "",
    options: list | None = None,
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
    normalized_fields = _normalize_fields(fields, options, required_input)
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
    legacy_options = options if isinstance(options, list) else _legacy_options(normalized_fields)
    if legacy_options:
        payload["options"] = legacy_options
    if required_input:
        payload["required_input"] = required_input
    return payload


def resume_values(answer: Any) -> dict[str, Any]:
    if isinstance(answer, dict):
        values = answer.get("values")
        if isinstance(values, dict):
            return dict(values)
        if any(key in answer for key in ("text", "input", "message", "optionId", "option_id")):
            result: dict[str, Any] = {}
            if answer.get("optionId") or answer.get("option_id"):
                result["choice"] = answer.get("optionId") or answer.get("option_id")
            if answer.get("text") or answer.get("input") or answer.get("message"):
                result["text"] = answer.get("text") or answer.get("input") or answer.get("message")
            leftover = {
                key: value for key, value in answer.items()
                if key not in {"type", "text", "input", "message", "optionId", "option_id", "interactionId", "values"}
            }
            result.update(leftover)
            return result
        return dict(answer)
    if answer is None:
        return {}
    return {"text": str(answer)}


def _normalize_fields(fields: list | None, options: list | None, required_input: str) -> list[dict[str, Any]]:
    if isinstance(fields, list) and fields:
        return [_normalize_field(item, index) for index, item in enumerate(fields)]
    result: list[dict[str, Any]] = []
    legacy = _normalize_options(options)
    if legacy:
        result.append({
            "id": "choice",
            "type": "single_select",
            "label": "请选择",
            "required": True,
            "placeholder": "",
            "options": legacy,
        })
        result.append({
            "id": "comment",
            "type": "textarea",
            "label": "补充说明",
            "required": False,
            "placeholder": required_input or "补充说明（可选）",
            "options": [],
        })
        return result
    result.append({
        "id": "text",
        "type": "textarea" if required_input else "text",
        "label": "回复",
        "required": True,
        "placeholder": required_input or "输入回复…",
        "options": [],
    })
    return result


def _unstructured_text_fields(fields: list[dict[str, Any]]) -> bool:
    if not fields:
        return True
    return all(
        field.get("type") in {"text", "textarea"} and not field.get("options")
        for field in fields
    )


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


def _legacy_options(fields: list[dict[str, Any]]) -> list[dict[str, str]]:
    for field in fields:
        if field.get("type") == "single_select" and field.get("options"):
            return [
                {"id": option["value"], "label": option["label"], "description": option.get("description", "")}
                for option in field["options"]
            ]
    return []
