"""DeepSeek Search and Fetch requests are mocked; this module never accesses the network."""
from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, ToolMessage

from agent.bootstrap.config import Settings
from agent.cli.rendering import _capture, _tool
from agent.cli.state import ToolBlock
from agent.bootstrap.factory import create_agent
from agent.runtime.runner import AgentRunner
from agent.execution.backend import BackendSelection
from agent.execution.types import ExecutionMode
from agent.tools.web import DEEPSEEK_TOOLS_URL, build_web_fetch_tool, build_web_search_tool
from tests.conftest import scripted_model


def _call(tool, **args) -> ToolMessage:
    defaults = {"query": "python release"} if tool.name == "web_search" else {"url": "https://example.com/a"}
    message = tool.invoke({"name": tool.name, "args": defaults | args, "id": "call-1", "type": "tool_call"})
    assert isinstance(message, ToolMessage)
    return message


def _mock_deepseek(monkeypatch, *, status: int = 200, results=None, error=None):
    requests = []
    original_client = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if error is not None:
            raise error
        if request.url.path.endswith("/fetch"):
            return httpx.Response(status, json={"title": "Article", "content": "# Article\n\nMarkdown body."})
        return httpx.Response(status, request=request, json={
            "results": results if results is not None else [{
                "title": "Python 3.14", "url": "https://python.org/a", "summary": "New release",
                "content": "Full page", "date": "2026-01-01", "rerank_score": 2.4,
            }],
        })

    monkeypatch.setattr("agent.tools.web.httpx.Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    return requests


def test_web_search_default_request(monkeypatch) -> None:
    requests = _mock_deepseek(monkeypatch)
    result = _call(build_web_search_tool("test-key"))
    assert result.status == "success"
    request = requests[0]
    assert request.method == "POST" and str(request.url) == f"{DEEPSEEK_TOOLS_URL}/search"
    assert request.headers["authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert body["query"] == "python release"
    assert body == {"query": "python release", "count": 10, "localization": "", "need_content": False}
    assert "https://python.org/a" in result.content and "New release" in result.content
    assert "Full page" not in result.content
    assert result.artifact["provider"] == "deepseek"
    assert result.artifact["results"][0]["rerank_score"] == 2.4


def test_web_search_optional_fields(monkeypatch) -> None:
    requests = _mock_deepseek(monkeypatch)
    query = "Python site:python.org lang:en after:2026"
    result = _call(build_web_search_tool("test-key"), query=query, count=50,
                   ori_question="Any new Python releases?", localization="8.8.8.8", need_content=True)
    assert json.loads(requests[0].content) == {
        "query": query, "count": 50, "ori_question": "Any new Python releases?",
        "localization": "8.8.8.8", "need_content": True,
    }
    assert "Full page" in result.content


@pytest.mark.parametrize("args", [{"count": 0}, {"count": 51}, {"count": True}, {"query": "   "},
                                  {"query": "网" * 129}, {"ori_question": ""}])
def test_web_search_validation(monkeypatch, args) -> None:
    requests = _mock_deepseek(monkeypatch)
    assert _call(build_web_search_tool("test-key"), **args).status == "error"
    assert not requests


def test_web_fetch_request_and_markdown(monkeypatch) -> None:
    requests = _mock_deepseek(monkeypatch)
    result = _call(build_web_fetch_tool("test-key"), url="https://example.com/path?q=1#section")
    request, = requests
    assert request.method == "POST" and str(request.url) == f"{DEEPSEEK_TOOLS_URL}/fetch"
    assert request.headers["authorization"] == "Bearer test-key"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {"url": "https://example.com/path?q=1#section"}
    assert result.status == "success"
    assert "# Article\n\nMarkdown body." in result.content
    assert result.artifact["title"] == "Article"


def test_web_fetch_tui_keeps_markdown_in_expanded_output(monkeypatch) -> None:
    _mock_deepseek(monkeypatch)
    result = _call(build_web_fetch_tool("test-key"))
    block = ToolBlock(
        tool_call_id=result.tool_call_id, name="web_fetch", arguments={"url": result.artifact["url"]},
        output=result.content, artifact=result.artifact, status="completed",
    )
    collapsed = _capture(_tool(block, False), 100)
    assert "https://example.com/a" in collapsed and "Article" in collapsed
    assert "Markdown body." not in collapsed
    assert "Markdown body." in _capture(_tool(block, True), 100)


@pytest.mark.parametrize("url", ["ftp://example.com/a", "https:///path", "https://user:pass@example.com",
                                 "https://@example.com", "https://example.com:abc", "https://x/" + "a" * 8192])
def test_web_fetch_rejects_invalid_url(monkeypatch, url) -> None:
    requests = _mock_deepseek(monkeypatch)
    assert _call(build_web_fetch_tool("test-key"), url=url).status == "error"
    assert not requests


@pytest.mark.parametrize(("status", "reason"), [(402, "balance"), (403, "safety policy"), (404, "usable page")])
def test_web_fetch_http_errors(monkeypatch, status, reason) -> None:
    _mock_deepseek(monkeypatch, status=status)
    result = _call(build_web_fetch_tool("test-key"))
    assert result.status == "error"
    assert reason in result.content


def test_web_search_timeout(monkeypatch) -> None:
    _mock_deepseek(monkeypatch, error=httpx.ReadTimeout("timeout"))
    result = _call(build_web_search_tool("test-key"))
    assert result.status == "error"
    assert "timed out after 20s" in result.content


def test_web_search_http_error(monkeypatch) -> None:
    _mock_deepseek(monkeypatch, status=429)
    result = _call(build_web_search_tool("test-key"))
    assert result.status == "error"
    assert "rate limit exceeded" in result.content


def test_web_search_empty_results(monkeypatch) -> None:
    _mock_deepseek(monkeypatch, results=[])
    result = _call(build_web_search_tool("test-key"))
    assert result.status == "success"
    assert result.content == "No web results found for: python release"
    assert result.artifact["results"] == []


def test_web_search_api_key_not_exposed(monkeypatch) -> None:
    _mock_deepseek(monkeypatch, status=401)
    tool = build_web_search_tool("super-secret-key")
    result = _call(tool)
    assert "super-secret-key" not in str(tool.args)
    assert "super-secret-key" not in tool.description
    assert "super-secret-key" not in result.content
    assert result.status == "error"


def test_agent_exposes_web_search_when_key_exists(monkeypatch) -> None:
    model = scripted_model([AIMessage(content="done")])
    prepared = create_agent(model=model, backend=StateBackend(), skills=[], settings=Settings(resolved_deepseek_api_key="test-key"))
    assert {"web_search", "web_fetch"} <= set(prepared.exposed_tool_names)
    runner = AgentRunner(prepared=prepared)
    runner.set_permission_mode("ask")
    assert {"web_search", "web_fetch"} <= set(runner.prepared.exposed_tool_names)
    direct = AgentRunner(model=scripted_model([AIMessage(content="done")]),
                         backend=StateBackend(), settings=Settings(resolved_deepseek_api_key="test-key"))
    assert {"web_search", "web_fetch"} <= set(direct.prepared.exposed_tool_names)


def test_agent_hides_web_search_without_key(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ignored")
    settings = Settings(resolved_deepseek_api_key=None)
    prepared = create_agent(model=scripted_model([AIMessage(content="done")]),
                            backend=StateBackend(), skills=[], settings=settings)
    assert {"web_search", "web_fetch"}.isdisjoint(prepared.exposed_tool_names)
    direct = AgentRunner(model=scripted_model([AIMessage(content="done")]),
                         backend=StateBackend(), settings=settings)
    assert {"web_search", "web_fetch"}.isdisjoint(direct.prepared.exposed_tool_names)


def test_ask_requires_approval_for_each_search(monkeypatch):
    """每次搜索分别暂停，只有批准之后才发送对应的 HTTP 请求。"""
    requests = _mock_deepseek(monkeypatch)
    runner = AgentRunner(model=scripted_model([
        AIMessage(content="", tool_calls=[{"id": "search-1", "name": "web_search", "args": {"query": "first"}}]),
        AIMessage(content="", tool_calls=[{"id": "search-2", "name": "web_search", "args": {"query": "second"}}]),
        AIMessage(content="done"),
    ]), backend=StateBackend(), settings=Settings(resolved_deepseek_api_key="test-key"))
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
    requests = _mock_deepseek(monkeypatch)
    runner = AgentRunner(model=scripted_model([
        AIMessage(content="", tool_calls=[{"id": "denied-search", "name": "web_search", "args": {"query": "x"}}]),
        AIMessage(content="done"),
    ]), backend=StateBackend(), settings=Settings(resolved_deepseek_api_key="test-key"))
    assert runner.invoke("搜索").status == "waiting_confirmation"
    assert runner.reject_tool("denied-search").status == "completed"
    assert not requests


def test_parallel_search_and_write_both_require_approval(monkeypatch):
    """混合工具调用的审批列表必须包含搜索和写文件两个动作。"""
    requests = _mock_deepseek(monkeypatch)
    runner = AgentRunner(model=scripted_model([
        AIMessage(content="", tool_calls=[
            {"id": "parallel-search", "name": "web_search", "args": {"query": "x"}},
            {"id": "parallel-write", "name": "write", "args": {"file_path": "/workspace/result.txt", "content": "result"}},
        ]),
    ]), backend=StateBackend(), settings=Settings(resolved_deepseek_api_key="test-key"))
    waiting = runner.invoke("搜索并写入")
    assert waiting.status == "waiting_confirmation"
    assert {item["name"] for item in waiting.pending_tool_calls} == {"web_search", "write"}
    assert not requests


def test_search_approval_returns_after_allow_to_ask_switch(monkeypatch):
    """切回 ask 后恢复每次搜索的审批，allow 下继续自动执行。"""
    requests = _mock_deepseek(monkeypatch)
    with patch("agent.bootstrap.factory.select_backend", return_value=BackendSelection(StateBackend(), ExecutionMode.SANDBOXED)):
        runner = AgentRunner(model=scripted_model([
            AIMessage(content="", tool_calls=[{"id": "allow-search", "name": "web_search", "args": {"query": "first"}}]),
            AIMessage(content="done"),
            AIMessage(content="", tool_calls=[{"id": "ask-search", "name": "web_search", "args": {"query": "second"}}]),
            AIMessage(content="done"),
        ]), settings=Settings(resolved_deepseek_api_key="test-key"))
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
    with pytest.raises(ValueError, match="default approval rule for web_search"):
        create_agent(model=scripted_model([AIMessage(content="done")]), backend=StateBackend(),
                     interrupt_on={"web_search": False})


@pytest.mark.parametrize("approve", [True, False])
def test_ask_waits_for_fetch_approval(monkeypatch, approve):
    requests = _mock_deepseek(monkeypatch)
    runner = AgentRunner(
        model=scripted_model([
            AIMessage(content="", tool_calls=[
                {"id": "fetch-1", "name": "web_fetch", "args": {"url": "https://example.com/a"}},
            ]),
            AIMessage(content="done"),
        ]),
        backend=StateBackend(), settings=Settings(resolved_deepseek_api_key="test-key"),
    )
    waiting = runner.invoke("读取网页")
    assert waiting.status == "waiting_confirmation"
    assert waiting.pending_tool_calls[0]["name"] == "web_fetch"
    assert not requests
    resolve = runner.approve_tool if approve else runner.reject_tool
    assert resolve("fetch-1").status == "completed"
    assert len(requests) == int(approve)


def test_web_search_tui_collapses_to_result_count() -> None:
    block = ToolBlock(
        tool_call_id="call-1", name="web_search", arguments={"query": "python release"},
        output="Web search results for: python release\n2 results\n\n1. First\n   URL: https://example.com/first",
        artifact={"provider": "deepseek", "results": [
            {"title": "First", "url": "https://example.com/first"},
            {"title": "Second", "url": "https://example.com/second"},
        ]},
        status="completed",
    )
    collapsed = _capture(_tool(block, False), 100)
    expanded = _capture(_tool(block, True), 100)
    assert 'Searched Web "python release"' in collapsed
    assert "… 2 results · Ctrl+O to expand" in collapsed
    assert "https://example.com/first" not in collapsed
    assert "https://example.com/first" in expanded


def test_web_search_preview_uses_artifact() -> None:
    block = ToolBlock(
        tool_call_id="call-2", name="web_search", arguments={"query": "deepagents"},
        output="Web search results for: deepagents\n5 results\n\n1. Deep Agents\n   URL: https://example.com/a",
        artifact={
            "provider": "deepseek",
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
    assert 'Searched Web "deepagents"' in collapsed
    assert "5 results" in collapsed
    assert "0.42" in collapsed
    assert "https://example.com/a" not in collapsed
    assert "… 5 results · 0.42s · Ctrl+O to expand" in collapsed


def test_web_search_preview_formats_numeric_response_time() -> None:
    block = ToolBlock(
        tool_call_id="call-3", name="web_search", arguments={"query": "timing"},
        output="Web search results for: timing\n1 results",
        artifact={"provider": "deepseek", "results": [{"title": "T", "url": "https://x"}], "response_time": 2.0},
        status="completed",
    )
    collapsed = _capture(_tool(block, False), 100)
    assert "… 1 results · 2s · Ctrl+O to expand" in collapsed
