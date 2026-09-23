from agent.tools.human_interaction import normalize_interaction_request


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
