"""Tavily search requests are mocked; this module never accesses the network."""
from __future__ import annotations

import json

import httpx
import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, ToolMessage

from agent.config import Settings
from agent.cli.rendering import _capture, _tool
from agent.cli.state import ToolBlock
from agent.factory import create_agent
from agent.runner import AgentRunner
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
