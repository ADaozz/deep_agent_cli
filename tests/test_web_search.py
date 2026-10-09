"""Tavily search requests are mocked; this module never accesses the network."""
from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, ToolMessage

from agent.config import Settings
from agent.cli.rendering import _capture, _tool
from agent.cli.state import ToolBlock
from agent.factory import create_agent
from agent.runner import AgentRunner
from agent.permission import ASK_INTERRUPT_ON
from agent.sandbox import BackendSelection, ExecutionMode
from agent.tools.web_search import TAVILY_SEARCH_URL, build_web_search_tool
from tests.conftest import scripted_model


def _call(tool, **args) -> ToolMessage:
    message = tool.invoke({"name": "web_search", "args": {"query": "python release", **args}, "id": "call-1", "type": "tool_call"})
    assert isinstance(message, ToolMessage)
    return message


def _mock_tavily(monkeypatch, *, status: int = 200, results=None, error=None):
    requests = []
    original_client = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if error is not None:
            raise error
        return httpx.Response(status, request=request, json={
            "results": results if results is not None else [{
                "title": "Python 3.14", "url": "https://python.org/a", "content": "New release",
                "published_date": "2026-01-01", "score": 0.9,
            }],
            "response_time": 0.3, "request_id": "req-123", "usage": {"credits": 1},
        })

    monkeypatch.setattr("agent.tools.web_search.httpx.Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    return requests


def test_web_search_default_request(monkeypatch) -> None:
    requests = _mock_tavily(monkeypatch)
    result = _call(build_web_search_tool("test-key"))
    assert result.status == "success"
    request = requests[0]
    assert request.method == "POST" and str(request.url) == TAVILY_SEARCH_URL
    assert request.headers["authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert body["query"] == "python release"
    assert body["max_results"] == 5 and body["topic"] == "general"
    assert body["search_depth"] == "basic" and body["include_usage"] is True
    assert "time_range" not in body and "include_domains" not in body
    assert "1 results" in result.content and "https://python.org/a" in result.content
    assert result.artifact["provider"] == "tavily"
    assert result.artifact["request_id"] == "req-123"
    assert result.artifact["usage"] == {"credits": 1}


def test_web_search_news_and_time_range(monkeypatch) -> None:
    requests = _mock_tavily(monkeypatch)
    _call(build_web_search_tool("test-key"), topic="news", time_range="week")
    body = json.loads(requests[0].content)
    assert body["topic"] == "news" and body["time_range"] == "week"


def test_web_search_domain_filter(monkeypatch) -> None:
    requests = _mock_tavily(monkeypatch)
    _call(build_web_search_tool("test-key"), include_domains=[" python.org "])
    assert json.loads(requests[0].content)["include_domains"] == ["python.org"]


@pytest.mark.parametrize("count", [0, 21])
def test_web_search_max_results_validation(monkeypatch, count: int) -> None:
    requests = _mock_tavily(monkeypatch)
    result = _call(build_web_search_tool("test-key"), max_results=count)
    assert result.status == "error"
    assert not requests


def test_web_search_timeout(monkeypatch) -> None:
    _mock_tavily(monkeypatch, error=httpx.ReadTimeout("timeout"))
    result = _call(build_web_search_tool("test-key"))
    assert result.status == "error"
    assert "timed out after 20s" in result.content


def test_web_search_http_error(monkeypatch) -> None:
    _mock_tavily(monkeypatch, status=429)
    result = _call(build_web_search_tool("test-key"))
    assert result.status == "error"
    assert "rate limit exceeded" in result.content


def test_web_search_empty_results(monkeypatch) -> None:
    _mock_tavily(monkeypatch, results=[])
    result = _call(build_web_search_tool("test-key"))
    assert result.status == "success"
    assert result.content == "No web results found for: python release"
    assert result.artifact["results"] == []


def test_web_search_api_key_not_exposed(monkeypatch) -> None:
    _mock_tavily(monkeypatch, status=401)
    tool = build_web_search_tool("super-secret-key")
    result = _call(tool)
    assert "super-secret-key" not in str(tool.args)
    assert "super-secret-key" not in tool.description
    assert "super-secret-key" not in result.content
    assert result.status == "error"


def test_agent_exposes_web_search_when_key_exists(monkeypatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    model = scripted_model([AIMessage(content="done")])
    prepared = create_agent(model=model, backend=StateBackend(), skills=[])
    assert "web_search" in prepared.exposed_tool_names
    runner = AgentRunner(prepared=prepared)
    runner.set_permission_mode("ask")
    assert "web_search" in runner.prepared.exposed_tool_names
    direct = AgentRunner(model=scripted_model([AIMessage(content="done")]),
                         backend=StateBackend(), settings=Settings())
    assert "web_search" in direct.prepared.exposed_tool_names


def test_agent_hides_web_search_without_key(monkeypatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    settings = Settings(tavily_api_key=None)
    prepared = create_agent(model=scripted_model([AIMessage(content="done")]),
                            backend=StateBackend(), skills=[], settings=settings)
    assert "web_search" not in prepared.exposed_tool_names
    direct = AgentRunner(model=scripted_model([AIMessage(content="done")]),
                         backend=StateBackend(), settings=settings)
    assert "web_search" not in direct.prepared.exposed_tool_names


def test_ask_requires_approval_for_each_search(monkeypatch):
    """每次搜索分别暂停，只有批准之后才发送对应的 HTTP 请求。"""
    requests = _mock_tavily(monkeypatch)
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    runner = AgentRunner(model=scripted_model([
        AIMessage(content="", tool_calls=[{"id": "search-1", "name": "web_search", "args": {"query": "first"}}]),
        AIMessage(content="", tool_calls=[{"id": "search-2", "name": "web_search", "args": {"query": "second"}}]),
        AIMessage(content="done"),
    ]), backend=StateBackend())
    waiting = runner.invoke("搜索两次")
    assert waiting.status == "waiting_confirmation"
    assert waiting.pending_tool_calls[0]["name"] == "web_search"
    assert not requests
    waiting = runner.approve_tool("search-1")
    assert waiting.status == "waiting_confirmation"
    assert waiting.pending_tool_calls[0]["toolCallId"] == "search-2"
    assert len(requests) == 1
    assert runner.approve_tool("search-2").status == "completed"
    assert len(requests) == 2


def test_rejected_search_sends_no_request(monkeypatch):
    """拒绝搜索时不访问搜索服务。"""
    requests = _mock_tavily(monkeypatch)
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    runner = AgentRunner(model=scripted_model([
        AIMessage(content="", tool_calls=[{"id": "denied-search", "name": "web_search", "args": {"query": "x"}}]),
        AIMessage(content="done"),
    ]), backend=StateBackend())
    assert runner.invoke("搜索").status == "waiting_confirmation"
    assert runner.reject_tool("denied-search").status == "completed"
    assert not requests


def test_parallel_search_and_write_both_require_approval(monkeypatch):
    """混合工具调用的审批列表必须包含搜索和写文件两个动作。"""
    requests = _mock_tavily(monkeypatch)
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    runner = AgentRunner(model=scripted_model([
        AIMessage(content="", tool_calls=[
            {"id": "parallel-search", "name": "web_search", "args": {"query": "x"}},
            {"id": "parallel-write", "name": "write_file", "args": {"file_path": "/workspace/result.txt", "content": "result"}},
        ]),
    ]), backend=StateBackend())
    waiting = runner.invoke("搜索并写入")
    assert waiting.status == "waiting_confirmation"
    assert {item["name"] for item in waiting.pending_tool_calls} == {"web_search", "write_file"}
    assert not requests


def test_search_approval_returns_after_allow_to_ask_switch(monkeypatch):
    """切回 ask 后恢复每次搜索的审批，allow 下继续自动执行。"""
    requests = _mock_tavily(monkeypatch)
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    with patch("agent.factory.select_backend", return_value=BackendSelection(StateBackend(), ExecutionMode.SANDBOXED)):
        runner = AgentRunner(model=scripted_model([
            AIMessage(content="", tool_calls=[{"id": "allow-search", "name": "web_search", "args": {"query": "first"}}]),
            AIMessage(content="done"),
            AIMessage(content="", tool_calls=[{"id": "ask-search", "name": "web_search", "args": {"query": "second"}}]),
            AIMessage(content="done"),
        ]))
        runner.set_permission_mode("allow")
        assert runner.invoke("搜索").status == "completed"
        assert len(requests) == 1
        runner.set_permission_mode("ask")
        assert runner.invoke("再搜索").status == "waiting_confirmation"
        assert len(requests) == 1
        assert runner.approve_tool("ask-search").status == "completed"
        assert len(requests) == 2


def test_search_default_approval_cannot_be_overridden(monkeypatch):
    """自定义审批配置不能关闭 ask 模式内置的搜索审批。"""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    with pytest.raises(ValueError, match="default approval rule for web_search"):
        create_agent(model=scripted_model([AIMessage(content="done")]), backend=StateBackend(),
                     interrupt_on={"web_search": False})


def test_old_search_policy_waits_for_other_tools_in_the_same_batch(monkeypatch):
    """复现旧规则：搜索与写文件同批出现时，被写文件审批一起阻塞。"""
    requests = _mock_tavily(monkeypatch)
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    old_policy = {name: rule for name, rule in ASK_INTERRUPT_ON.items() if name != "web_search"}
    with patch("agent.factory.interrupt_on_for_mode", return_value=old_policy):
        prepared = create_agent(model=scripted_model([
            AIMessage(content="", tool_calls=[
                {"id": "old-search", "name": "web_search", "args": {"query": "x"}},
                {"id": "old-write", "name": "write_file", "args": {"file_path": "/workspace/result.txt", "content": "result"}},
            ]),
            AIMessage(content="done"),
        ]), backend=StateBackend())
    runner = AgentRunner(prepared=prepared)
    waiting = runner.invoke("搜索并写入")
    assert waiting.status == "waiting_confirmation"
    assert [item["name"] for item in waiting.pending_tool_calls] == ["write_file"]
    assert not requests
    assert runner.approve_tool("old-write").status == "completed"
    assert len(requests) == 1


def test_web_search_tui_collapses_to_result_count() -> None:
    block = ToolBlock(
        tool_call_id="call-1", name="web_search", arguments={"query": "python release"},
        output="Web search results for: python release\n2 results\n\n1. First\n   URL: https://example.com/first",
        status="completed",
    )
    collapsed = _capture(_tool(block, False), 100)
    expanded = _capture(_tool(block, True), 100)
    assert 'web_search "python release"' in collapsed
    assert "… 2 results · Ctrl+O to expand" in collapsed
    assert "https://example.com/first" not in collapsed
    assert "https://example.com/first" in expanded


def test_web_search_preview_uses_artifact() -> None:
    block = ToolBlock(
        tool_call_id="call-2", name="web_search", arguments={"query": "deepagents"},
        output="Web search results for: deepagents\n5 results\n\n1. Deep Agents\n   URL: https://example.com/a",
        artifact={
            "provider": "tavily",
            "query": "deepagents",
            "results": [
                {"title": "Deep Agents", "url": "https://example.com/a"},
                {"title": "Deep Agents docs", "url": "https://example.com/b"},
                {"title": "Deep Agents repo", "url": "https://example.com/c"},
                {"title": "Deep Agents blog", "url": "https://example.com/d"},
                {"title": "Deep Agents paper", "url": "https://example.com/e"},
            ],
            "response_time": "0.42",
        },
        status="completed",
    )
    collapsed = _capture(_tool(block, False), 100)
    assert 'web_search "deepagents"' in collapsed
    assert "5 results" in collapsed
    assert "0.42" in collapsed
    assert "https://example.com/a" not in collapsed
    assert "… 5 results · 0.42s · Ctrl+O to expand" in collapsed


def test_web_search_preview_formats_numeric_response_time() -> None:
    block = ToolBlock(
        tool_call_id="call-3", name="web_search", arguments={"query": "timing"},
        output="Web search results for: timing\n1 results",
        artifact={"provider": "tavily", "results": [{"title": "T", "url": "https://x"}], "response_time": 2.0},
        status="completed",
    )
    collapsed = _capture(_tool(block, False), 100)
    assert "… 1 results · 2s · Ctrl+O to expand" in collapsed
