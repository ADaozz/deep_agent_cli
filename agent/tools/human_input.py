# request_human_input — local LangGraph interrupt tool.
# The tool body only builds a Semantic Interaction Schema and calls interrupt().
# Resume hands the human's values back to the agent. No side effects before interrupt().
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import interrupt
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from agent.tools.human_interaction import (
    FIELD_TYPES,
    INTERACTION_TYPES,
    FieldType,
    InteractionType,
    normalize_interaction_request,
    resume_values,
)


REQUEST_TOOL_NAME = "request_human_input"


class FieldOption(BaseModel):
    """single_select / multi_select 的一条候选项。"""

    model_config = ConfigDict(extra="ignore")

    value: str = Field(
        default="",
        description="选项值，用户选中后按这个值回传",
        validation_alias=AliasChoices("value", "id"),
    )
    label: str = Field(default="", description="展示给用户的选项文案")
    description: str = Field(default="", description="选项补充说明，可留空")


class FieldSpec(BaseModel):
    """一个待用户填写的字段。只声明交互语义，不指定前端组件。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="", description="字段 id，用户的回答按这个 key 回传")
    type: FieldType = Field(default="text", description="字段类型")
    label: str = Field(default="", description="字段标题")
    required: bool = Field(default=False, description="是否必须回答")
    placeholder: str = Field(default="", description="输入框占位提示，可留空")
    options: list[FieldOption] = Field(
        default_factory=list,
        description="single_select / multi_select 的候选项，其他类型留空数组",
    )

    @field_validator("type", mode="before")
    @classmethod
    def _lenient_type(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        return text if text in FIELD_TYPES else "text"


class Recommendation(BaseModel):
    """推荐答案。value 对应某个 field 的选项值或建议填写的内容。"""

    model_config = ConfigDict(extra="ignore")

    value: str = Field(default="", description="推荐选中或填写的内容")
    reason: str = Field(default="", description="推荐理由，可留空")


class HumanInputArgs(BaseModel):
    """request_human_input 的参数。fields / recommendation / impact 必须是原生 JSON 结构。"""

    model_config = ConfigDict(extra="ignore")

    reason: str = Field(description="为什么必须由用户介入才能继续")
    question: str = Field(description="简短题干，不要把选项写成 Markdown 列表")
    interaction_type: InteractionType = Field(
        default="clarification", description="交互类型"
    )
    title: str = Field(default="", description="交互标题，可留空")
    fields: list[FieldSpec] = Field(
        default_factory=list,
        description="需要用户填写的字段；留空表示只有一个自由文本回复框",
    )
    recommendation: Recommendation | None = Field(
        default=None, description="推荐答案，没有明确推荐时整个省略"
    )
    impact: list[str] = Field(
        default_factory=list, description="这个决策会影响什么，每条一句；可留空"
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
        "请求用户提供继续执行所需的信息或决策，并暂停直到收到响应。"
        "当你要让用户从多个后续操作中选择时，必须调用本工具，不要只在普通回复中列出编号选项。"
        "只声明交互语义，不要指定前端组件。"
        "fields / recommendation / impact 必须传原生 JSON 结构，不要序列化成字符串。"
        "有可选项时必须用 single_select 或 multi_select，选项写进该字段的 options，"
        "不要把选项写成 question 里的 Markdown 列表。"
        "question 只保留简短题干。"
        "recommendation 与 impact 可选。这不是工具权限确认。"
    )
