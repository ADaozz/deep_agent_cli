import asyncio
from dataclasses import dataclass, replace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.middleware.payload_recovery import PayloadRecoveryMiddleware, _limit_tool_media


@dataclass
class Request:
    messages: list

    def override(self, **kwargs):
        return replace(self, **kwargs)


class StatusError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


def test_media_budget_keeps_newest_tool_images_without_changing_history():
    block = {"type": "image", "base64": "a" * 100, "mime_type": "image/png"}
    messages = [
        HumanMessage(content=[block]),
        ToolMessage(content=[block], tool_call_id="old"),
        ToolMessage(content=[block], tool_call_id="new"),
    ]
    reduced = _limit_tool_media(messages, budget=200)
    assert reduced[0] is messages[0]
    assert reduced[1].content[0]["type"] == "text"
    assert reduced[2].content[0]["type"] == "image"
    assert messages[1].content == [block]


def test_media_budget_applies_before_first_request(monkeypatch):
    import agent.middleware.payload_recovery as recovery

    original = recovery._limit_tool_media
    monkeypatch.setattr(recovery, "_limit_tool_media", lambda messages: original(messages, budget=1))
    request = Request([ToolMessage(content=[{"type": "image", "base64": "abc"}], tool_call_id="1")])
    calls = []
    assert PayloadRecoveryMiddleware().wrap_model_call(request, lambda req: calls.append(req) or "ok") == "ok"
    assert len(calls) == 1 and calls[0].messages[0].content[0]["type"] == "text"
    assert request.messages[0].content[0]["type"] == "image"


def test_413_retries_with_smaller_tool_results_without_changing_history():
    image = {"type": "image", "base64": "a" * 10000, "mime_type": "image/png"}
    messages = [
        HumanMessage(content="保留用户请求"),
        ToolMessage(content=[image], tool_call_id="image"),
        ToolMessage(content="x" * 10000, tool_call_id="text"),
        ToolMessage(content="small", tool_call_id="small"),
    ]
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise StatusError(413)
        return "ok"

    assert PayloadRecoveryMiddleware().wrap_model_call(Request(messages), handler) == "ok"
    retried = calls[1].messages
    assert retried[0] is messages[0]
    assert retried[1].content[0]["type"] == "text"
    assert "原始会话记录" in retried[1].content[0]["text"]
    assert len(retried[2].content) < 5000
    assert retried[3] is messages[3]
    assert messages[1].content == [image] and len(messages[2].content) == 10000


@pytest.mark.parametrize("status,large", [(400, True), (413, False)])
def test_unrecoverable_errors_are_not_retried(status, large):
    calls = []
    request = Request([ToolMessage(content="x" * (10000 if large else 1), tool_call_id="1")])

    def handler(request):
        calls.append(request)
        raise StatusError(status)

    with pytest.raises(StatusError):
        PayloadRecoveryMiddleware().wrap_model_call(request, handler)
    assert len(calls) == 1


def test_async_413_retry():
    calls = []
    request = Request([ToolMessage(content=[{"type": "file", "base64": "abc"}], tool_call_id="1")])

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise StatusError(413)
        return "ok"

    assert asyncio.run(PayloadRecoveryMiddleware().awrap_model_call(request, handler)) == "ok"
    assert calls[1].messages[0].content[0]["type"] == "text"


def test_413_recovery_runs_in_assembled_agent():
    from deepagents.backends import StateBackend
    from langchain_core.outputs import ChatGeneration, ChatResult
    from agent.bootstrap.factory import create_agent
    from tests.conftest import ScriptedToolModel

    calls = []

    class OversizedModel(ScriptedToolModel):
        def _generate(self, messages, **kwargs):
            calls.append(messages)
            if len(calls) == 1:
                raise StatusError(413)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="recovered"))])

    model = OversizedModel(messages=iter([]))
    model.profile = {"image_inputs": True}
    graph = create_agent(model=model, backend=StateBackend(), skills=[]).graph
    images = [{"type": "image", "base64": "a" * 10000, "mime_type": "image/png"}]
    result = graph.invoke({"messages": [
        HumanMessage(content="inspect"),
        AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"file_path": "/x.png"}, "id": "call-1"}]),
        ToolMessage(content=images, name="read_file", tool_call_id="call-1"),
        HumanMessage(content="continue"),
    ]}, {"configurable": {"thread_id": "test-413"}})
    assert result["messages"][-1].content == "recovered"
    assert len(calls) == 2
    assert any(isinstance(m, ToolMessage) and "原始会话记录" in str(m.content) for m in calls[1])
    assert result["messages"][2].content == images
