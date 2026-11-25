from __future__ import annotations

from typing import Any, Literal, get_args

from agent.secrets.store import SecretScope, validate_secret_name, validate_secret_scope

# Semantic Human Interaction Schema — Agent declares what information it needs, not UI widgets.


FieldType = Literal["text", "textarea", "single_select", "multi_select", "boolean", "secret"]
InteractionType = Literal["clarification", "decision", "confirmation", "review"]
InteractionSource = Literal["agent_input", "runtime_auth"]

FIELD_TYPES: tuple[str, ...] = tuple(get_args(FieldType))
INTERACTION_TYPES: tuple[str, ...] = tuple(get_args(InteractionType))
_SECRET_TYPE_KEYWORDS: tuple[str, ...] = (
    "secret", "password", "passwd", "credential", "api_key", "apikey", "token", "passphrase", "private_key",
)


def is_rejected_secret_type(raw_type: str) -> bool:
    """True when an unknown field type looks like a secret alias that must never degrade to 'text'."""
    lowered = raw_type.strip().lower()
    if not lowered or lowered in FIELD_TYPES:
        return False
    return any(keyword in lowered for keyword in _SECRET_TYPE_KEYWORDS)


def normalize_interaction_request(
    reason: str,
    question: str,
    interaction_type: str | None = None,
    title: str | None = None,
    fields: list | None = None,
    recommendation: dict | None = None,
    impact: list | None = None,
    interaction_id: str | None = None,
    *,
    source: InteractionSource = "agent_input",
) -> dict[str, Any]:
    if source not in {"agent_input", "runtime_auth"}:
        raise ValueError(f"Unsupported interaction source: {source!r}")
    kind = (interaction_type or "clarification").strip().lower()
    if kind not in INTERACTION_TYPES:
        kind = "clarification"
    normalized_fields = _normalize_fields(fields, source=source)
    payload: dict[str, Any] = {
        "type": "human_input",
        "source": source,
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


def build_runtime_auth_request(
    *,
    interaction_id: str,
    title: str = "Authentication required",
    question: str = "Enter your sudo password to continue.",
    reason: str = "",
    label: str = "sudo Password",
) -> dict[str, Any]:
    """Trusted host-only constructor for one-time runtime authentication."""
    if not interaction_id:
        raise ValueError("runtime_auth requires an interaction_id")
    return normalize_interaction_request(
        reason=reason,
        question=question,
        interaction_type="confirmation",
        title=title,
        fields=[{
            "id": "password",
            "type": "secret",
            "label": label,
            "required": True,
        }],
        interaction_id=interaction_id,
        source="runtime_auth",
    )


def validate_human_input_values(
    values: Any,
    payload: dict[str, Any],
    *,
    allow_omitted_secrets: bool = False,
) -> None:
    """Validate against the pending server-side declaration without changing the answer."""
    if not isinstance(values, dict):
        raise ValueError("Human input response must contain a values object")
    for field in payload.get("fields") or []:
        field_type = field.get("type")
        if field_type == "secret" and allow_omitted_secrets:
            continue
        if not field.get("required"):
            continue
        value = values.get(field["id"])
        if field_type == "secret":
            if value is None or (isinstance(value, str) and value == ""):
                raise ValueError("Required secret fields must not be empty")
            continue
        if value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, list) and not value):
            raise ValueError("Required human input fields must not be empty")


def resume_values(answer: Any) -> dict[str, Any]:
    if not isinstance(answer, dict) or answer.get("type") != "human_input":
        raise ValueError("Human input response must have type human_input")
    values = answer.get("values") if isinstance(answer, dict) else None
    if not isinstance(values, dict):
        raise ValueError("Human input response must contain a values object")
    return dict(values)


def _normalize_fields(
    fields: list | None,
    *,
    source: InteractionSource = "agent_input",
) -> list[dict[str, Any]]:
    if isinstance(fields, list) and fields:
        normalized = [_normalize_field(item, index, source=source) for index, item in enumerate(fields)]
        seen_ids: set[str] = set()
        seen_secrets: set[str] = set()
        for item in normalized:
            fid = item["id"]
            if fid in seen_ids:
                raise ValueError(f"Duplicate human input field id: {fid!r}")
            seen_ids.add(fid)
            secret_name = item.get("secret_name")
            if secret_name:
                if secret_name in seen_secrets:
                    raise ValueError(f"Duplicate secret_name in one request: {secret_name!r}")
                seen_secrets.add(secret_name)
        return normalized
    return [{
        "id": "text",
        "type": "text",
        "label": "回复",
        "required": True,
        "placeholder": "输入回复…",
        "options": [],
    }]


def _normalize_field(
    item: Any,
    index: int,
    *,
    source: InteractionSource = "agent_input",
) -> dict[str, Any]:
    raw = item if isinstance(item, dict) else {"label": str(item)}
    raw_type = str(raw.get("type") or "text").strip().lower()
    has_secret_attrs = any(
        raw.get(key) not in (None, "")
        for key in ("secret_name", "secret_scope")
    )
    if is_rejected_secret_type(raw_type):
        raise ValueError(
            f"Unsupported secret field type {raw_type!r}: use type='secret' with secret_name and secret_scope"
        )
    if has_secret_attrs and raw_type != "secret":
        raise ValueError("Fields declaring secret_name or secret_scope must set type='secret'")
    field_type = raw_type if raw_type in FIELD_TYPES else "text"
    field_id = str(raw.get("id") or f"field_{index}").strip() or f"field_{index}"
    options = _normalize_options(raw.get("options"))
    if field_type == "secret":
        if raw.get("options"):
            raise ValueError("Secret fields cannot declare options")
        result: dict[str, Any] = {
            "id": field_id,
            "type": "secret",
            "label": str(raw.get("label") or field_id),
            "required": bool(raw.get("required", True if source == "runtime_auth" else False)),
            "placeholder": str(raw.get("placeholder") or ""),
            "options": [],
        }
        if source == "agent_input":
            secret_name = validate_secret_name(raw.get("secret_name"))
            secret_scope: SecretScope = validate_secret_scope(raw.get("secret_scope"))
            result["secret_name"] = secret_name
            result["secret_scope"] = secret_scope
        else:
            if raw.get("secret_name"):
                result["secret_name"] = validate_secret_name(raw.get("secret_name"))
            if raw.get("secret_scope"):
                result["secret_scope"] = validate_secret_scope(raw.get("secret_scope"))
        return result
    if field_type in {"single_select", "multi_select"}:
        options = ensure_other_option(options)
    return {
        "id": field_id,
        "type": field_type,
        "label": str(raw.get("label") or field_id),
        "required": bool(raw.get("required")),
        "placeholder": str(raw.get("placeholder") or ""),
        "options": options,
    }


def _normalize_options(options: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(options, list):
        return result
    for item in options:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or "").strip()
        label = str(item.get("label") or value).strip()
        if not value or not label:
            continue
        result.append({
            "value": value,
            "label": label,
            "description": str(item.get("description") or ""),
            **({"allowsCustomText": True} if item.get("allowsCustomText") else {}),
        })
    return result


def ensure_other_option(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose one custom answer choice without colliding with supplied values."""
    result = [dict(option) for option in options if not (
        option.get("allowsCustomText") or
        str(option.get("label", "")).strip().lower() in {"other", "other:", "其他", "其他：", "其它", "其它："}
    )]
    used = {str(option.get("value")) for option in result}
    marker = "__other__"
    while marker in used:
        marker += "_"
    result.append({"value": marker, "label": "Other:", "description": "", "allowsCustomText": True})
    return result
