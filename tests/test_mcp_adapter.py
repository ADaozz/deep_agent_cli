"""P0: MCPAdapter tools run on the synchronous runner bridge."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from deepagents.backends import StateBackend

from agent.cancel import ToolCancelContext, clear_cancel_context, set_cancel_context
from agent.mcp.adapter import open_server
from agent.mcp.bridge import SyncAsyncBridge
from agent.mcp.config import server_from_fields
from agent.mcp.manager import MCPManager
from agent.runner import AgentRunner
from tests.conftest import graph_tool_names, scripted_model
from tests.mcp_fixture import HttpFixture, text_of


def test_adapter_returns_base_tools_and_sync_invoke_returns_the_result(http_mcp: HttpFixture) -> None:
    bridge = SyncAsyncBridge()
    server = server_from_fields(name="local", transport="streamable_http", url=http_mcp.url)
    loaded = open_server(server, bridge, call_timeout=8, connect_timeout=8, oauth_callback_timeout=1)
    try:
        assert loaded.tools
        assert all(isinstance(tool, BaseTool) for tool in loaded.tools)
        echo = next(tool for tool in loaded.tools if tool.name == "mcp__local__echo")
        added = next(tool for tool in loaded.tools if tool.name == "mcp__local__add")
        failed = next(tool for tool in loaded.tools if tool.name == "mcp__local__fail")
        assert "hello" in text_of(echo.invoke({"text": "hello"}))
        assert "5" in text_of(added.invoke({"a": 2, "b": 3}))
        failed_result = text_of(failed.invoke({}))
        assert "fixture failure" in failed_result.lower() or "error" in failed_result.lower()
    finally:
        bridge.call(loaded.client.close(), timeout=5)
        bridge.shutdown()


def test_call_timeout_ends(http_mcp: HttpFixture) -> None:
    bridge = SyncAsyncBridge()
    server = server_from_fields(name="local", transport="streamable_http", url=http_mcp.url)
    loaded = open_server(server, bridge, call_timeout=0.4, connect_timeout=8, oauth_callback_timeout=1)
    try:
        sleep = next(tool for tool in loaded.tools if tool.name == "mcp__local__sleep")
        started = time.monotonic()
        result = text_of(sleep.invoke({"seconds": 5}))
        assert time.monotonic() - started < 4
        assert "timed out" in result.lower()
    finally:
        bridge.call(loaded.client.close(), timeout=5)
        bridge.shutdown()


def test_cancel_leaves_no_extra_bridge_tasks(http_mcp: HttpFixture) -> None:
    bridge = SyncAsyncBridge()
    server = server_from_fields(name="local", transport="streamable_http", url=http_mcp.url)
    loaded = open_server(server, bridge, call_timeout=30, connect_timeout=8, oauth_callback_timeout=1)
    try:
        sleep = next(tool for tool in loaded.tools if tool.name == "mcp__local__sleep")
        baseline = bridge.pending_task_count()
        event = threading.Event()
        holder: dict[str, object] = {}

        def run() -> None:
            context = ToolCancelContext("mcp__local__sleep", "call-sleep", event)
            set_cancel_context(context)
            try:
                holder["result"] = sleep.invoke({"seconds": 30})
            except Exception as exc:  # noqa: BLE001 — the assertion checks the outcome
                holder["error"] = exc
            finally:
                clear_cancel_context()

        worker = threading.Thread(target=run)
        worker.start()
        time.sleep(0.3)
        event.set()
        worker.join(5)
        assert not worker.is_alive()
        outcome = text_of(holder.get("result", holder.get("error", "")))
        assert "cancel" in outcome.lower()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and bridge.pending_task_count() > baseline + 1:
            time.sleep(0.05)
        assert bridge.pending_task_count() <= baseline + 1
    finally:
        bridge.call(loaded.client.close(), timeout=5)
        bridge.shutdown()


def test_native_tools_still_run_when_mcp_is_connected(tmp_path: Path, http_mcp: HttpFixture) -> None:
    manager = MCPManager(tmp_path / "mcp.json", call_timeout=8, connect_timeout=5)
    try:
        added = manager.add_from_fields(name="local", transport="streamable_http", url=http_mcp.url)
        assert added.ok, added.message
        runner = AgentRunner(
            model=scripted_model([
                AIMessage(content="", tool_calls=[{
                    "id": "call-write",
                    "name": "write_file",
                    "args": {"file_path": "/workspace/note.txt", "content": "hello"},
                }]),
                AIMessage(content="写入完成。"),
            ]),
            backend=StateBackend(),
            mcp_manager=manager,
        )
        try:
            names = graph_tool_names(runner.prepared.graph)
            assert "write_file" in names
            assert "mcp__local__echo" in names
            waiting = runner.invoke("写入")
            assert waiting.status == "waiting_confirmation"
            resumed = runner.approve_tool("call-write")
            assert resumed.status == "completed"
            assert resumed.output == "写入完成。"
        finally:
            runner.close()
    finally:
        if not manager._closed:
            manager.close()
