from agent.tools.human_interaction import fields_from_question, normalize_interaction_request


QUESTION = """请帮助澄清你对\"代码评审 agent\"的具体需求：

**1. 核心功能**（你希望这个 agent 主要做什么？）
- 自动审查代码质量（代码风格、最佳实践）
- 检测潜在 bug 和安全漏洞
- 检查测试覆盖率
- 提供改进建议
- 其他：_______

**2. 使用场景**
- 集成到 CI/CD 流程中自动运行
- 作为开发者的交互式工具（对话式评审）
- 两者都需要

**3. 目标代码类型**
- 特定语言（如 Python、Java、JavaScript 等）
- 多语言支持
- 特定框架

**4. 部署形式**
- 作为独立服务运行
- 作为现有平台的插件/扩展
- 作为命令行工具
- 其他：_______

**5. 已有约束**
- 是否需要与现有系统集成？
- 是否有性能或响应时间要求？
- 是否有安全或合规要求？

请描述你的期望，我将据此形成完整的 intent.md 文档。"""


def test_fields_from_markdown_questionnaire() -> None:
    fields = fields_from_question(QUESTION)
    assert [item["type"] for item in fields] == [
        "multi_select",
        "single_select",
        "single_select",
        "single_select",
        "textarea",
        "textarea",
    ]
    assert any(opt["label"].startswith("自动审查代码质量") for opt in fields[0]["options"])


def test_normalize_replaces_text_field_with_selects() -> None:
    payload = normalize_interaction_request(
        reason="需求不一致",
        question=QUESTION,
        fields=[{"id": "text", "type": "text", "label": "回复", "required": True, "options": []}],
    )
    assert payload["fields"][0]["type"] == "multi_select"
    assert "自动审查" in payload["fields"][0]["options"][0]["label"]
    assert "1. 核心功能" not in payload["question"]
