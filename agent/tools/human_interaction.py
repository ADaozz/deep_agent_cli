# Semantic Human Interaction Schema — Agent declares what information it needs, not UI widgets.
from __future__ import annotations

import re
from typing import Any

FIELD_TYPES = ("text", "textarea", "single_select", "multi_select", "boolean")
INTERACTION_TYPES = ("clarification", "decision", "confirmation", "review")
_HEADING_BOLD = re.compile(r"^\*\*(\d+)[.、.)]\s*(.+?)\*\*\s*(.*)$")
_HEADING_PLAIN = re.compile(r"^(?:#{1,3}\s*)?(\d+)[.、.)]\s+(.+)$")
_BULLET = re.compile(r"^\s*[-*•]\s+(.+)$")
_MD = re.compile(r"[*_`]+")


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
    blocking: bool = True,
    interaction_id: str | None = None,
) -> dict[str, Any]:
    kind = (interaction_type or "clarification").strip().lower()
    if kind not in INTERACTION_TYPES:
        kind = "clarification"
    normalized_fields = _normalize_fields(fields, options, required_input)
    if _unstructured_text_fields(normalized_fields):
        extracted = fields_from_question(question)
        if extracted:
            normalized_fields = extracted
            question = question_intro(question) or question
    payload: dict[str, Any] = {
        "type": "human_input",
        "interactionType": kind,
        "title": (title or "").strip(),
        "reason": reason or "",
        "question": question or "",
        "fields": normalized_fields,
        "blocking": bool(blocking),
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


def fields_from_question(question: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    closing: list[str] = []
    for raw in (question or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        headed = _HEADING_BOLD.match(line) or _HEADING_PLAIN.match(line)
        if headed:
            current = {
                "title": _strip_md(headed.group(2) or ""),
                "hint": _strip_md(headed.group(3) if headed.lastindex and headed.lastindex >= 3 else ""),
                "bullets": [],
            }
            sections.append(current)
            continue
        item = _BULLET.match(line)
        if item and current is not None:
            current["bullets"].append(_strip_md(item.group(1) or ""))
            continue
        if current is not None:
            closing.append(_strip_md(line))
    fields: list[dict[str, Any]] = []
    for index, section in enumerate(sections, start=1):
        bullets = [str(item) for item in section["bullets"]]
        options = [item for item in bullets if not _is_other(item) and not _is_question_bullet(item)]
        questions = [item for item in bullets if _is_question_bullet(item)]
        field_id = f"q{index}"
        label = str(section["title"] or f"问题 {index}")
        hint = str(section.get("hint") or "")
        if len(options) >= 2:
            fields.append({
                "id": field_id,
                "type": _select_type(label, options),
                "label": f"{label} {hint}".strip(),
                "required": True,
                "placeholder": "",
                "options": [
                    {"value": _slug(item, f"{field_id}_{n}"), "label": item, "description": ""}
                    for n, item in enumerate(options, start=1)
                ],
            })
        elif questions:
            fields.append({
                "id": field_id,
                "type": "textarea",
                "label": label,
                "required": False,
                "placeholder": " ".join(questions),
                "options": [],
            })
    if any(field["type"] in {"single_select", "multi_select"} for field in fields):
        fields.append({
            "id": "comment",
            "type": "textarea",
            "label": "补充说明",
            "required": False,
            "placeholder": " ".join(closing) or "补充说明（可选）",
            "options": [],
        })
    return fields


def question_intro(question: str) -> str:
    intro: list[str] = []
    for raw in (question or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _HEADING_BOLD.match(line) or _HEADING_PLAIN.match(line):
            break
        intro.append(_strip_md(line))
    return "\n".join(intro)


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


def _strip_md(value: str) -> str:
    return _MD.sub("", value).strip()


def _is_other(label: str) -> bool:
    return bool(re.match(r"^(其他|其它|other)\b", label, re.I))


def _is_question_bullet(label: str) -> bool:
    return bool(re.search(r"[?？]\s*$", label) or re.match(r"^(是否|有没有|需要)", label))


def _select_type(title: str, options: list[str]) -> str:
    if re.search(r"场景|形式|部署|方式|还是|或者|单选", title):
        return "single_select"
    if re.search(r"功能|能力|范围|包含|哪些|多选", title):
        return "multi_select"
    if any(re.search(r"两者|都需要|以上都是", item) for item in options):
        return "single_select"
    return "multi_select" if len(options) >= 4 else "single_select"


def _slug(label: str, fallback: str) -> str:
    compact = re.sub(r"（.*?）|\(.*?\)", "", label)
    compact = re.sub(r"[^\w]+", "_", compact, flags=re.UNICODE).strip("_")[:48]
    return compact or fallback
