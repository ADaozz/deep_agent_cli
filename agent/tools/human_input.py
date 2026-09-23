# request_human_input / handoff_to_human — local LangGraph interrupt tools.
# The tool body only builds a Semantic Interaction Schema and calls interrupt().
# Resume hands the human's values back to the agent. No side effects before interrupt().
from __future__ import annotations

import json
from uuid import uuid4

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import interrupt

from agent.tools.human_interaction import normalize_interaction_request, resume_values


HANDOFF_TOOL_NAME = "handoff_to_human"
REQUEST_TOOL_NAME = "request_human_input"


def build_handoff_tool() -> BaseTool:
    return _build_tool(HANDOFF_TOOL_NAME, _legacy_description())


def build_human_input_tools() -> list[BaseTool]:
    return [
        _build_tool(HANDOFF_TOOL_NAME, _legacy_description()),
        _build_tool(REQUEST_TOOL_NAME, _request_description()),
    ]


def _build_tool(name: str, description: str) -> BaseTool:
    def request_human_input(
        reason: str,
        question: str,
        required_input: str = "",
        options: list | None = None,
        interaction_type: str = "clarification",
        title: str = "",
        fields: list | None = None,
        recommendation: dict | None = None,
        impact: list | None = None,
    ) -> str:
        payload = normalize_interaction_request(
            reason=reason,
            question=question,
            required_input=required_input,
            options=options,
            interaction_type=interaction_type,
            title=title,
            fields=fields,
            recommendation=recommendation,
            impact=impact,
            interaction_id=str(uuid4()),
        )
        answer = interrupt(payload)
        values = resume_values(answer)
        return json.dumps(values, ensure_ascii=False)

    return StructuredTool.from_function(
        func=request_human_input,
        name=name,
        description=description,
    )


def _legacy_description() -> str:
    return (
        "当缺少业务判断或关键信息、无法继续时转交人工。"
        "reason 说明为何无法继续；question 只写一句问题，不要把选项写进 question。"
        "用户需要选择时必须传 fields：single_select 或 multi_select，options 为 {value,label}。"
        "禁止只用一个 text 字段再把问卷塞进 question。"
        "也可传 interaction_type、recommendation、impact。调用后会暂停，直到人类回复。"
    )


def _request_description() -> str:
    return (
        "请求用户提供继续执行所需的信息或决策，并暂停直到收到响应。"
        "只声明交互语义，不要指定前端组件。"
        "interaction_type: clarification | decision | confirmation | review。"
        "fields 类型仅限 text、textarea、single_select、multi_select、boolean。"
        "有可选项时必须用 single_select 或 multi_select，每个选项一条 options，不要把选项写成 question 里的 Markdown 列表。"
        "question 只保留简短题干。"
        "recommendation 与 impact 可选。这不是工具权限确认。"
    )
