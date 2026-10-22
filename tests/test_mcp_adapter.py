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


def test_stdio_timeout_and_cancel_reap_processes(tmp_path):
    from tests.mcp_fixture import stdio_argv
    from tests.test_mcp_manager import _children
    import os

    for cancel in (False, True):
        before = _children(os.getpid())
        manager = MCPManager(tmp_path / f"{cancel}.json", call_timeout=.2 if not cancel else 8,
                             connect_timeout=8)
        executable, script = stdio_argv()
        spawned = set()
        try:
            assert manager.add_from_fields(
                name="local", transport="stdio", command=executable, args=[script],
            ).ok
            spawned = _children(os.getpid()) - before
            sleep = next(t for t in manager.get("local").tools if t.name.endswith("__sleep"))
            event = threading.Event()
            timer = threading.Timer(.1, event.set) if cancel else None
            if timer is not None:
                timer.start()
            set_cancel_context(ToolCancelContext("mcp__local__sleep", "call", event))
            try:
                outcome = text_of(sleep.invoke({"seconds": 3}))
                assert ("cancel" if cancel else "timed out") in outcome.lower()
            finally:
                clear_cancel_context()
                if timer is not None:
                    timer.cancel()
                    timer.join()
        finally:
            manager.close()
        assert spawned
        assert not any(Path(f"/proc/{pid}").exists() for pid in spawned)


def test_sync_and_async_results_errors_and_redaction_match(tmp_path, http_mcp, monkeypatch):
    import asyncio

    secret = "p2-bearer-secret-xyz"
    monkeypatch.setenv("P2_TOKEN", secret)
    manager = MCPManager(tmp_path / "mcp.json", connect_timeout=5)
    try:
        assert manager.add_from_fields(
            name="local", transport="streamable_http", url=http_mcp.url,
            auth_type="bearer", token_env="P2_TOKEN",
        ).ok
        tools = {tool.name: tool for tool in manager.get("local").tools}

        def without_ids(value):
            if isinstance(value, list):
                return [without_ids(item) for item in value]
            if isinstance(value, dict):
                return {k: without_ids(v) for k, v in value.items() if k != "id"}
            return value

        async def check():
            echo = tools["mcp__local__echo"]
            sync = echo.invoke({"text": secret})
            asynchronous = await echo.ainvoke({"text": secret})
            assert type(sync) is type(asynchronous)
            assert without_ids(sync) == without_ids(asynchronous)
            assert secret not in str(asynchronous)
            assert "[redacted]" in str(asynchronous)
            failed = tools["mcp__local__fail"]
            assert without_ids(failed.invoke({})) == without_ids(await failed.ainvoke({}))
        asyncio.run(check())
    finally:
        manager.close()


def test_async_cancellation_reaches_http_and_stdio_server_and_client_recovers(tmp_path, http_mcp):
    import asyncio
    from tests.mcp_fixture import stdio_argv

    async def exists(path):
        async with asyncio.timeout(4):
            while not path.exists():
                await asyncio.sleep(.01)

    for transport in ("streamable_http", "stdio"):
        manager = MCPManager(tmp_path / f"{transport}.json", call_timeout=10, connect_timeout=8)
        fields = {"url": http_mcp.url} if transport == "streamable_http" else {
            "command": stdio_argv()[0], "args": stdio_argv()[1:],
        }
        try:
            assert manager.add_from_fields(name="local", transport=transport, **fields).ok
            tools = {tool.name: tool for tool in manager.get("local").tools}
            started = tmp_path / f"{transport}-started"
            cancelled = tmp_path / f"{transport}-cancelled"

            async def check():
                import pytest
                baseline = manager._bridge.pending_task_count()
                call = asyncio.create_task(tools["mcp__local__wait_for_cancel"].ainvoke({
                    "started": str(started), "cancelled": str(cancelled),
                }))
                await exists(started)
                call.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await call
                await exists(cancelled)
                result = await tools["mcp__local__echo"].ainvoke({"text": "recovered"})
                assert "recovered" in text_of(result)
                assert manager._bridge.pending_task_count() <= baseline
            asyncio.run(check())
        finally:
            manager.close()
        assert not manager._bridge._thread.is_alive()


def test_async_timeout_and_cancel_context_match_sync(tmp_path, http_mcp):
    import asyncio

    manager = MCPManager(tmp_path / "mcp.json", call_timeout=.15, connect_timeout=5)
    try:
        assert manager.add_from_fields(name="local", transport="streamable_http", url=http_mcp.url).ok
        tool = next(t for t in manager.get("local").tools if t.name.endswith("__wait_for_cancel"))

        async def check():
            for asynchronous in (False, True):
                started, cancelled = tmp_path / f"start-{asynchronous}", tmp_path / f"cancel-{asynchronous}"
                fields = {"started": str(started), "cancelled": str(cancelled)}
                begin = time.monotonic()
                result = await tool.ainvoke(fields) if asynchronous else tool.invoke(fields)
                assert time.monotonic() - begin < 3
                assert "timed out" in text_of(result).lower()
                async with asyncio.timeout(3):
                    while not cancelled.exists():
                        await asyncio.sleep(.01)
            event = threading.Event()
            set_cancel_context(ToolCancelContext(tool.name, "async-context", event))
            try:
                started, cancelled = tmp_path / "context-start", tmp_path / "context-cancel"
                call = asyncio.create_task(tool.ainvoke({
                    "started": str(started), "cancelled": str(cancelled),
                }))
                async with asyncio.timeout(3):
                    while not started.exists():
                        await asyncio.sleep(.01)
                event.set()
                assert "cancel" in text_of(await call).lower()
                async with asyncio.timeout(3):
                    while not cancelled.exists():
                        await asyncio.sleep(.01)
            finally:
                clear_cancel_context()
        asyncio.run(check())
    finally:
        manager.close()


def test_bridge_async_executes_on_own_loop_and_cleans_up_before_return():
    import asyncio
    import pytest

    bridge = SyncAsyncBridge()
    started, cleaned = threading.Event(), threading.Event()

    async def work():
        assert asyncio.get_running_loop() is bridge._loop
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            await asyncio.sleep(.02)
            cleaned.set()

    async def check():
        call = asyncio.create_task(bridge.acall(work(), timeout=5))
        while not started.is_set():
            await asyncio.sleep(.01)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert cleaned.is_set()
        assert bridge.pending_task_count() == 0
        assert await bridge.acall(asyncio.sleep(0, result="ready"), timeout=.01) == "ready"
    try:
        asyncio.run(check())
    finally:
        bridge.shutdown()


def test_alias_keeps_remote_schema_description_and_actual_call_target():
    import asyncio
    from langchain_core.tools import StructuredTool
    from agent.mcp.adapter import _bridge_tool
    from agent.mcp.names import external_tool_name

    async def remote(value: str) -> str:
        return value
    original = StructuredTool.from_function(
        coroutine=remote, name="read.file", description="Original documentation.",
    )
    bridge = SyncAsyncBridge()
    try:
        wrapped = _bridge_tool(
            original, name=external_tool_name("local", original.name), bridge=bridge,
            timeout=3, server="local", remote_name=original.name, secrets=[],
        )
        assert original.name == "read.file"
        assert wrapped.args_schema is original.args_schema
        assert wrapped.description.startswith(original.description)
        assert wrapped.metadata["deep_agent_mcp"] == {"server": "local", "remote_name": "read.file"}
        assert wrapped.coroutine is not original.coroutine
        assert wrapped.invoke({"value": "sync"}) == "sync"
        assert asyncio.run(wrapped.ainvoke({"value": "async"})) == "async"
    finally:
        bridge.shutdown()
