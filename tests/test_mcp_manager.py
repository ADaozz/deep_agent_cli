"""Server lifecycle: several servers, prefixes, failure isolation, process cleanup."""
from __future__ import annotations

import time
from pathlib import Path

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
