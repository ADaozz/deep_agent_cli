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

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, ValidationError


_MAX_INPUT_CHARS = 160
_MAX_SCHEMA_CHARS = 2500

_EXPECTATIONS = {
    "list_type": "必须是原生 JSON 数组，不要把结构序列化成字符串",
    "dict_type": "必须是原生 JSON 对象，不要把结构序列化成字符串",
    "string_type": "必须是字符串",
    "bool_type": "必须是布尔值 true / false",
    "int_type": "必须是整数",
    "float_type": "必须是数字",
    "missing": "必填参数，不能省略",
    "literal_error": "取值不在允许的枚举内，见下方 schema",
    "enum": "取值不在允许的枚举内，见下方 schema",
    "string_pattern_mismatch": "字符串格式不符合要求，见下方 schema",
    "too_short": "长度不足，见下方 schema",
    "model_attributes_type": "结构不符合该参数的 schema，见下方 schema",
    "model_type": "必须是 JSON 对象，见下方 schema",
}


class ToolArgHintMiddleware(AgentMiddleware):
    """Rewrite argument validation errors so the model fixes the payload instead of resending it."""

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return _rewrite(request, handler(request))


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
        f"参数校验失败，`{tool_name}` 没有执行，也没有产生任何副作用。",
        "请按下面的说明修正参数后重新调用，不要原样重发同一份参数。",
        "",
        "错误：",
    ]
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc") or ()) or "(root)"
        value = error.get("input")
        expectation = _EXPECTATIONS.get(str(error.get("type") or ""))
        detail = f"- {loc}: 收到 {type(value).__name__}（{error.get('msg')}）"
        if expectation:
            detail += f" → {expectation}"
        lines.append(detail)
        preview = _preview(value)
        if preview:
            lines.append(f"  收到内容：{preview}")
    lines.extend(["", f"`{tool_name}` 的参数 schema：", _schema_text(schema)])
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
        return f"{text[:_MAX_SCHEMA_CHARS]}…（schema 已截断）"
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
