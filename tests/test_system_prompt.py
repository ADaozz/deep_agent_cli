"""验证系统提示词组装、原生技能目录及工具 Schema 未被运行期改写。"""
import re
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from deepagents.backends.filesystem import FilesystemBackend
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from agent.config import SandboxConfig, Settings
from agent.factory import create_agent
from agent.sandbox import BackendSelection, ExecutionMode
from tests.conftest import graph_tool_names, scripted_model

# 上游内置工具：描述与参数说明由工具定义提供，不应在模型请求阶段被改写。
UPSTREAM_TOOLS = ("ls", "read_file", "write_file", "edit_file", "delete", "compact_conversation")


@pytest.mark.parametrize('role', ['main', 'subagent'])
@pytest.mark.parametrize('timezone_name, dates', [
    ('Asia/Shanghai', ['2026-10-29', '2026-10-30']),
    ('America/Los_Angeles', ['2026-10-28', '2026-10-29']),
])
def test_current_user_date_refreshes_in_configured_timezone(tmp_path, monkeypatch, role, timezone_name, dates):
    now = [datetime(2026, 10, 28, 16, 30, tzinfo=timezone.utc)]

    class Clock:
        @staticmethod
        def now(tz):
            return now[0].astimezone(tz)

    monkeypatch.setattr('agent.middleware.current_time.datetime', Clock)
    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    prepared = create_agent(
        model=scripted_model([AIMessage(content='first'), AIMessage(content='second')]),
        backend=backend,
        backend_selection=BackendSelection(backend, ExecutionMode.SANDBOXED),
        settings=replace(Settings(), ui_timezone=timezone_name),
        skills=[], role=role,
    )
    capture = CaptureSystem()
    config = {'configurable': {'thread_id': 'current-date'}, 'callbacks': [capture]}
    for day in (28, 29):
        now[0] = now[0].replace(day=day)
        payload = {'messages': [HumanMessage(content='hello')]}
        prepared.graph.invoke(payload, config)
    assert len(capture.messages) == 2
    for message, date in zip(capture.messages, dates):
        content = str(message.content)
        assert f'当前日期：{date}' in content
        assert f'用户时区：{timezone_name}' in content
        assert content.count('# 当前用户时间') == 1
    assert '# 当前用户时间' not in prepared.system_prompt


class CaptureSystem(BaseCallbackHandler):
    def __init__(self):
        self.messages = []

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.messages.extend(message for batch in messages for message in batch if isinstance(message, SystemMessage))


@pytest.mark.parametrize("with_skills", [False, True])
def test_system_prompt_composition_and_native_tool_schemas(tmp_path, with_skills, monkeypatch):
    skill = tmp_path / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: 示例技能\n---\n技能正文")
    (tmp_path / "AGENTS.md").write_text("保留项目约定。")
    model = scripted_model([AIMessage(content="完成")])
    schemas = {}
    original_bind = type(model).bind_tools

    def capture_tools(self, tools, **kwargs):
        schemas.update({tool.name if hasattr(tool, "name") else tool["name"]: tool for tool in tools})
        return original_bind(self, tools, **kwargs)

    monkeypatch.setattr(type(model), "bind_tools", capture_tools)
    prepared = create_agent(
        model=model,
        backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        sandbox_config=SandboxConfig(workspace=tmp_path),
        instructions="保留用户约定。", skills=["/skills/"] if with_skills else None,
    )
    capture = CaptureSystem()
    config = {"configurable": {"thread_id": "prompt"}, "callbacks": [capture]}
    payload = {"messages": [HumanMessage(content="你好")]}
    result = prepared.graph.invoke(payload, config)
    assert result["messages"][-1].content == "完成"
    # 模型绑定的 Schema 与工具定义逐一致：没有请求期翻译或参数描述覆盖。
    assert set(UPSTREAM_TOOLS) <= set(schemas)
    bound = prepared.graph.nodes["tools"].bound.tools_by_name
    for name in UPSTREAM_TOOLS:
        assert convert_to_openai_tool(bound[name]) == convert_to_openai_tool(schemas[name])
        function = convert_to_openai_tool(schemas[name])["function"]
        assert not re.search(r"[\u4e00-\u9fff]", function["description"])
        for schema in function["parameters"].get("properties", {}).values():
            if "description" in schema:
                assert not re.search(r"[\u4e00-\u9fff]", schema["description"])
    assert len(capture.messages) == 1
    content = str(capture.messages[0].content)
    for expected in ("编程智能体", "# 用户指令", "保留用户约定。", "# 项目指令", "保留项目约定。"):
        assert expected in content
    if with_skills:
        # 上游 SkillsMiddleware 渲染技能目录，技能可被发现并呈现。
        assert content.count("## Skills System") == 1
        assert "demo" in content and "示例技能" in content and "/skills/demo/SKILL.md" in content
    assert {"read_file", "request_human_input", "compact_conversation"} <= graph_tool_names(prepared.graph)


def test_upstream_tool_schema_unchanged_without_localization(tmp_path):
    """启用与未启用技能时，上游工具 Schema 均保持原样。"""
    skill = tmp_path / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: 示例技能\n---\n技能正文")
    model = scripted_model([AIMessage(content="unused")])
    prepared = create_agent(model=model, backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True), skills=[])
    bound = prepared.graph.nodes["tools"].bound.tools_by_name
    for name in UPSTREAM_TOOLS:
        function = convert_to_openai_tool(bound[name])["function"]
        assert function["description"], name
        assert not re.search(r"[\u4e00-\u9fff]", function["description"])
        for schema in function["parameters"].get("properties", {}).values():
            if "description" in schema:
                assert not re.search(r"[\u4e00-\u9fff]", schema["description"])
