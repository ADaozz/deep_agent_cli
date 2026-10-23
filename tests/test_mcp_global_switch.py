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
from agent.mcp.manager import MCPManager, MCPResult, ServerStatus
from agent.mcp.middleware import MCP_HOST_PROMPT
from agent.permission import PermissionMode
from agent.runner import AgentRunner, InterruptKind
from agent.sandbox import BackendSelection, ExecutionMode
from tests.conftest import graph_tool_names, scripted_model
from tests.mcp_fixture import HttpFixture, stdio_argv, text_of


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
        self.state.status = text


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


def test_new_epoch_does_not_reuse_or_erase_the_next_connection(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {
        "local": {"transport": "stdio", "command": "fixture"},
    }}), encoding="utf-8")
    release_a, release_b = threading.Event(), threading.Event()
    entered_b = threading.Event()
    handshakes: list[str] = []
    closed: list[str] = []

    class Client:
        def __init__(self, label: str) -> None:
            self.label = label

        async def close(self) -> None:
            closed.append(self.label)

    def connect(_config, _bridge, **kwargs):
        label = "A" if "A" not in handshakes else "B"
        if label == "B" and "B" in handshakes:
            handshakes.append("extra")
            return LoadedServer(Client("extra"), [], [])
        handshakes.append(label)
        client = Client(label)
        if label == "A":
            assert kwargs["cancel_event"].wait(5)
            assert release_a.wait(5)
            return LoadedServer(client, [], [])
        from langchain_core.tools import StructuredTool

        def echo() -> str:
            """Return the new connection marker."""
            return "from-b"

        entered_b.set()
        assert release_b.wait(5)
        tool = StructuredTool.from_function(echo, name="mcp__local__echo")
        return LoadedServer(client, [tool], [])

    monkeypatch.setattr("agent.mcp.manager.open_server", connect)
    manager = MCPManager(path, connect_timeout=5)
    manager.load()
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(manager.connect_pending)
            _wait_until(lambda: handshakes == ["A"])
            assert manager.set_enabled(False).ok
            assert manager.set_enabled(True).ok
            second = pool.submit(manager.connect_pending)
            assert entered_b.wait(5)
            b_key = next(key for key in manager._connecting if key[1] == manager._activation_epoch)
            assert any(key[1] != manager._activation_epoch for key in manager._connecting)
            joined = threading.Event()
            future = manager._connecting[b_key]
            original_result = future.result

            def result_and_signal(*args, **kwargs):
                joined.set()
                return original_result(*args, **kwargs)

            future.result = result_and_signal  # type: ignore[method-assign]
            third = pool.submit(manager.test, "local")
            assert joined.wait(5)
            assert handshakes == ["A", "B"]
            assert not third.done()
            release_a.set()
            assert not first.result(5).ok
            assert b_key in manager._connecting
            assert not any(key[1] != manager._activation_epoch for key in manager._connecting)
            release_b.set()
            assert second.result(5).ok
            assert third.result(5).ok
        state = manager.get("local")
        assert state is not None and state.status is ServerStatus.CONNECTED
        live = manager._states["local"].client
        assert live.label == "B"
        assert "from-b" in text_of(state.tools[0].invoke({}))
        assert closed == ["A"]
        runner = _runner(manager)
        try:
            native = _native(runner.prepared.graph)
            assert "mcp__local__echo" in graph_tool_names(runner.prepared.graph)
            assert not manager.toolset_dirty()
            assert manager.set_enabled(False).ok
            runner._apply_pending_runtime_config()
            assert not any(name.startswith("mcp") for name in graph_tool_names(runner.prepared.graph))
            assert _native(runner.prepared.graph) == native
            assert not manager.toolset_dirty()
        finally:
            runner.close()
    finally:
        release_a.set()
        release_b.set()
        manager.close()


def test_final_switch_determines_the_graph(tmp_path: Path, http_mcp: HttpFixture) -> None:
    manager = MCPManager(tmp_path / "mcp.json", call_timeout=8, connect_timeout=5)
    try:
        assert manager.add_from_fields(
            name="local", transport="streamable_http", url=http_mcp.url,
        ).ok
        runner = _runner(manager)
        try:
            native = _native(runner.prepared.graph)
            assert "mcp__local__echo" in graph_tool_names(runner.prepared.graph)
            assert manager.set_enabled(False).ok
            assert manager.set_enabled(True).ok
            assert manager.connect_pending().ok
            assert manager.set_enabled(False).ok
            runner._apply_pending_runtime_config()
            assert manager.enabled is False
            assert not any(name.startswith("mcp") for name in graph_tool_names(runner.prepared.graph))
            assert _native(runner.prepared.graph) == native
            assert not manager.toolset_dirty()
            assert manager.set_enabled(True).ok
            assert manager.connect_pending().ok
            runner._apply_pending_runtime_config()
            assert {"mcp_add", "mcp_list", "mcp_remove", "mcp__local__echo"} <= graph_tool_names(runner.prepared.graph)
            assert _native(runner.prepared.graph) == native
            assert not manager.toolset_dirty()
        finally:
            runner.close()
    finally:
        manager.close()


@async_test
async def test_switch_commands_keep_submission_order_and_latest_status(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "mcp.json"
    manager = MCPManager(path)
    manager.load()
    runner = _runner(manager)
    app = _App(runner)
    try:
        await _overlap_switches(app, "disable", "enable")
        operation = getattr(app, "_mcp_operation", None)
        if operation is not None:
            await operation
        assert manager.enabled is True
        assert app.status == "MCP: Enabled"
        enabled_at = max(
            index for index, block in enumerate(app.state.blocks)
            if block.content.startswith("MCP: Enabled")
        )
        assert not any(
            block.content.startswith("MCP: Disabled") for block in app.state.blocks[enabled_at + 1:]
        )
        assert json.loads(path.read_text(encoding="utf-8"))["enabled"] is True

        await _overlap_switches(app, "enable", "disable")
        assert manager.enabled is False
        assert app.status == "MCP: Disabled"

        await _overlap_switches(app, "disable", "enable", "disable")
        assert manager.enabled is False
        assert app.status == "MCP: Disabled"
        saved = path.read_bytes()
        stamp = path.stat().st_mtime_ns
        await handle_mcp_command(app, "disable")
        assert path.read_bytes() == saved
        assert path.stat().st_mtime_ns == stamp

        path.write_text(json.dumps({
            "enabled": False,
            "servers": {"local": {"transport": "stdio", "command": "fixture", "enabled": True}},
        }), encoding="utf-8")
        manager.load()
        opened: list[str] = []

        def connect(config, _bridge, **_kwargs):
            opened.append(config.name)
            return LoadedServer(None, [], [])

        monkeypatch.setattr("agent.mcp.manager.open_server", connect)
        await handle_mcp_command(app, "enable")
        operation = getattr(app, "_mcp_operation", None)
        if operation is not None:
            await operation
        await handle_mcp_command(app, "enable")
        assert opened == ["local"]
    finally:
        runner.close()
        manager.close()


@async_test
async def test_failed_switch_preserves_state_and_a_later_command_wins(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "mcp.json"
    manager = MCPManager(path)
    manager.load()
    runner = _runner(manager)
    app = _App(runner)
    real = atomic_write_text
    calls = {"n": 0}

    def flaky(target, text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        real(target, text)

    monkeypatch.setattr("agent.mcp.manager.atomic_write_text", flaky)
    try:
        await handle_mcp_command(app, "disable")
        assert manager.enabled is True
        assert app.status == "MCP update failed"
        assert not path.exists()
        await handle_mcp_command(app, "disable")
        assert manager.enabled is False
        assert json.loads(path.read_text(encoding="utf-8"))["enabled"] is False
        assert app.status == "MCP: Disabled"
    finally:
        runner.close()
        manager.close()


def test_disable_cancels_oauth_wait_and_enable_starts_a_new_attempt(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    from fastmcp.client.auth import OAuth
    from fastmcp.server.auth.auth import ClientRegistrationOptions
    from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider

    from agent.mcp.oauth import ManagedOAuth

    started, cancelled = threading.Event(), threading.Event()
    opened: list[str] = []
    managed_calls: list[str] = []
    original = ManagedOAuth.redirect_handler

    async def count_managed(self, url: str) -> None:
        managed_calls.append(url)
        await original(self, url)

    async def delayed_redirect(self, url: str) -> None:
        started.set()
        try:
            await asyncio.sleep(30)
            opened.append(url)
        finally:
            cancelled.set()

    monkeypatch.setattr(ManagedOAuth, "redirect_handler", count_managed)
    monkeypatch.setattr(OAuth, "redirect_handler", delayed_redirect)
    protected = HttpFixture(auth_factory=lambda base: InMemoryOAuthProvider(
        base_url=base, client_registration_options=ClientRegistrationOptions(enabled=True),
    ))
    manager = MCPManager(tmp_path / "mcp.json", oauth_callback_timeout=120)
    manager.path.write_text(json.dumps({"servers": {"oauth": {
        "transport": "streamable_http", "url": protected.url, "auth": {"type": "oauth"},
    }}}))
    manager.load()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(manager.connect_pending)
            assert started.wait(5)
            assert manager.set_enabled(False).ok
            assert manager.set_enabled(True).ok
            begun = time.monotonic()
            second = pool.submit(manager.connect_pending)
            second_result = second.result(5)
            assert time.monotonic() - begun < 5
            assert not second_result.ok
            assert "already attempted" in second_result.message
            assert not first.result(5).ok
            assert cancelled.wait(3)
        assert len(managed_calls) >= 2
        assert opened == []
        assert manager._oauth_browser_attempts.get("oauth") is not None
    finally:
        manager.close()
        protected.close()


def test_oauth_enable_after_disable_reuses_cached_authorization_and_releases_tools(
    tmp_path: Path, monkeypatch,
) -> None:
    import threading
    import urllib.request
    from fastmcp.server.auth.auth import ClientRegistrationOptions
    from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider

    browsers: list[threading.Thread] = []
    errors: list[BaseException] = []

    def browser(url, *_args, **_kwargs):
        def follow() -> None:
            deadline = time.monotonic() + 5
            while True:
                try:
                    with urllib.request.urlopen(url, timeout=3) as response:
                        response.read()
                    return
                except Exception as exc:
                    if time.monotonic() >= deadline:
                        errors.append(exc)
                        return
                    time.sleep(0.05)

        thread = threading.Thread(target=follow, daemon=True)
        browsers.append(thread)
        thread.start()
        return True

    monkeypatch.setattr("webbrowser.open", browser)
    protected = HttpFixture(auth_factory=lambda base: InMemoryOAuthProvider(
        base_url=base, client_registration_options=ClientRegistrationOptions(enabled=True),
    ))
    manager = MCPManager(tmp_path / "mcp.json", connect_timeout=5, oauth_callback_timeout=10)
    manager.path.write_text(json.dumps({"servers": {"oauth": {
        "transport": "streamable_http", "url": protected.url, "auth": {"type": "oauth"},
    }}}))
    manager.load()
    try:
        assert manager.connect_pending().ok, errors
        check = next(tool for tool in manager.get("oauth").tools if tool.name == "mcp__oauth__auth_check")
        assert "subject:" in text_of(check.invoke({}))
        assert browsers
        browser_count = len(browsers)
        assert manager.set_enabled(False).ok
        assert manager.get("oauth").tools == []
        assert manager.set_enabled(True).ok
        assert manager.connect_pending().ok, errors
        check = next(tool for tool in manager.get("oauth").tools if tool.name == "mcp__oauth__auth_check")
        assert "subject:" in text_of(check.invoke({}))
        assert len(browsers) == browser_count
        assert manager.set_enabled(False).ok
        assert manager.snapshot()[1] == []
        assert not manager.get("oauth").tools
        for thread in browsers:
            thread.join(3)
        assert not errors
    finally:
        manager.close()
        protected.close()


def test_future_keeps_the_epoch_captured_before_connect_starts(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {
        "local": {"transport": "stdio", "command": "fixture"},
    }}), encoding="utf-8")
    entered, release = threading.Event(), threading.Event()
    opened: list[str] = []

    class Client:
        async def close(self) -> None:
            return None

    def connect(config, _bridge, **_kwargs):
        opened.append(config.name)
        return LoadedServer(Client(), [], [])

    monkeypatch.setattr("agent.mcp.manager.open_server", connect)
    manager = MCPManager(path)
    manager.load()
    original = manager._activate_once

    def paused(server, *, allow_oauth_browser, cancel_event, expected_epoch):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(
            server,
            allow_oauth_browser=allow_oauth_browser,
            cancel_event=cancel_event,
            expected_epoch=expected_epoch,
        )

    monkeypatch.setattr(manager, "_activate_once", paused)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(manager.connect_pending)
            assert entered.wait(5)
            assert manager._connecting
            assert opened == []
            assert manager.set_enabled(False).ok
            assert manager.set_enabled(True).ok
            fresh = pool.submit(manager.connect_pending)
            assert fresh.result(5).ok
            assert opened == ["local"]
            kept = manager._states["local"].client
            release.set()
            assert not old.result(5).ok
            assert opened == ["local"]
            assert manager._states["local"].client is kept
    finally:
        release.set()
        manager.close()


def test_old_connect_batch_stops_for_a_new_enable_round(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {
        "alpha": {"transport": "stdio", "command": "fixture"},
        "beta": {"transport": "stdio", "command": "fixture"},
    }}), encoding="utf-8")
    entered, release = threading.Event(), threading.Event()
    opened: list[str] = []

    class Client:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self) -> None:
            return None

    def connect(config, _bridge, **_kwargs):
        opened.append(config.name)
        if config.name == "alpha" and opened.count("alpha") == 1:
            entered.set()
            assert release.wait(5)
        return LoadedServer(Client(config.name), [], [])

    monkeypatch.setattr("agent.mcp.manager.open_server", connect)
    manager = MCPManager(path)
    manager.load()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(manager.connect_pending)
            assert entered.wait(5)
            assert manager.set_enabled(False).ok
            assert manager.set_enabled(True).ok
            fresh = pool.submit(manager.connect_pending)
            assert fresh.result(5).ok
            assert opened.count("beta") == 1
            assert opened.count("alpha") == 2
            alpha = manager._states["alpha"].client
            beta = manager._states["beta"].client
            release.set()
            old_result = old.result(5)
            assert not old_result.ok
            assert old_result.message == "MCP connection round was superseded."
            assert opened.count("beta") == 1
            assert manager._states["alpha"].client is alpha
            assert manager._states["beta"].client is beta
            assert alpha.name == "alpha" and beta.name == "beta"
    finally:
        release.set()
        manager.close()


@async_test
async def test_switch_publish_queues_when_busy_and_does_not_retry_failure(tmp_path: Path, monkeypatch) -> None:
    from agent.runner import RunEvent

    manager = MCPManager(tmp_path / "mcp.json")
    manager.load()
    runner = _runner(manager)
    app = _App(runner)
    calls = {"n": 0}

    def busy(_runner):
        calls["n"] += 1
        return None

    monkeypatch.setattr("agent.cli.mcp_ui._apply_when_idle_worker", busy)
    try:
        await handle_mcp_command(app, "disable")
        assert calls["n"] == 1
        assert "queued" in app.state.blocks[-1].content
        assert "failed" not in app.status
        calls["n"] = 0

        def failed(_runner):
            calls["n"] += 1
            return [RunEvent(type="runtime_config_failed", content="boom", is_error=True)]

        monkeypatch.setattr("agent.cli.mcp_ui._apply_when_idle_worker", failed)
        await handle_mcp_command(app, "enable")
        assert calls["n"] == 1
        assert app.status == "MCP tool update failed; retry at the next safe point"
    finally:
        runner.close()
        manager.close()


@async_test
async def test_two_blocked_connects_do_not_stall_the_cli_executor(tmp_path: Path) -> None:
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from agent.cli.app import CliApplication
    from agent.cli.mcp_ui import _apply_when_idle_worker, _start_operation

    entered, release = threading.Event(), threading.Event()
    started = {"n": 0}
    gate = threading.Lock()

    def block() -> MCPResult:
        with gate:
            started["n"] += 1
            if started["n"] == 2:
                entered.set()
        assert release.wait(5)
        return MCPResult(True, False, "connected")

    manager = MCPManager(tmp_path / "mcp.json")
    manager.load()
    runner = _runner(manager)
    with create_pipe_input() as pipe:
        app = CliApplication(
            runner, config_dir=tmp_path / "config", input=pipe, output=DummyOutput(),
        )
        assert app._io_executor._max_workers == 2
        first = second = None
        try:
            first = _start_operation(app, block, quiet_success=True)
            second = _start_operation(app, block, quiet_success=True, force=True)
            assert first is not None and second is not None
            assert await asyncio.to_thread(entered.wait, 2)
            published = await asyncio.wait_for(
                app._run_blocking(_apply_when_idle_worker, app.runner), 1,
            )
            worked = await asyncio.wait_for(app._run_blocking(lambda: "agent-work"), 1)
            assert isinstance(published, list)
            assert worked == "agent-work"
        finally:
            release.set()
            for task in (first, second):
                if task is not None:
                    await task
            app._io_executor.shutdown(wait=False, cancel_futures=True)
            runner.close()


@async_test
async def test_quiet_connect_publishes_the_server_that_succeeded(tmp_path: Path, monkeypatch) -> None:
    from langchain_core.tools import StructuredTool

    from agent.cli.mcp_ui import _MCP_OPERATION_RUNNING, _start_operation

    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {
        "alpha": {"transport": "stdio", "command": "fixture"},
        "beta": {"transport": "stdio", "command": "fixture"},
    }}), encoding="utf-8")

    class Client:
        async def close(self) -> None:
            return None

    def connect(config, _bridge, **_kwargs):
        if config.name == "beta":
            raise RuntimeError("beta unreachable")

        def echo() -> str:
            """Marker from the server that connected."""
            return "alpha"

        return LoadedServer(Client(), [StructuredTool.from_function(echo, name="mcp__alpha__echo")], [])

    monkeypatch.setattr("agent.mcp.manager.open_server", connect)
    manager = MCPManager(path)
    manager.load()
    runner = _runner(manager)
    app = _App(runner)
    try:
        task = _start_operation(app, manager.connect_pending, quiet_success=True)
        assert task is not None
        await task
        assert "mcp__alpha__echo" in graph_tool_names(runner.prepared.graph)
        assert any(block.is_error and "beta" in block.content for block in app.state.blocks)
        assert app.status != _MCP_OPERATION_RUNNING
        assert not app._mcp_busy
    finally:
        runner.close()
        manager.close()


@async_test
async def test_quiet_connect_shows_errors_and_leaves_the_running_status(tmp_path: Path, monkeypatch) -> None:
    from agent.cli.mcp_ui import _MCP_OPERATION_RUNNING, _start_operation

    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {
        "alpha": {"transport": "stdio", "command": "fixture"},
        "beta": {"transport": "stdio", "command": "fixture"},
    }}), encoding="utf-8")

    def connect(_config, _bridge, **_kwargs):
        raise RuntimeError("unreachable")

    monkeypatch.setattr("agent.mcp.manager.open_server", connect)
    manager = MCPManager(path)
    manager.load()
    runner = _runner(manager)
    app = _App(runner)
    try:
        task = _start_operation(app, manager.connect_pending, quiet_success=True)
        assert task is not None
        await task
        errors = [block.content for block in app.state.blocks if block.is_error]
        assert errors
        assert "alpha" in errors[0] and "beta" in errors[0]
        assert "unreachable" in errors[0]
        assert app.status == "Ready"
        assert app.status != _MCP_OPERATION_RUNNING
        assert app._mcp_quiet_settled
        assert not app._mcp_busy
    finally:
        runner.close()
        manager.close()


def test_partial_batch_reports_superseded(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {
        "alpha": {"transport": "stdio", "command": "fixture"},
        "beta": {"transport": "stdio", "command": "fixture"},
    }}), encoding="utf-8")
    opened: list[str] = []

    class Client:
        async def close(self) -> None:
            return None

    def connect(config, _bridge, **_kwargs):
        opened.append(config.name)
        return LoadedServer(Client(), [], [])

    monkeypatch.setattr("agent.mcp.manager.open_server", connect)
    manager = MCPManager(path)
    manager.load()
    original = manager.test

    def stop_after_alpha(name: str, **kwargs):
        result = original(name, **kwargs)
        if name == "alpha":
            assert manager.set_enabled(False).ok
        return result

    manager.test = stop_after_alpha  # type: ignore[method-assign]
    try:
        result = manager.connect_pending()
        assert not result.ok
        assert result.message == "MCP connection round was superseded."
        assert opened == ["alpha"]
        assert manager.get("beta").status is ServerStatus.DISCONNECTED
    finally:
        manager.close()


@async_test
async def test_enable_does_not_wait_for_the_previous_connection(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {
        "local": {"transport": "stdio", "command": "fixture"},
    }}), encoding="utf-8")
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []

    class Client:
        async def close(self) -> None:
            return None

    def connect(config, _bridge, **_kwargs):
        calls.append(config.name)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return LoadedServer(Client(), [], [])

    monkeypatch.setattr("agent.mcp.manager.open_server", connect)
    manager = MCPManager(path)
    manager.load()
    runner = _runner(manager)
    app = _App(runner)
    previous = asyncio.create_task(handle_mcp_command(app, "test local"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await handle_mcp_command(app, "disable")
        await asyncio.wait_for(handle_mcp_command(app, "enable"), 2)
        assert calls.count("local") >= 2
        assert manager.enabled is True
    finally:
        release.set()
        await previous
        runner.close()
        manager.close()


def _wait_until(predicate, *, steps: int = 1000) -> None:
    for _ in range(steps):
        if predicate():
            return
        time.sleep(0.001)
    raise AssertionError("condition not met")


async def _overlap_switches(app: _App, *commands: str) -> None:
    """Run switch commands that overlap inside set_enabled, in submission order."""
    manager = app.runner.mcp
    original = manager.set_enabled
    entered, release = threading.Event(), threading.Event()
    seen = {"n": 0}

    def wrapped(enabled: bool):
        seen["n"] += 1
        if seen["n"] == 1:
            entered.set()
            assert release.wait(5)
        return original(enabled)

    manager.set_enabled = wrapped  # type: ignore[method-assign]
    try:
        tasks = [asyncio.create_task(handle_mcp_command(app, command)) for command in commands]
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(50):
            lock = getattr(app, "_mcp_switch_lock", None)
            if lock is not None and lock.locked():
                break
            await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*tasks)
    finally:
        release.set()
        manager.set_enabled = original  # type: ignore[method-assign]


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
