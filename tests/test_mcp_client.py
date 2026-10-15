import asyncio
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import ToolNode

from agent.mcp.bridge import AsyncBridge, SyncMCPTool
from agent.mcp.client import discover_mcp_tools
from agent.mcp.config import MCPProfile


@pytest.fixture(params=["http", "sse"])
def mcp_server(request, tmp_path, monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (tmp_path / "server.log").open("w+") as log:
        request.node.mcp_server_log = tmp_path / "server.log"
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("mcp_test_server.py")),
                                    request.param, str(port)], stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.seek(0)
                    pytest.fail(log.read())
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.05)
            else:
                pytest.fail("MCP test server did not start")
            yield MCPProfile("test", f"http://127.0.0.1:{port}/{'sse' if request.param == 'sse' else 'mcp'}",
                             "sse" if request.param == "sse" else "streamable_http")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def test_official_adapter_and_sync_graph(mcp_server):
    bridge = AsyncBridge()
    try:
        originals = bridge.run(discover_mcp_tools(mcp_server))
        original = next(tool for tool in originals if tool.name == "remote_add")
        assert original.response_format == "content_and_artifact"
        assert original.args_schema["properties"]["a"]["type"] == "integer"
        with pytest.raises(NotImplementedError):
            original.invoke({"a": 2, "b": 3})
        call = {"type": "tool_call", "id": "test-id", "name": "remote_add", "args": {"a": 2, "b": 3}}
        async_result = bridge.run(original.ainvoke(call))
        assert isinstance(async_result, ToolMessage)
        assert async_result.artifact == {"structured_content": {"sum": 5}}
        tools = [SyncMCPTool(tool, bridge, profile="test", url=mcp_server.url) for tool in originals]
        graph = StateGraph(MessagesState)
        graph.add_node("tools", ToolNode(tools))
        graph.add_edge(START, "tools")
        graph.add_edge("tools", END)
        calls = [call, {"type": "tool_call", "id": "rich", "name": "rich_result", "args": {}},
                 {"type": "tool_call", "id": "error", "name": "fail_tool", "args": {}}]
        values = list(graph.compile().stream({"messages": [AIMessage(content="", tool_calls=calls)]},
                                             stream_mode="values"))
        results = [m for m in values[-1]["messages"] if isinstance(m, ToolMessage)]
        assert results[0].artifact == async_result.artifact
        assert results[0].tool_call_id == "test-id"
        assert {block["type"] for block in results[1].content} == {"text", "image", "file"}
        assert results[1].artifact == {"structured_content": {"value": [1, 2]}}
        assert results[2].status == "error"
    finally:
        bridge.close()
