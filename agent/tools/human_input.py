from __future__ import annotations

import json
from typing import Any
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import interrupt

from agent.secrets.store import SecretScope, validate_secret_name, validate_secret_scope
from agent.tools.human_interaction import (
    FIELD_TYPES,
    INTERACTION_TYPES,
    FieldType,
    InteractionType,
    is_rejected_secret_type,
    normalize_interaction_request,
    resume_values,
)

# request_human_input — local LangGraph interrupt tool.
# The tool body only builds a Semantic Interaction Schema and calls interrupt().
# Resume hands the human's values back to the agent. No side effects before interrupt().


REQUEST_TOOL_NAME = "request_human_input"


class FieldOption(BaseModel):
    """single_select / multi_select 的一条候选项。"""

    model_config = ConfigDict(extra="ignore")

    value: str = Field(description="Value returned when selected.")
    label: str = Field(
        default="",
        description="Objective action/option label shown to the user; no 1st/2nd person ('I will', 'You', '我来', '你去').",
    )
    description: str = Field(default="", description="Optional brief detail beyond label.")


class FieldSpec(BaseModel):
    """一个待用户填写的字段。只声明交互语义，不指定前端组件。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="", description="Field id returned in answer values.")
    type: FieldType = Field(default="text", description="Field type. Use 'secret' for sensitive credentials.")
    label: str = Field(default="", description="Short field/credential name; do not repeat title or question.")
    required: bool = Field(default=False, description="Whether an answer is required.")
    placeholder: str = Field(default="", description="Optional input placeholder.")
    secret_name: str | None = Field(
        default=None,
        description="Required for secret: reference name returned (e.g. GITHUB_TOKEN), never the value.",
    )
    secret_scope: SecretScope | None = Field(
        default=None,
        description="Required for secret: 'host' (e.g. MCP) or 'sandbox' (command env).",
    )
    options: list[FieldOption] = Field(
        default_factory=list,
        max_length=3,
        description="At most 3 objective choices (no 1st/2nd person). System adds Other automatically.",
    )

    @field_validator("type", mode="before")
    @classmethod
    def _lenient_type(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        if is_rejected_secret_type(text):
            raise ValueError(
                f"Unsupported secret field type {text!r}: use type='secret' with secret_name and secret_scope"
            )
        return text if text in FIELD_TYPES else "text"

    @model_validator(mode="after")
    def _validate_secret_declaration(self) -> "FieldSpec":
        has_secret_attrs = self.secret_name not in (None, "") or self.secret_scope not in (None, "")
        if has_secret_attrs and self.type != "secret":
            raise ValueError("secret_name and secret_scope require type='secret'")
        if self.type == "secret":
            if self.options:
                raise ValueError("Secret fields cannot declare options")
            self.secret_name = validate_secret_name(self.secret_name)
            self.secret_scope = validate_secret_scope(self.secret_scope)
        return self


class Recommendation(BaseModel):
    """推荐答案。value 对应某个 field 的选项值或建议填写的内容。"""

    model_config = ConfigDict(extra="ignore")

    value: str = Field(default="", description="Recommended selection or text.")
    reason: str = Field(default="", description="Optional reason for the recommendation.")


class HumanInputArgs(BaseModel):
    """request_human_input 的参数。fields / recommendation / impact 必须是原生 JSON 结构。"""

    model_config = ConfigDict(extra="ignore")

    reason: str = Field(description="Brief necessary context; do not repeat title or question.")
    question: str = Field(
        description="Short direct question in the conversation language (for secrets, name service and operation). Do not list options."
    )
    interaction_type: InteractionType = Field(
        default="clarification", description="Interaction type."
    )
    title: str = Field(default="", description="Optional short heading; do not repeat question.")
    fields: list[FieldSpec] = Field(
        default_factory=list,
        description="Fields for the user. Empty means one free-text reply.",
    )
    recommendation: Recommendation | None = Field(
        default=None,
        description="Recommended choice and optional brief reason shown beside that option.",
    )
    impact: list[str] = Field(
        default_factory=list, description="Actual effects of this decision, one short sentence each. Optional."
    )

    @field_validator("interaction_type", mode="before")
    @classmethod
    def _lenient_interaction_type(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        return text if text in INTERACTION_TYPES else "clarification"

    @field_validator("fields", "recommendation", "impact", mode="before")
    @classmethod
    def _decode_json_text(cls, value: Any) -> Any:
        # 模型和网关有时会把嵌套参数整体 json.dumps 成字符串；解析失败时原样返回，
        # 交给 pydantic 报错，再由 ToolArgHintMiddleware 转成可操作的提示。
        return _decode_json_text(value)


def build_human_input_tools() -> list[BaseTool]:
    return [_build_tool(REQUEST_TOOL_NAME, _request_description())]


def _build_tool(name: str, description: str) -> BaseTool:
    def request_human_input(
        reason: str,
        question: str,
        interaction_type: InteractionType = "clarification",
        title: str = "",
        fields: list[FieldSpec] | None = None,
        recommendation: Recommendation | None = None,
        impact: list[str] | None = None,
    ) -> str:
        payload = normalize_interaction_request(
            reason=reason,
            question=question,
            interaction_type=interaction_type,
            title=title,
            fields=[_plain(field) for field in fields] if fields is not None else None,
            recommendation=_plain(recommendation),
            impact=list(impact) if impact is not None else None,
            interaction_id=str(uuid4()),
        )
        answer = interrupt(payload)
        values = resume_values(answer)
        return json.dumps(values, ensure_ascii=False)

    return StructuredTool.from_function(
        func=request_human_input,
        name=name,
        description=description,
        args_schema=HumanInputArgs,
    )


def _decode_json_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _plain(value: Any) -> Any:
    return value.model_dump() if isinstance(value, BaseModel) else value


def _request_description() -> str:
    return (
        "Request user input only when missing information or a genuine user decision "
        "prevents meaningful progress. Never use for tool permission approvals. "
        "Write concise, natural wording in the current conversation language without repeating "
        "the same content across title, reason, question, label, and description, and never repeat "
        "field options inside question. "
        "Use objective action, choice, or outcome labels for options (e.g. 'Automatic configuration', "
        "'Manual configuration', 'Keep existing settings', '自动安装依赖', '跳过安装'); "
        "never use first- or second-person phrasing ('I will...', 'You...', 'Let me...', '我来...', '你去...'). "
        "For sensitive credentials, always use type='secret' with secret_name and secret_scope, "
        "and clearly state what credential is needed, which service it is for, and what operation it enables. "
        "Only the reference name is returned, never the secret value. "
        "Never ask the user to provide credentials as plain text. "
        "Pass nested arguments as native JSON values, not serialized strings."
    )
