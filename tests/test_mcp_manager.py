"""Server lifecycle: several servers, prefixes, failure isolation, process cleanup."""
from __future__ import annotations

import time
from pathlib import Path
import pytest

from agent.mcp.manager import MCPManager, ServerStatus
from tests.mcp_fixture import HttpFixture, stdio_argv, text_of


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


def test_stdio_process_is_reaped_on_close(tmp_path: Path) -> None:
    import os

    executable, script = stdio_argv()
    parent = os.getpid()
    before = _children(parent)
    manager = MCPManager(tmp_path / "mcp.json", call_timeout=8, connect_timeout=8)
    spawned: set[int] = set()
    try:
        added = manager.add_from_fields(
            name="local", transport="stdio", command=executable, args=[script],
        )
        assert added.ok, added.message
        state = manager.get("local")
        assert state is not None and state.status is ServerStatus.CONNECTED
        echo = next(tool for tool in state.tools if tool.name == "mcp__local__echo")
        assert "stdio" in text_of(echo.invoke({"text": "stdio"}))
        spawned = _children(parent) - before
        assert spawned
    finally:
        manager.close()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and any(Path(f"/proc/{pid}").exists() for pid in spawned):
        time.sleep(0.1)
    assert not any(Path(f"/proc/{pid}").exists() for pid in spawned)


def test_two_servers_keep_separate_tools_and_one_failure_does_not_block(tmp_path: Path, http_mcp: HttpFixture) -> None:
    second = HttpFixture()
    manager = MCPManager(tmp_path / "mcp.json", call_timeout=8, connect_timeout=3)
    try:
        first = manager.add_from_fields(name="alpha", transport="streamable_http", url=http_mcp.url)
        other = manager.add_from_fields(name="beta", transport="streamable_http", url=second.url)
        broken = manager.add_from_fields(name="down", transport="streamable_http", url="http://127.0.0.1:1/mcp")
        assert first.ok and other.ok
        assert not broken.ok
        assert "not available" in broken.message
        alpha = manager.get("alpha")
        beta = manager.get("beta")
        down = manager.get("down")
        assert alpha is not None and beta is not None and down is not None
        assert alpha.status is ServerStatus.CONNECTED
        assert beta.status is ServerStatus.CONNECTED
        assert down.status is ServerStatus.ERROR
        alpha_echo = next(tool for tool in alpha.tools if tool.name == "mcp__alpha__echo")
        beta_echo = next(tool for tool in beta.tools if tool.name == "mcp__beta__echo")
        assert alpha_echo is not beta_echo
        assert "from-alpha" in text_of(alpha_echo.invoke({"text": "from-alpha"}))
        assert "from-beta" in text_of(beta_echo.invoke({"text": "from-beta"}))
        names = {tool.name for tool in manager.snapshot()[1]}
        assert "mcp__alpha__echo" in names and "mcp__beta__echo" in names
        assert not any(name.startswith("mcp__down__") for name in names)
    finally:
        manager.close()
        second.close()


def test_disable_removes_tools_on_the_next_snapshot(tmp_path: Path, http_mcp: HttpFixture) -> None:
    path = tmp_path / "mcp.json"
    manager = MCPManager(path, call_timeout=8, connect_timeout=5)
    try:
        added = manager.add_from_fields(name="local", transport="streamable_http", url=http_mcp.url)
        assert added.ok
        assert any(tool.name == "mcp__local__echo" for tool in manager.snapshot()[1])
        saved = path.read_text(encoding="utf-8").replace('"enabled": true', '"enabled": false', 1)
        path.write_text(saved, encoding="utf-8")
        manager.load()
        state = manager.get("local")
        assert state is not None and state.status is ServerStatus.DISABLED
        assert manager.snapshot()[1] == []
        assert manager.toolset_dirty()
    finally:
        manager.close()


def test_reload_restores_a_server_and_repeat_add_is_idempotent(tmp_path: Path, http_mcp: HttpFixture) -> None:
    path = tmp_path / "mcp.json"
    manager = MCPManager(path, call_timeout=8, connect_timeout=5)
    try:
        added = manager.add_from_fields(name="local", transport="streamable_http", url=http_mcp.url)
        assert added.ok and added.pending
        saved = path.read_bytes()
        again = manager.add_from_fields(name="local", transport="streamable_http", url=http_mcp.url)
        assert again.ok and not again.changed and not again.pending
        assert path.read_bytes() == saved
        manager.close()
        reloaded = MCPManager(path, call_timeout=8, connect_timeout=5)
        try:
            reloaded.load()
            assert reloaded.connect_pending().ok
            state = reloaded.get("local")
            assert state is not None and state.status is ServerStatus.CONNECTED
            echo = next(tool for tool in state.tools if tool.name == "mcp__local__echo")
            assert "again" in text_of(echo.invoke({"text": "again"}))
            removed = reloaded.remove("local")
            assert removed.changed
            gone = reloaded.remove("local")
            assert not gone.changed
            assert "local" not in path.read_text(encoding="utf-8")
        finally:
            reloaded.close()
    finally:
        if not manager._closed:
            manager.close()


def test_startup_reconnects_oauth_with_automatic_authorization_enabled(tmp_path, monkeypatch):
    import json
    from agent.mcp.adapter import LoadedServer

    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {
        "plain": {"transport": "streamable_http", "url": "http://example.com/mcp"},
        "oauth": {"transport": "streamable_http", "url": "http://example.com/mcp",
                  "auth": {"type": "oauth"}},
    }}))
    opened = []
    monkeypatch.setattr("agent.mcp.manager.open_server",
                        lambda config, *a, **kw: opened.append((config.name, kw["allow_oauth_browser"])) or LoadedServer(None, [], []))
    manager = MCPManager(path)
    try:
        manager.load()
        assert opened == []
        assert manager.get("oauth").status is ServerStatus.DISCONNECTED
        revision, tools = manager.snapshot()
        assert tools == []
        manager.discard(revision)
        assert manager.connect_pending().ok
        assert opened == [("plain", True), ("oauth", True)]
        manager.test("oauth")
        assert opened == [("plain", True), ("oauth", True), ("oauth", True)]
    finally:
        manager.close()


def test_connection_results_cannot_resurrect_removed_or_reloaded_server(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from agent.mcp.adapter import LoadedServer
    from agent.mcp.config import dumps_mcp_file, MCPFile, server_from_fields

    for action in ("remove", "reload", "close"):
        path = tmp_path / f"{action}.json"
        config = server_from_fields(name="local", transport="stdio", command="fixture")
        path.write_text(dumps_mcp_file(MCPFile(servers={"local": config})))
        started, release = threading.Event(), threading.Event()
        closed = []

        class Client:
            async def close(self):
                closed.append(self)

        client = Client()

        def connect(*args, **kwargs):
            started.set()
            assert release.wait(5)
            return LoadedServer(client, [], [])

        monkeypatch.setattr("agent.mcp.manager.open_server", connect)
        manager = MCPManager(path)
        manager.load()
        # Use a close observer because close() may already have stopped the bridge.
        monkeypatch.setattr(manager, "_close_clients", lambda clients: closed.extend(c for c in clients if c))
        try:
            with ThreadPoolExecutor() as pool:
                pending = pool.submit(manager.test, "local")
                assert started.wait(5)
                if action == "remove":
                    assert manager.remove("local").ok
                elif action == "reload":
                    path.write_text('{"servers": {}}')
                    manager.load()
                else:
                    manager.close()
                release.set()
                assert not pending.result(5).ok
            assert client in closed
            assert manager.get("local") is None or manager.get("local").client is None
            revision, tools = manager.snapshot()
            assert tools == []
            manager.discard(revision)
        finally:
            release.set()
            manager.close()


@pytest.mark.parametrize("operation", ["test", "add"])
def test_concurrent_connections_share_one_attempt(tmp_path, monkeypatch, operation):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from agent.mcp.adapter import LoadedServer
    from agent.mcp.config import dumps_mcp_file, MCPFile, server_from_fields

    config = server_from_fields(name="local", transport="stdio", command="fixture")
    path = tmp_path / "mcp.json"
    path.write_text(dumps_mcp_file(MCPFile(servers={"local": config})))
    first_started, release_first = threading.Event(), threading.Event()
    clients, closed = [], []

    class Client:
        async def close(self):
            closed.append(self)

    def connect(*args, **kwargs):
        client = Client()
        clients.append(client)
        if len(clients) == 1:
            first_started.set()
            assert release_first.wait(5)
        return LoadedServer(client, [], [])

    monkeypatch.setattr("agent.mcp.manager.open_server", connect)
    manager = MCPManager(path)
    manager.load()
    try:
        with ThreadPoolExecutor() as pool:
            first = pool.submit(manager.connect_pending)
            assert first_started.wait(5)
            second = pool.submit(manager.test, "local") if operation == "test" else pool.submit(manager.add, config)
            time.sleep(.05)
            assert not second.done()
            release_first.set()
            assert first.result(5).ok
            assert second.result(5).ok
        assert len(clients) == 1
        assert closed == []
        assert manager._states["local"].client is clients[0]
    finally:
        release_first.set()
        manager.close()
    assert closed.count(clients[0]) == 1


def test_reload_and_remove_serialize_disk_catalog(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import agent.mcp.manager as module

    manager = MCPManager(tmp_path / "mcp.json")
    from agent.mcp.config import server_from_fields
    config = server_from_fields(
        name="local", transport="stdio", command="fixture", enabled=False,
    )
    assert manager.add(config).ok
    original = module.load_mcp_file
    reading, release = threading.Event(), threading.Event()

    def read(path):
        document = original(path)
        reading.set()
        assert release.wait(5)
        return document

    monkeypatch.setattr(module, "load_mcp_file", read)
    try:
        with ThreadPoolExecutor() as pool:
            reload = pool.submit(manager.load)
            assert reading.wait(5)
            removal = pool.submit(manager.remove, "local")
            release.set()
            reload.result(5)
            assert removal.result(5).ok
        assert manager.get("local") is None
        assert "local" not in manager.path.read_text()
    finally:
        release.set()
        manager.close()


def test_snapshot_pins_exact_clients_and_stale_commit_is_rejected(tmp_path, http_mcp):
    import pytest

    manager = MCPManager(tmp_path / "mcp.json", connect_timeout=5)
    try:
        assert manager.add_from_fields(name="local", transport="streamable_http", url=http_mcp.url).ok
        revision, tools = manager.snapshot()
        echo = next(tool for tool in tools if tool.name == "mcp__local__echo")
        assert manager.remove("local").ok
        # The graph builder's client remains valid even before publication.
        assert "pinned" in text_of(echo.invoke({"text": "pinned"}))
        with pytest.raises(RuntimeError, match="registry changed"):
            manager.commit(revision)
        assert manager.toolset_dirty()
        manager.discard(revision)
        latest, tools = manager.snapshot()
        assert tools == []
        manager.commit(latest)
        assert not manager.toolset_dirty()
        assert not manager._retired
    finally:
        manager.close()


def test_close_during_real_stdio_handshake_reaps_child(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import os
    import sys

    before = _children(os.getpid())
    manager = MCPManager(tmp_path / "mcp.json", connect_timeout=8)
    spawned = set()
    try:
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(
                manager.add_from_fields, name="slow", transport="stdio",
                command=sys.executable, args=["-c", "import time; time.sleep(30)"],
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                spawned = _children(os.getpid()) - before
                if spawned:
                    break
                time.sleep(.02)
            assert spawned
            manager.close()
            assert not pending.result(5).ok
        assert not any(Path(f"/proc/{pid}").exists() for pid in spawned)
        assert manager.get("slow") is None
        assert not manager._bridge._thread.is_alive()
    finally:
        manager.close()


def test_remote_aliases_route_independently_and_survive_reconnect_and_restart(tmp_path):
    from agent.mcp.names import external_tool_name
    from tests.mcp_fixture import COMPAT_TOOL_NAMES, build_compat_server

    fixture = HttpFixture(server_factory=build_compat_server)
    path = tmp_path / "mcp.json"
    manager = MCPManager(path, connect_timeout=5)
    try:
        for server in ("alpha", "beta"):
            assert manager.add_from_fields(name=server, transport="streamable_http", url=fixture.url).ok
        expected = {
            external_tool_name(server, remote)
            for server in ("alpha", "beta") for remote in COMPAT_TOOL_NAMES
        }
        assert len(expected) == 2 * len(COMPAT_TOOL_NAMES)

        def check():
            found = set()
            for server in ("alpha", "beta"):
                state = manager.get(server)
                assert not state.warnings
                for tool in state.tools:
                    found.add(tool.name)
                    raw = tool.metadata["deep_agent_mcp"]["remote_name"]
                    assert raw in COMPAT_TOOL_NAMES
                    assert tool.name == external_tool_name(server, raw)
                    assert f"{raw}:routed" in text_of(tool.invoke({"value": "routed"}))
            assert found == expected

        check()
        assert manager.test("alpha").ok
        check()
        manager.close()
        manager = MCPManager(path, connect_timeout=5)
        manager.load()
        assert manager.connect_pending().ok
        check()
    finally:
        manager.close()
        fixture.close()


def test_unmappable_and_duplicate_tools_are_reported_to_user(tmp_path, monkeypatch):
    from langchain_core.tools import StructuredTool
    from agent.cli.mcp_ui import _show_tools
    from agent.cli.state import CliState
    from types import SimpleNamespace

    async def call(value: str) -> str:
        return value

    tools = [StructuredTool.from_function(
        coroutine=call, name="placeholder", description="test",
    ).model_copy(update={"name": name}) for name in ("", "duplicate", "duplicate")]

    class Client:
        async def __aenter__(self):
            return self
        async def close(self):
            pass

    class Adapter:
        def __init__(self, target):
            self.client = Client()
        async def list_tools(self, **kwargs):
            return tools

    monkeypatch.setattr("agent.mcp.adapter.MCPAdapter", Adapter)
    manager = MCPManager(tmp_path / "mcp.json")
    try:
        result = manager.add_from_fields(name="local", transport="stdio", command="fixture")
        assert result.ok
        assert "non-empty" in result.message and "conflicts" in result.message
        assert "non-empty" in manager.test("local").message
        state = manager.get("local")
        assert len(state.tools) == 1
        assert len(state.warnings) == 2
        assert "non-empty" in manager.describe()
        assert "conflicts" in manager.describe()
        app = SimpleNamespace(state=CliState())
        _show_tools(app, manager, "local")
        assert "non-empty" in app.state.blocks[-1].content
        assert "conflicts" in app.state.blocks[-1].content
    finally:
        manager.close()
