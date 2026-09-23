from typing import Any, Iterator

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage


class ScriptedToolModel(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> BaseChatModel:  # type: ignore[override]
        return self

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def _get_ls_params(self, stop: Any = None, **kwargs: Any) -> dict[str, str]:
        return {"ls_provider": "openai", "ls_model_name": "fake"}


def scripted_model(messages: list[AIMessage]) -> ScriptedToolModel:
    iterator: Iterator[AIMessage] = iter(messages)
    return ScriptedToolModel(messages=iterator)


def graph_tool_names(graph: Any) -> set[str]:
    names: set[str] = set()
    nodes = getattr(graph, "nodes", {}) or {}
    for node in nodes.values():
        stack = [node]
        for attr in ("runnable", "bound", "_runnable"):
            nested = getattr(node, attr, None)
            if nested is not None:
                stack.append(nested)
        for obj in stack:
            tools_by_name = getattr(obj, "tools_by_name", None)
            if isinstance(tools_by_name, dict):
                names.update(str(name) for name in tools_by_name)
            tools = getattr(obj, "tools", None)
            if isinstance(tools, (list, tuple)):
                for tool in tools:
                    name = getattr(tool, "name", None)
                    if name:
                        names.add(str(name))
    return names


@pytest.fixture
def fake_done() -> ScriptedToolModel:
    return scripted_model([AIMessage(content="done")])
