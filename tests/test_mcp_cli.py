"""Slash command /mcp shares MCPManager and the existing interaction controller."""
from __future__ import annotations

from pathlib import Path
import asyncio
from types import SimpleNamespace
from functools import partial, wraps

from langchain_core.messages import AIMessage
from deepagents.backends import StateBackend
from prompt_toolkit.document import Document

from agent.cli.app import SlashCompleter
from agent.cli.commands import command_table
from agent.cli.interactions import InteractionController
from agent.cli.mcp_ui import finish_mcp_interaction, handle_mcp_command
from agent.cli.state import CliState
from agent.mcp.manager import MCPManager
from agent.runner import AgentRunner
from tests.conftest import graph_tool_names, scripted_model
from tests.mcp_fixture import HttpFixture


def async_test(func):
    @wraps(func)
    def run(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))
    return run


class _App:
    def __init__(self, runner: AgentRunner) -> None:
        self.runner = runner
        self.state = CliState()
        self.interaction: InteractionController | None = None
        self._mcp_draft: dict[str, str] = {}
        self.status = ""
        self.application = SimpleNamespace(invalidate=lambda: None)

    async def _run_blocking(self, func, /, *args, **kwargs):
        return await asyncio.to_thread(partial(func, *args, **kwargs))

    def _apply_event(self, _event: object, announce: bool = False) -> None:
        return None

    def set_status(self, text: str, transient: bool = True) -> None:
        self.status = text


def _advance(app: _App, text: str = "", *, option: int | None = None) -> None:
    interaction = app.interaction
    assert interaction is not None
    if option is not None:
        interaction.option_index = option
    assert interaction.accept(text)
    finish_mcp_interaction(app, interaction, cancelled=False)


def test_existing_commands_stay_registered() -> None:
    names = {command.name for command in command_table()}
    assert {"model", "permission", "sandbox", "mcp"} <= names
    completer = SlashCompleter(command_table())
    listed = list(completer.get_completions(Document("/"), None))
    assert len(listed) == len(command_table())
    subcommands = {item.text for item in completer.get_completions(Document("/mcp "), None)}
    assert subcommands == {"add", "remove", "test", "tools"}


@async_test
async def test_list_test_tools_and_failed_add_message(tmp_path: Path, http_mcp: HttpFixture) -> None:
    manager = MCPManager(tmp_path / "mcp.json", call_timeout=8, connect_timeout=3)
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="done")]),
        backend=StateBackend(),
        mcp_manager=manager,
    )
    app = _App(runner)
    try:
        added = manager.add_from_fields(name="local", transport="streamable_http", url=http_mcp.url)
        assert added.ok, added.message
        await handle_mcp_command(app, "")
        assert "local" in app.state.blocks[-1].content
        assert "connected" in app.state.blocks[-1].content
        await handle_mcp_command(app, "tools local")
        assert "mcp__local__echo" in app.state.blocks[-1].content
        await handle_mcp_command(app, "test local")
        assert "connected" in app.state.blocks[-1].content
        await handle_mcp_command(app, "add")
        _advance(app, "down")
        _advance(app)
        _advance(app, "http://127.0.0.1:1/mcp")
        _advance(app)
        _advance(app, option=1)
        await app._mcp_operation
        assert any(block.is_error and "not connected" in block.content for block in app.state.blocks)
    finally:
        runner.close()


@async_test
async def test_cancel_and_missing_remove_do_not_write(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text('{"servers": {}}\n', encoding="utf-8")
    before = path.read_bytes()
    manager = MCPManager(path)
    manager.load()
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="done")]),
        backend=StateBackend(),
        mcp_manager=manager,
    )
    app = _App(runner)
    try:
        await handle_mcp_command(app, "add")
        finish_mcp_interaction(app, app.interaction, cancelled=True)
        assert app.interaction is None
        assert path.read_bytes() == before
        await handle_mcp_command(app, "remove missing")
        assert path.read_bytes() == before
        assert any("not modified" in block.content for block in app.state.blocks)
    finally:
        runner.close()


@async_test
async def test_busy_run_keeps_the_current_graph(tmp_path: Path, http_mcp: HttpFixture) -> None:
    manager = MCPManager(tmp_path / "mcp.json", call_timeout=8, connect_timeout=5)
    assert manager.add_from_fields(name="local", transport="streamable_http", url=http_mcp.url).ok
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="done")]),
        backend=StateBackend(),
        mcp_manager=manager,
    )
    app = _App(runner)
    try:
        app.state.running = True
        original = id(runner.prepared.graph)
        await handle_mcp_command(app, "remove local")
        assert id(runner.prepared.graph) == original
        assert "mcp__local__echo" in graph_tool_names(runner.prepared.graph)
        assert "safe point" in app.status
        app.state.running = False
        from agent.cli.mcp_ui import _apply_when_idle

        await _apply_when_idle(app)
        assert "mcp__local__echo" not in graph_tool_names(runner.prepared.graph)
    finally:
        runner.close()


def test_approval_card_shows_the_host_command_and_hides_tokens() -> None:
    stdio = InteractionController.approval([{
        "name": "mcp_add",
        "toolCallId": "call-1",
        "args": {
            "transport": "stdio",
            "command": "python",
            "args": ["server.py", "--flag"],
            "auth_type": "none",
        },
    }])
    assert "python server.py --flag" in stdio.question
    assert "outside the sandbox" in stdio.question
    bearer = InteractionController.approval([{
        "name": "mcp_add",
        "toolCallId": "call-2",
        "args": {
            "transport": "streamable_http",
            "url": "https://example.com/mcp?token=super-secret",
            "auth_type": "bearer",
            "token_env": "INTERNAL_MCP_TOKEN",
        },
    }])
    assert "super-secret" not in bearer.question
    assert "INTERNAL_MCP_TOKEN" in bearer.question
    remove = InteractionController.approval([{"name": "mcp_remove", "args": {"name": "local"}}])
    assert "local" in remove.question
    assert "unchanged" in remove.question


@async_test
async def test_real_prompt_toolkit_loop_remains_responsive_and_busy_until_worker_finishes(tmp_path, monkeypatch):
    import threading
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from agent.cli.app import CliApplication
    from agent.mcp.manager import MCPResult

    started, release = threading.Event(), threading.Event()
    calls = []

    def connect(name):
        calls.append(name)
        started.set()
        assert release.wait(5)
        return MCPResult(False, False, "Connection timed out; retry available")

    manager = MCPManager(tmp_path / "mcp.json")
    monkeypatch.setattr(manager, "test", connect)
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]),
                         backend=StateBackend(), mcp_manager=manager)
    with create_pipe_input() as pipe:
        app = CliApplication(runner, config_dir=tmp_path / "config", input=pipe, output=DummyOutput())
        task = asyncio.create_task(app.run_async())
        try:
            for _ in range(100):
                if app.application.is_running and not getattr(app, "_mcp_busy", False):
                    break
                await asyncio.sleep(.01)
            pipe.send_text("/mcp test slow\r")
            assert await asyncio.to_thread(started.wait, 2)
            # Real input is consumed, and a second operation is refused.
            pipe.send_text("/mcp test slow\r")
            await asyncio.sleep(.05)
            pipe.send_text("still typing")
            for _ in range(100):
                if app.buffer.text == "still typing":
                    break
                await asyncio.sleep(.01)
            assert app.buffer.text == "still typing"
            assert calls == ["slow"]
            pipe.send_text("\x1b")
            await asyncio.sleep(.6)  # prompt-toolkit's escape sequence timeout
            assert app._mcp_busy
            assert "still running" in app.state.status
            # Cancelling the waiter must not clear busy while its thread runs.
            from agent.cli.mcp_ui import _wait_operation
            waiter = asyncio.create_task(_wait_operation(app, app._mcp_operation))
            await asyncio.sleep(0)
            waiter.cancel()
            await waiter
            assert app._mcp_busy
            assert any("still running" in b.content for b in app.state.blocks)
            release.set()
            await asyncio.wait_for(app._mcp_operation, 3)
            assert not app._mcp_busy
            await handle_mcp_command(app, "test slow")
            assert calls == ["slow", "slow"]
            assert not app._mcp_busy
        finally:
            release.set()
            if hasattr(app, "_mcp_operation"):
                await asyncio.wait_for(app._mcp_operation, 3)
            if app.application.is_running:
                app.application.exit()
            await task
            runner.close()
