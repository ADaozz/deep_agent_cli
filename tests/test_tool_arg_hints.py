from langchain_core.messages import ToolMessage

from agent.middleware.tool_arg_hints import ToolArgHintMiddleware
from agent.tools.human_input import build_human_input_tools


class _Request:
    def __init__(self, tool: object, args: dict) -> None:
        self.tool = tool
        self.tool_call = {"name": getattr(tool, "name", "tool"), "id": "call-1", "args": args}


def _error_message(tool: object) -> ToolMessage:
    return ToolMessage(
        content="Error invoking tool with error:\nfields: Input should be a valid list\n"
        " Please fix the error and try again.",
        tool_call_id="call-1",
        name=str(getattr(tool, "name", "tool")),
        status="error",
    )


def test_validation_error_becomes_actionable_hint() -> None:
    tool = build_human_input_tools()[0]
    request = _Request(tool, {"reason": "r", "question": "q", "fields": "五个字段，见上文"})
    result = ToolArgHintMiddleware().wrap_tool_call(request, lambda _: _error_message(tool))
    content = str(result.content)
    assert result.status == "error"
    assert result.tool_call_id == "call-1"
    assert "没有执行" in content
    assert "不要原样重发" in content
    assert "原生 JSON 数组" in content
    assert "五个字段，见上文" in content
    assert '"FieldSpec"' in content
    assert '"title":{' in content.replace(" ", "")
    assert "收到 str" in content


def test_tool_body_error_is_not_blamed_on_arguments() -> None:
    # 参数合法说明失败来自工具体本身，不能被改写成"参数写错了"。
    tool = build_human_input_tools()[0]
    request = _Request(tool, {"reason": "r", "question": "q"})
    original = _error_message(tool)
    assert ToolArgHintMiddleware().wrap_tool_call(request, lambda _: original) is original


def test_successful_result_is_passed_through() -> None:
    tool = build_human_input_tools()[0]
    request = _Request(tool, {"reason": "r", "question": "q", "fields": "bad"})
    ok = ToolMessage(content="{}", tool_call_id="call-1", name=tool.name)
    assert ToolArgHintMiddleware().wrap_tool_call(request, lambda _: ok) is ok


def test_non_pydantic_schema_is_left_alone() -> None:
    class _RawSchemaTool:
        name = "raw"
        tool_call_schema = {"type": "object"}

    tool = _RawSchemaTool()
    original = _error_message(tool)
    request = _Request(tool, {"anything": 1})
    assert ToolArgHintMiddleware().wrap_tool_call(request, lambda _: original) is original
