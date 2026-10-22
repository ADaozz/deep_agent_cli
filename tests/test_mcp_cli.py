"""Slash command /mcp shares MCPManager and the existing interaction controller."""
from __future__ import annotations

from pathlib import Path

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


class _App:
    def __init__(self, runner: AgentRunner) -> None:
        self.runner = runner
        self.state = CliState()
        self.interaction: InteractionController | None = None
        self._mcp_draft: dict[str, str] = {}
        self.status = ""

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


def test_list_test_tools_and_failed_add_message(tmp_path: Path, http_mcp: HttpFixture) -> None:
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
        handle_mcp_command(app, "")
        assert "local" in app.state.blocks[-1].content
        assert "connected" in app.state.blocks[-1].content
        handle_mcp_command(app, "tools local")
        assert "mcp__local__echo" in app.state.blocks[-1].content
        handle_mcp_command(app, "test local")
        assert "connected" in app.state.blocks[-1].content
        handle_mcp_command(app, "add")
        _advance(app, "down")
        _advance(app)
        _advance(app, "http://127.0.0.1:1/mcp")
        _advance(app)
        _advance(app, option=1)
        assert any(block.is_error and "not connected" in block.content for block in app.state.blocks)
    finally:
        runner.close()


def test_cancel_and_missing_remove_do_not_write(tmp_path: Path) -> None:
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
        handle_mcp_command(app, "add")
        finish_mcp_interaction(app, app.interaction, cancelled=True)
        assert app.interaction is None
        assert path.read_bytes() == before
        handle_mcp_command(app, "remove missing")
        assert path.read_bytes() == before
        assert any("not modified" in block.content for block in app.state.blocks)
    finally:
        runner.close()


def test_busy_run_keeps_the_current_graph(tmp_path: Path, http_mcp: HttpFixture) -> None:
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
        handle_mcp_command(app, "remove local")
        assert id(runner.prepared.graph) == original
        assert "mcp__local__echo" in graph_tool_names(runner.prepared.graph)
        assert "safe point" in app.status
        app.state.running = False
        from agent.cli.mcp_ui import _apply_when_idle

        _apply_when_idle(app)
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
