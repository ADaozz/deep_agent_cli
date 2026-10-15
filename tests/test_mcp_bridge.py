import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from agent.cancel import ToolCancelContext, set_cancel_context, clear_cancel_context
from agent.mcp.bridge import AsyncBridge, SyncMCPTool


def test_concurrent_cancel_isolation_and_cleanup():
    started = threading.Event()
    cleaned = threading.Event()
    async def work(value: str):
        """Return a structured result after work."""
        if value == "cancel":
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                await asyncio.sleep(.05)
                cleaned.set()
        return ([{"type": "text", "text": value}], {"value": value})
    original = StructuredTool.from_function(coroutine=work, response_format="content_and_artifact")
    bridge = AsyncBridge()
    tool = SyncMCPTool(original, bridge, profile="test", url="http://localhost/mcp")
    ctx = ToolCancelContext(tool.name, "cancel-id", threading.Event(), generation=1)
    def invoke_cancel():
        set_cancel_context(ctx)
        try:
            return tool.invoke({"type": "tool_call", "id": "cancel-id", "name": tool.name,
                                "args": {"value": "cancel"}})
        finally:
            clear_cancel_context()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(invoke_cancel)
            assert started.wait(2)
            other = pool.submit(tool.invoke, {"type": "tool_call", "id": "other-id", "name": tool.name,
                                              "args": {"value": "other"}})
            ctx.request_cancel()
            result = future.result(timeout=3)
            assert cleaned.is_set()
            assert result.status == "error" and result.tool_call_id == "cancel-id"
            assert other.result(timeout=3).artifact == {"value": "other"}
        # A cancel arriving after completion cannot overwrite that result.
        assert result.status == "error"
    finally:
        bridge.close()
    assert not bridge._thread.is_alive()


def test_loop_self_wait_rejected_and_closed_bridge():
    bridge = AsyncBridge()
    async def self_wait():
        with pytest.raises(RuntimeError, match="synchronously wait"):
            bridge.run(asyncio.sleep(0))
    bridge.run(self_wait())
    bridge.close()
    with pytest.raises(RuntimeError, match="closed"):
        bridge.run(asyncio.sleep(0))


def test_validation_schema_and_tool_message_unchanged():
    async def value(number: int):
        """Return a number."""
        return ([{"type": "text", "text": str(number)}], {"number": number})
    original = StructuredTool.from_function(coroutine=value, response_format="content_and_artifact")
    bridge = AsyncBridge()
    try:
        tool = SyncMCPTool(original, bridge, profile="test", url="http://localhost/mcp")
        assert tool.tool_call_schema.model_json_schema() == original.tool_call_schema.model_json_schema()
        result = tool.invoke({"type": "tool_call", "id": "id", "name": tool.name, "args": {"number": 4}})
        assert isinstance(result, ToolMessage)
        assert result.content == [{"type": "text", "text": "4"}]
        assert result.artifact == {"number": 4}
        assert tool.run({"number": 4}) == result.content
        assert asyncio.run(tool.arun({"number": 4})) == result.content
    finally:
        bridge.close()


def test_runner_retains_lease_until_cancel_cleanup_drains(monkeypatch):
    from deepagents.backends import StateBackend
    from langchain_core.messages import AIMessage
    from agent.factory import create_agent
    from agent.mcp.client import MCPToolSet
    from agent.runner import AgentRunner
    from tests.conftest import scripted_model
    entered = threading.Event()
    cleanup = threading.Event()
    release = threading.Event()
    async def stubborn():
        """Take time to finish local cancellation cleanup."""
        entered.set()
        try:
            await asyncio.sleep(60)
        finally:
            cleanup.set()
            while not release.is_set():
                await asyncio.sleep(.01)
        return "done"
    original = StructuredTool.from_function(coroutine=stubborn)
    bridge = AsyncBridge()
    tool = SyncMCPTool(original, bridge, profile="test", url="http://localhost/mcp")
    prepared = create_agent(model=scripted_model([
        AIMessage(content="", tool_calls=[{"name": tool.name, "id": "slow", "args": {}}]),
        AIMessage(content="done")]), backend=StateBackend(), extra_tools=[tool])
    runner = AgentRunner(prepared=prepared)
    runner._mcp_tools = MCPToolSet(bridge, (tool,))
    monkeypatch.setattr("agent.runner.CLOSE_DRAIN_SECONDS", .05)
    try:
        assert runner.invoke("slow").status == "waiting_confirmation"
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(runner.approve_tool, "slow")
            try:
                assert entered.wait(2)
                runner.request_cancel()
                assert cleanup.wait(2)
                with pytest.raises(RuntimeError, match="lease is kept"):
                    runner.close()
                assert runner._runtime.lease is not None
                assert not future.done()
            finally:
                release.set()
            assert future.result(timeout=3).status == "cancelled"
    finally:
        release.set()
        runner.close()
    assert not bridge._thread.is_alive()
