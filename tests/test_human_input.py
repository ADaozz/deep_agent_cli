import json

import pytest
from pydantic import ValidationError

from agent.tools.human_input import (
    HumanInputArgs,
    Recommendation,
    build_human_input_tools,
)
from agent.tools.human_interaction import (
    FIELD_TYPES,
    normalize_interaction_request,
    resume_values,
)


def test_explicit_fields_and_question_are_preserved() -> None:
    question = "请选择范围。\n- 部门\n- 全局"
    fields = [{
        "id": "scope", "type": "single_select", "label": "范围", "required": True,
        "options": [
            {"value": "department", "label": "部门"},
            {"value": "global", "label": "全局"},
        ],
    }]
    payload = normalize_interaction_request(reason="需要决策", question=question, fields=fields)
    assert payload["question"] == question
    assert payload["fields"][0]["id"] == "scope"
    assert payload["fields"][0]["type"] == "single_select"
    assert [option["value"] for option in payload["fields"][0]["options"]] == ["department", "global"]
    assert payload["blocking"] is True


def test_plain_question_gets_one_text_field() -> None:
    payload = normalize_interaction_request(reason="缺少信息", question="请提供目标目录")
    assert len(payload["fields"]) == 1
    assert payload["fields"][0]["type"] == "text"


def test_human_response_requires_values() -> None:
    assert resume_values({"type": "human_input", "values": {"scope": "department"}}) == {
        "scope": "department",
    }
    with pytest.raises(ValueError, match="values object"):
        resume_values({"type": "human_input", "text": "old format"})
    with pytest.raises(ValueError, match="type human_input"):
        resume_values({"values": {"text": "no type"}})
    with pytest.raises(ValueError, match="type human_input"):
        resume_values(True)


def test_tool_schema_describes_field_shape() -> None:
    # 裸 list/dict 注解会退化成 items: {}，模型看不到字段结构就会把整个参数序列化成字符串。
    schema = build_human_input_tools()[0].tool_call_schema.model_json_schema()
    assert schema["properties"]["fields"]["items"] == {"$ref": "#/$defs/FieldSpec"}
    field_spec = schema["$defs"]["FieldSpec"]["properties"]
    assert {"id", "type", "label", "required", "options"} <= set(field_spec)
    assert field_spec["type"]["enum"] == list(FIELD_TYPES)
    assert schema["$defs"]["FieldOption"]["properties"]["value"]["type"] == "string"
    assert schema["$defs"]["Recommendation"]["properties"]["value"]["type"] == "string"


def test_stringified_fields_are_decoded() -> None:
    args = HumanInputArgs.model_validate({
        "reason": "缺少接入信息",
        "question": "demo 用哪种 SDK 形态？",
        "fields": json.dumps([{
            "id": "sdk_choice", "type": "single_select", "label": "SDK 形态",
            "options": [{"id": "deepagents", "label": "官方 deepagents 包"}],
        }], ensure_ascii=False),
        "recommendation": json.dumps({"value": "deepagents", "reason": "内置规划与子代理"}, ensure_ascii=False),
        "impact": json.dumps(["SDK 选择决定依赖清单"], ensure_ascii=False),
    })
    assert args.fields[0].id == "sdk_choice"
    assert args.fields[0].options[0].value == "deepagents"
    assert args.recommendation == Recommendation(value="deepagents", reason="内置规划与子代理")
    assert args.impact == ["SDK 选择决定依赖清单"]


def test_unknown_enum_values_degrade_instead_of_failing() -> None:
    args = HumanInputArgs.model_validate({
        "reason": "r",
        "question": "q",
        "interaction_type": "poll",
        "fields": [{"id": "scope", "type": "select", "label": "范围"}],
    })
    assert args.interaction_type == "clarification"
    assert args.fields[0].type == "text"


def test_unparsable_fields_still_raise_validation_error() -> None:
    # 还原不了的结构必须继续报错，交给 ToolArgHintMiddleware 生成提示。
    with pytest.raises(ValidationError) as excinfo:
        HumanInputArgs.model_validate({"reason": "r", "question": "q", "fields": "五个字段，见上文"})
    assert excinfo.value.errors()[0]["type"] == "list_type"
