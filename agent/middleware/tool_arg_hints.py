"""Turn tool argument validation failures into guidance the model can act on.

ToolNode reports pydantic errors verbatim ("Input should be a valid list"), which
does not tell the model *how* the payload was wrong. Models then resend the same
arguments with only the prose reworded, and the call fails identically. This
middleware re-validates the arguments on the error path and replaces the content
with the offending value, the expectation, and the parameter schema.
"""

from __future__ import annotations

import json
from typing import Any, Callable
from pydantic import BaseModel, ValidationError

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage


_MAX_INPUT_CHARS = 160
_MAX_SCHEMA_CHARS = 2500

_EXPECTATIONS = {
    "list_type": "must be a JSON array, not a serialized string",
    "dict_type": "must be a JSON object, not a serialized string",
    "string_type": "must be a string",
    "bool_type": "must be a boolean true or false",
    "int_type": "must be an integer",
    "float_type": "must be a number",
    "missing": "required; do not omit it",
    "literal_error": "value is outside the allowed enum; see the schema below",
    "enum": "value is outside the allowed enum; see the schema below",
    "string_pattern_mismatch": "string format does not match the schema below",
    "too_short": "value is too short; see the schema below",
    "too_long": "value exceeds the schema maxItems or maxLength; shorten it and call again",
    "model_attributes_type": "structure does not match the parameter schema below",
    "model_type": "must be a JSON object; see the schema below",
}


class ToolArgHintMiddleware(AgentMiddleware):
    """Rewrite argument validation errors so the model fixes the payload instead of resending it."""

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return _rewrite(request, handler(request))

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return _rewrite(request, await handler(request))


def _rewrite(request: Any, result: Any) -> Any:
    if not isinstance(result, ToolMessage) or result.status != "error":
        return result
    tool = getattr(request, "tool", None)
    schema = getattr(tool, "tool_call_schema", None)
    args = (getattr(request, "tool_call", None) or {}).get("args")
    if not isinstance(schema, type) or not issubclass(schema, BaseModel):
        return result
    if not isinstance(args, dict):
        return result
    try:
        schema.model_validate(args)
    except ValidationError as exc:
        content = _describe(str(getattr(tool, "name", "") or "tool"), exc, schema)
    else:
        # Arguments are valid, so the failure came from the tool body. Leave it alone.
        return result
    return ToolMessage(
        content=content,
        tool_call_id=result.tool_call_id,
        name=result.name,
        status="error",
    )


def _describe(tool_name: str, exc: ValidationError, schema: type[BaseModel]) -> str:
    lines = [
        f"Argument validation failed. `{tool_name}` did not run and produced no side effects.",
        "Correct the arguments below and call the tool again. Do not resend the same arguments.",
        "",
        "Errors:",
    ]
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc") or ()) or "(root)"
        value = error.get("input")
        expectation = _EXPECTATIONS.get(str(error.get("type") or ""))
        detail = f"- {loc}: received {type(value).__name__} ({error.get('msg')})"
        if expectation:
            detail += f" -> {expectation}"
        lines.append(detail)
        preview = _preview(value)
        if preview:
            lines.append(f"  received: {preview}")
    lines.extend(["", f"`{tool_name}` parameter schema:", _schema_text(schema)])
    return "\n".join(lines)


def _preview(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    text = text.replace("\n", " ")
    if len(text) > _MAX_INPUT_CHARS:
        return f"{text[:_MAX_INPUT_CHARS]}…"
    return text


def _schema_text(schema: type[BaseModel]) -> str:
    doc = schema.model_json_schema()
    doc.pop("description", None)
    text = json.dumps(_without_titles(doc), ensure_ascii=False, separators=(",", ":"))
    if len(text) > _MAX_SCHEMA_CHARS:
        return f"{text[:_MAX_SCHEMA_CHARS]}… (schema truncated)"
    return text


def _without_titles(node: Any) -> Any:
    # pydantic 给每个字段生成的 "title" 元数据对模型没有信息量，只会挤占提示预算。
    # properties / $defs 下面的 key 是参数名和模型名，必须原样保留。
    if isinstance(node, dict):
        kept: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("properties", "$defs", "definitions"):
                kept[key] = {
                    name: _without_titles(item) for name, item in value.items()
                } if isinstance(value, dict) else value
            elif key != "title":
                kept[key] = _without_titles(value)
        return kept
    if isinstance(node, list):
        return [_without_titles(item) for item in node]
    return node
