"""验证实际模型请求中的中文提示词和工具说明。"""
import asyncio
import re
from copy import deepcopy

import pytest
from deepagents.backends import StateBackend
from deepagents.backends.filesystem import FilesystemBackend
from langchain.agents.middleware.types import ModelRequest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from agent.config import SandboxConfig
from agent.factory import create_agent
from agent.middleware.tool_descriptions import ChineseToolDescriptionsMiddleware, TOOL_DESCRIPTIONS
from tests.conftest import graph_tool_names, scripted_model


class CaptureSystem(BaseCallbackHandler):
    def __init__(self):
        self.messages = []

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.messages.extend(message for batch in messages for message in batch if isinstance(message, SystemMessage))


@pytest.mark.parametrize("with_skills", [False, True])
def test_actual_system_prompt_has_chinese_context_without_tool_instructions(tmp_path, with_skills, monkeypatch):
    skill = tmp_path / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: 示例技能\n---\n技能正文")
    (tmp_path / "AGENTS.md").write_text("保留项目约定。")
    model = scripted_model([AIMessage(content="完成")])
    schemas = []
    original_bind = type(model).bind_tools

    def capture_tools(self, tools, **kwargs):
        schemas.extend(convert_to_openai_tool(tool) for tool in tools)
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
    prepared.graph.invoke(payload, config)
    assert schemas
    for tool in schemas:
        assert re.search(r"[\u4e00-\u9fff]", tool["function"]["description"])
    assert len(capture.messages) == 1
    content = str(capture.messages[0].content)
    for expected in ("编程智能体", "# 用户指令", "保留用户约定。", "# 项目指令", "保留项目约定。"):
        assert expected in content
    for unwanted in ("# 工具使用", "# 人工输入", "request_human_input", "read_file", "edit_file", "mcp_add", "How to Use", "limit=1000"):
        assert unwanted not in content
    if with_skills:
        assert "## 技能目录" in content
        assert "demo" in content and "示例技能" in content and "/skills/demo/SKILL.md" in content
        assert content.count("## 技能目录") == 1
    assert {"read_file", "request_human_input", "compact_conversation"} <= graph_tool_names(prepared.graph)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_library_tool_descriptions_are_chinese_and_original_schema_is_unchanged(asynchronous):
    prepared = create_agent(model=scripted_model([AIMessage(content="unused")]), backend=StateBackend(), skills=[])
    originals = [tool for name, tool in prepared.graph.nodes["tools"].bound.tools_by_name.items() if name in TOOL_DESCRIPTIONS]
    schemas = [convert_to_openai_tool(tool) for tool in originals]
    before = deepcopy(schemas)
    request = ModelRequest(model=prepared.model, messages=[], tools=originals)
    middleware = ChineseToolDescriptionsMiddleware()
    if asynchronous:
        async def handler(value):
            return value
        localized = asyncio.run(middleware.awrap_model_call(request, handler))
    else:
        localized = middleware.wrap_model_call(request, lambda value: value)
    for original, tool in zip(before, localized.tools):
        function = convert_to_openai_tool(tool)["function"]
        assert re.search(r"[\u4e00-\u9fff]", function["description"])
        properties = function["parameters"].get("properties", {})
        assert properties.keys() == original["function"]["parameters"].get("properties", {}).keys()
        for schema in properties.values():
            assert re.search(r"[\u4e00-\u9fff]", schema["description"])
    assert [convert_to_openai_tool(tool) for tool in originals] == before
    assert schemas == before
    dict_request = request.override(tools=schemas)
    middleware.wrap_model_call(dict_request, lambda value: value)
    assert schemas == before
