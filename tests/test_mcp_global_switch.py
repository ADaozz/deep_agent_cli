"""Global MCP enable/disable: catalog, connections, graph tools, and /mcp."""
from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from functools import partial, wraps
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage
from deepagents.backends import StateBackend

from agent.cli.mcp_ui import handle_mcp_command
from agent.cli.state import CliState
from agent.mcp.adapter import LoadedServer
from agent.mcp.config import atomic_write_text
from agent.mcp.manager import MCPManager, ServerStatus
from agent.mcp.middleware import MCP_HOST_PROMPT
from agent.permission import PermissionMode
from agent.runner import AgentRunner, InterruptKind
from agent.sandbox import BackendSelection, ExecutionMode
from tests.conftest import graph_tool_names, scripted_model
from tests.mcp_fixture import HttpFixture, stdio_argv


def async_test(func):
    @wraps(func)
    def run(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))
    return run


class _App:
    def __init__(self, runner: AgentRunner) -> None:
        self.runner = runner
        self.state = CliState()
        self.interaction = None
        self._mcp_draft: dict[str, str] = {}
        self.status = ""
        self.application = SimpleNamespace(invalidate=lambda: None)

    async def _run_blocking(self, func, /, *args, **kwargs):
        return await asyncio.to_thread(partial(func, *args, **kwargs))

    def _apply_event(self, _event: object, announce: bool = False) -> None:
        return None

    def set_status(self, text: str, transient: bool = True) -> None:
        self.status = text


def _native(graph: object) -> set[str]:
    return {name for name in graph_tool_names(graph) if not name.startswith("mcp")}


def _runner(manager: MCPManager, messages: list[AIMessage] | None = None) -> AgentRunner:
    return AgentRunner(
        model=scripted_model(messages or [AIMessage(content="done")]),
        backend=StateBackend(),
        mcp_manager=manager,
    )


def test_disabled_catalog_does_not_connect_or_spawn(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({
        "enabled": False,
        "servers": {
            "local": {"transport": "stdio", "command": "python", "args": ["-c", "import time; time.sleep(30)"]},
            "remote": {
                "transport": "streamable_http",
                "url": "https://example.invalid/mcp",
                "auth": {"type": "oauth"},
            },
        },
    }), encoding="utf-8")
    opened: list[str] = []
    monkeypatch.setattr(
        "agent.mcp.manager.open_server",
        lambda config, *args, **kwargs: opened.append(config.name) or LoadedServer(None, [], []),
    )
    manager = MCPManager(path, connect_timeout=2)
    try:
        manager.load()
        assert manager.enabled is False
        assert manager.connect_pending().ok
        assert opened == []
        assert manager.test("local").ok is False
        assert "enable" in manager.test("local").message
        assert manager.add_from_fields(
            name="other", transport="streamable_http", url="https://example.invalid/mcp",
        ).ok is False
        assert opened == []
        assert "other" not in path.read_text(encoding="utf-8")
    finally:
        manager.close()


def test_switch_persists_and_failed_write_keeps_the_previous_file(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "mcp.json"
    manager = MCPManager(path)
    try:
        manager.load()
        assert not path.exists()
        assert manager.set_enabled(True).changed is False
        assert not path.exists()
        assert manager.set_enabled(False).ok
        assert json.loads(path.read_text(encoding="utf-8"))["enabled"] is False
        before = path.read_bytes()
        monkeypatch.setattr(
            "agent.mcp.manager.atomic_write_text",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )
        failed = manager.set_enabled(True)
        assert not failed.ok
        assert manager.enabled is False
        assert path.read_bytes() == before
        monkeypatch.setattr("agent.mcp.manager.atomic_write_text", atomic_write_text)
        assert manager.set_enabled(True).ok
        manager.close()
        restored = MCPManager(path)
        try:
            restored.load()
            assert restored.enabled is True
        finally:
            restored.close()
    finally:
        if not manager._closed:
            manager.close()


def test_disable_cancels_the_round_and_a_late_client_cannot_republish(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {
        "oauth": {
            "transport": "streamable_http",
            "url": "https://example.invalid/mcp",
            "auth": {"type": "oauth"},
        },
    }}), encoding="utf-8")
    started, release = threading.Event(), threading.Event()
    closed: list[object] = []
    calls = {"count": 0}

    class Client:
        async def close(self) -> None:
            closed.append(self)

    def connect(config, _bridge, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            started.set()
            assert kwargs["cancel_event"].wait(5)
            assert release.wait(5)
            return LoadedServer(Client(), [], [])
        return LoadedServer(Client(), [], [])

    monkeypatch.setattr("agent.mcp.manager.open_server", connect)
    manager = MCPManager(path, connect_timeout=5)
    monkeypatch.setattr(manager, "_close_clients", lambda clients: closed.extend(client for client in clients if client))
    try:
        manager.load()
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(manager.test, "oauth")
            assert started.wait(5)
            assert manager.set_enabled(False).ok
            assert manager.enabled is False
            release.set()
            assert pending.result(5).ok is False
        assert manager.snapshot()[1] == []
        assert calls["count"] == 1
        assert manager.set_enabled(True).ok
        assert manager.connect_pending().ok
        assert calls["count"] == 2
        again = manager.set_enabled(True)
        assert again.changed is False
        assert calls["count"] == 2
    finally:
        release.set()
        manager.close()


def test_repeated_enable_disable_is_idempotent_and_reaps_stdio(tmp_path: Path) -> None:
    executable, script = stdio_argv()
    parent = os.getpid()
    before = _children(parent)
    path = tmp_path / "mcp.json"
    manager = MCPManager(path, call_timeout=8, connect_timeout=8)
    try:
        added = manager.add_from_fields(name="local", transport="stdio", command=executable, args=[script])
        assert added.ok, added.message
        runner = _runner(manager)
        try:
            assert "mcp__local__echo" in graph_tool_names(runner.prepared.graph)
            spawned = _children(parent) - before
            assert spawned
            disabled = manager.set_enabled(False)
            assert disabled.ok and disabled.changed
            saved = path.read_bytes()
            repeat = manager.set_enabled(False)
            assert repeat.ok and repeat.changed is False
            assert path.read_bytes() == saved
            runner._apply_pending_runtime_config()
            assert "mcp__local__echo" not in graph_tool_names(runner.prepared.graph)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and any(Path(f"/proc/{pid}").exists() for pid in spawned):
                time.sleep(0.05)
            assert not any(Path(f"/proc/{pid}").exists() for pid in spawned)
            assert manager.set_enabled(True).ok
            assert manager.connect_pending().ok
            runner._apply_pending_runtime_config()
            assert "mcp__local__echo" in graph_tool_names(runner.prepared.graph)
            assert manager.set_enabled(False).ok
            runner._apply_pending_runtime_config()
            assert not manager.toolset_dirty()
            assert _native(runner.prepared.graph)
        finally:
            runner.close()
    finally:
        manager.close()


def test_graph_hides_mcp_until_enabled_and_keeps_native_tools(tmp_path: Path, http_mcp: HttpFixture) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({
        "enabled": False,
        "servers": {"local": {"transport": "streamable_http", "url": http_mcp.url, "enabled": True}},
    }), encoding="utf-8")
    manager = MCPManager(path, call_timeout=8, connect_timeout=5)
    try:
        manager.load()
        runner = _runner(manager)
        try:
            hidden = graph_tool_names(runner.prepared.graph)
            native = _native(runner.prepared.graph)
            assert not any(name.startswith("mcp") for name in hidden)
            assert "mcp_add" not in hidden
            assert MCP_HOST_PROMPT not in runner.prepared.system_prompt
            assert not manager.toolset_dirty()
            assert manager.set_enabled(True).ok
            assert manager.connect_pending().ok
            runner._apply_pending_runtime_config()
            restored = graph_tool_names(runner.prepared.graph)
            assert {"mcp_add", "mcp_list", "mcp_remove", "mcp__local__echo"} <= restored
            assert MCP_HOST_PROMPT in runner.prepared.system_prompt
            assert _native(runner.prepared.graph) == native
            assert manager.set_enabled(False).ok
            runner._apply_pending_runtime_config()
            assert not any(name.startswith("mcp") for name in graph_tool_names(runner.prepared.graph))
            assert _native(runner.prepared.graph) == native
            assert not manager.toolset_dirty()
        finally:
            runner.close()
    finally:
        manager.close()


def test_permission_and_sandbox_switches_do_not_change_the_mcp_switch(
    tmp_path: Path, http_mcp: HttpFixture, monkeypatch,
) -> None:
    from unittest.mock import patch

    manager = MCPManager(tmp_path / "mcp.json", call_timeout=8, connect_timeout=5)
    try:
        assert manager.add_from_fields(name="local", transport="streamable_http", url=http_mcp.url).ok
        with patch(
            "agent.factory.select_backend",
            return_value=BackendSelection(StateBackend(), ExecutionMode.SANDBOXED),
        ):
            runner = AgentRunner(
                model=scripted_model([AIMessage(content="done")]),
                mcp_manager=manager,
            )
            try:
                assert manager.set_enabled(False).ok
                runner._apply_pending_runtime_config()
                mode = runner.permission_mode()
                runner.request_permission_change(PermissionMode.ALLOW)
                runner._apply_pending_runtime_config()
                assert runner.permission_mode() is PermissionMode.ALLOW
                assert manager.enabled is False
                runner.request_permission_change(mode)
                runner._apply_pending_runtime_config()
                monkeypatch.setattr(runner, "_apply_sandbox_mode", lambda _mode: None)
                runner.request_sandbox_mode("full-access")
                runner._apply_pending_runtime_config()
                assert runner.sandbox_mode() == "full-access"
                assert manager.enabled is False
                assert json.loads(manager.path.read_text(encoding="utf-8"))["enabled"] is False
                assert not any(name.startswith("mcp") for name in graph_tool_names(runner.prepared.graph))
            finally:
                runner.close()
    finally:
        manager.close()


def test_disable_during_tool_call_and_pending_approval_keeps_the_current_graph(
    tmp_path: Path, http_mcp: HttpFixture,
) -> None:
    manager = MCPManager(tmp_path / "mcp.json", call_timeout=8, connect_timeout=5)
    assert manager.add_from_fields(name="local", transport="streamable_http", url=http_mcp.url).ok
    runner = _runner(manager, [
        AIMessage(content="", tool_calls=[{
            "id": "live", "name": "mcp__local__echo", "args": {"text": "still running"},
        }]),
        AIMessage(content="done"),
    ])
    started, release = threading.Event(), threading.Event()
    try:
        assert runner.invoke("call").status == "waiting_confirmation"
        original = runner.prepared.graph
        tool = original.nodes["tools"].bound.tools_by_name["mcp__local__echo"]
        invoke = tool.func

        def held(**kwargs):
            started.set()
            assert release.wait(5)
            return invoke(**kwargs)

        tool.func = held
        waiting = runner.current_interrupt()
        assert waiting is not None and waiting.kind is InterruptKind.WAITING_CONFIRMATION
        assert manager.set_enabled(False).ok
        assert runner.prepared.graph is original
        assert runner.current_interrupt() is not None
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(runner.approve_tool, "live")
            assert started.wait(5)
            assert "mcp__local__echo" in graph_tool_names(runner.prepared.graph)
            release.set()
            result = pending.result(5)
        assert result.status == "completed", result.error
        assert any(
            "still running" in str(message.content)
            for message in runner.prepared.graph.get_state(runner._thread_config()).values["messages"]
            if isinstance(message, ToolMessage)
        )
        assert "mcp__local__echo" not in graph_tool_names(runner.prepared.graph)
        assert manager.enabled is False
    finally:
        release.set()
        runner.close()
        manager.close()


@async_test
async def test_stale_connection_does_not_overwrite_disabled_status(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {
        "local": {"transport": "streamable_http", "url": "https://example.invalid/mcp"},
    }}), encoding="utf-8")
    started, release = threading.Event(), threading.Event()

    def connect(_config, _bridge, **_kwargs):
        started.set()
        assert release.wait(5)
        return LoadedServer(None, [], [])

    monkeypatch.setattr("agent.mcp.manager.open_server", connect)
    manager = MCPManager(path, connect_timeout=5)
    manager.load()
    runner = _runner(manager)
    app = _App(runner)
    try:
        task = asyncio.create_task(handle_mcp_command(app, "test local"))
        assert await asyncio.to_thread(started.wait, 5)
        await handle_mcp_command(app, "disable")
        assert app.status == "MCP: Disabled"
        release.set()
        await task
        assert app.status == "MCP: Disabled"
        assert not any("connected" in block.content for block in app.state.blocks)
    finally:
        release.set()
        runner.close()
        manager.close()


@async_test
async def test_mcp_commands_queue_disable_and_restore_on_enable(tmp_path: Path, http_mcp: HttpFixture) -> None:
    manager = MCPManager(tmp_path / "mcp.json", call_timeout=8, connect_timeout=5)
    assert manager.add_from_fields(name="local", transport="streamable_http", url=http_mcp.url).ok
    runner = _runner(manager)
    app = _App(runner)
    try:
        await handle_mcp_command(app, "")
        assert "MCP: Enabled" in app.state.blocks[-1].content
        assert "local" in app.state.blocks[-1].content
        app.state.running = True
        await handle_mcp_command(app, "disable")
        queued = app.state.blocks[-1].content
        assert "MCP disable queued." in queued
        assert "next safe point" in queued
        assert "MCP: Disabled" not in queued
        assert "mcp__local__echo" in graph_tool_names(runner.prepared.graph)
        app.state.running = False
        await handle_mcp_command(app, "")
        assert "MCP disable queued." in app.state.blocks[-1].content
        from agent.cli.mcp_ui import _apply_when_idle

        await _apply_when_idle(app)
        await handle_mcp_command(app, "")
        assert app.state.blocks[-1].content.startswith("MCP: Disabled")
        assert "inactive" in app.state.blocks[-1].content
        await handle_mcp_command(app, "test local")
        assert any("Run /mcp enable" in block.content for block in app.state.blocks)
        assert manager.get("local") is not None
        before = manager.path.read_bytes()
        await handle_mcp_command(app, "disable")
        assert manager.path.read_bytes() == before
        await handle_mcp_command(app, "enable")
        operation = getattr(app, "_mcp_operation", None)
        if operation is not None:
            await operation
        await _apply_when_idle(app)
        assert manager.enabled is True
        assert "mcp__local__echo" in graph_tool_names(runner.prepared.graph)
        saved = manager.path.read_bytes()
        await handle_mcp_command(app, "enable")
        assert manager.path.read_bytes() == saved
    finally:
        runner.close()


def _children(parent: int) -> set[int]:
    found: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text()
            ppid = int(raw.rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        if ppid == parent:
            found.add(int(entry.name))
    return found
