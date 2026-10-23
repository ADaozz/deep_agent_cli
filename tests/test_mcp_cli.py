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
    assert subcommands == {"enable", "disable", "add", "remove", "test", "tools"}


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


def test_approval_card_shows_host_config_without_guessing_credentials() -> None:
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
    assert "https://example.com/mcp?token=super-secret" in bearer.question
    assert "INTERNAL_MCP_TOKEN" in bearer.question
    remove = InteractionController.approval([{"name": "mcp_remove", "args": {"name": "secret-server"}}])
    assert "secret-server" in remove.question
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


def test_mcp_approval_preview_preserves_structure_and_original_arguments():
    from copy import deepcopy

    args = {"nonce": "TUI_E2E_APPROVE_002", "path": "/tmp/test.txt", "count": 7,
            "enabled": True, "items": [1, False, {"target": "repo"}], "empty": None}
    original = deepcopy(args)
    call = {"name": "mcp__tui_e2e__write_marker", "toolCallId": "original-id", "args": args}
    card = InteractionController.approval([call])
    assert "Execution: Host (outside sandbox)" in card.question
    assert "nonce: TUI_E2E_APPROVE_002" in card.question
    assert "path: /tmp/test.txt" in card.question
    assert "count: 7" in card.question and "enabled: true" in card.question
    from agent.cli.interactions import mcp_arguments_preview
    expanded = mcp_arguments_preview(args, expanded=True)
    assert "- false" in expanded and "target: repo" in expanded and "empty: null" in expanded
    assert args == original
    assert card.calls[0]["args"] is args
    assert card.tool_call_ids == ["original-id"]


def test_mcp_preview_preserves_unknown_credential_fields_and_authorization_text():
    from agent.cli.interactions import mcp_arguments_preview

    values = ["api_key", "apiKey", "token", "access_token", "Authorization", "password",
              "secret", "client_secret", "private_key", "cookie", "credential"]
    args = {"repo": "ADaozz/deep_agent_cli", "auth": {
        key: f"fake-secret-{index}" for index, key in enumerate(values)
    }, "headers": ["Authorization: Bearer fake-bearer-value"], "token_count": 9}
    preview = mcp_arguments_preview(args, expanded=True)
    assert "auth:\n    api_key: fake-secret-0" in preview
    assert all(f"fake-secret-{index}" in preview for index in range(len(values)))
    assert "Authorization: Bearer fake-bearer-value" in preview
    assert "repo: ADaozz/deep_agent_cli" in preview
    assert "token_count: 9" in preview


def test_multiple_mcp_approvals_keep_arguments_with_their_call():
    card = InteractionController.approval([
        {"name": "mcp__alpha__write", "toolCallId": "first", "args": {"nonce": "FIRST"}},
        {"name": "mcp__beta__remove", "toolCallId": "second", "args": {"path": "/tmp/SECOND"}},
    ])
    first, second = card.question.split("Tool: mcp__beta__remove")
    assert "nonce: FIRST" in first and "/tmp/SECOND" not in first
    assert "path: /tmp/SECOND" in second and "FIRST" not in second
    assert card.question.count("Execution: Host") == 2
    assert card.tool_call_ids == ["first", "second"]


def test_mcp_argument_previews_have_bounded_expansion_without_credential_guessing():
    from agent.cli.interactions import mcp_arguments_preview
    from agent.cli.state import ToolBlock
    from agent.cli.rendering import render_transcript

    args = {"payload": "A" * 100000, "auth": {"api_key": "fake-private-value"},
            "tail": "AFTER_PAYLOAD"}
    short = mcp_arguments_preview(args)
    full = mcp_arguments_preview(args, expanded=True)
    assert len(short) < 1000 and len(short.splitlines()) <= 9
    assert len(full) < 8500 and len(full.splitlines()) <= 81
    assert "omitted" in short and "omitted" in full
    assert len(full) > len(short)
    assert "AFTER_PAYLOAD" in full
    for expanded in (False, True):
        state = CliState(blocks=[ToolBlock("private", "mcp__local__write", args, status="waiting")],
                         tools_expanded=expanded)
        rendered = render_transcript(state, 80)
        assert "fake-private-value" in rendered
        assert "[redacted]" not in rendered
    assert args["auth"]["api_key"] == "fake-private-value"
    assert len(args["payload"]) == 100000


def test_mcp_preview_strips_terminal_controls_and_cannot_forge_keys():
    from agent.cli.interactions import mcp_arguments_preview

    args = {"nonce": "\x1b[31mvisible\x1b[0m\x1b]52;c;clipboard\x07\x00\u202e",
            "bad\npassword: forged": "plain\nline\tvalue",
            "authorization": "\x1b[32mBearer fake-ansi-token"}
    result = mcp_arguments_preview(args, expanded=True)
    assert "visible" in result
    assert "\x1b" not in result and "\x00" not in result and "\u202e" not in result
    assert "clipboard" not in result
    assert "Bearer fake-ansi-token" in result
    import json
    assert json.dumps(r"bad\npassword: forged", ensure_ascii=False) in result
    assert r"plain\nline\tvalue" in result


def test_mcp_preview_depth_cycles_and_large_objects_are_bounded():
    from agent.cli.interactions import mcp_arguments_preview

    args = {}
    args["self"] = args
    assert "omitted" in mcp_arguments_preview(args)
    large = {f"long-key-{i}" * 10: "x" * 1000 for i in range(10000)}
    result = mcp_arguments_preview(large)
    assert len(result) < 1000 and "omitted" in result
    nested = {"leaf": "NOT_AT_THIS_DEPTH"}
    for _ in range(20):
        nested = {"nested": nested}
    assert "NOT_AT_THIS_DEPTH" not in mcp_arguments_preview(nested, expanded=True)


def test_mcp_confirm_preserves_multiline_plain_text_and_other_panels_use_markdown():
    from rich.markdown import Markdown
    from rich.text import Text
    from agent.cli.mcp_ui import _ask_confirm
    from agent.cli.rendering import render_interaction
    from types import SimpleNamespace

    from agent.permission import PermissionMode

    app = SimpleNamespace(
        set_status=lambda *a, **kw: None,
        runner=SimpleNamespace(permission_mode=lambda: PermissionMode.ASK),
    )
    draft = {"name": "tui_e2e", "transport": "stdio",
             "target": "/path/to/python /tmp/mcp-e2e/server_with_[brackets].py"}
    _ask_confirm(app, draft)
    panel = app.interaction
    assert isinstance(list(panel.render().renderables)[1], Text)
    for width in (45, 100):
        rendered = render_interaction(panel, width)
        assert "Name: tui_e2e" in rendered and "Transport: stdio" in rendered
        assert rendered.index("Name:") < rendered.index("Transport:")
        assert "[brackets]" in rendered
    other = InteractionController.human({"question": "**Keep Markdown**",
                                        "fields": [{"id": "reply", "type": "text"}]})
    assert isinstance(list(other.render().renderables)[1], Markdown)
    panel.move(1)
    assert panel.option_index == 1
    assert panel.accept() and panel.values["confirm"] == "add"


@async_test
async def test_allow_add_wizard_saves_without_final_approval(tmp_path: Path, http_mcp: HttpFixture) -> None:
    from unittest.mock import patch
    from agent.factory import create_agent
    from agent.sandbox import BackendSelection, ExecutionMode

    manager = MCPManager(tmp_path / "mcp.json", call_timeout=8, connect_timeout=5)
    with patch("agent.factory.select_backend", return_value=BackendSelection(StateBackend(), ExecutionMode.SANDBOXED)):
        prepared = create_agent(model=scripted_model([AIMessage(content="done")]), interrupt_on={}, mcp=manager)
        runner = AgentRunner(prepared=prepared, mcp_manager=manager)
        app = _App(runner)
        try:
            await handle_mcp_command(app, "add")
            _advance(app, "local")
            _advance(app)  # Streamable HTTP
            _advance(app, http_mcp.url)
            _advance(app)  # No authentication; final save is automatic in allow.
            assert app.interaction is None
            await app._mcp_operation
            assert manager.get("local") is not None
            assert "mcp__local__echo" in runner.prepared.model_tool_names
            assert runner.prepared.interrupt_on == {}
        finally:
            runner.close()
