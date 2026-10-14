import asyncio

from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage
from prompt_toolkit.document import Document
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent.cli.app import CliApplication
from agent.cli.interactions import InteractionController
from agent.mcp.config import MCPConfig, MCPProfile
from agent.runner import AgentRunner
from tests.conftest import scripted_model


def test_menu_add_save_only_none_escape_and_completion(tmp_path):
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    runner.load_mcp(tmp_path / "mcp.yaml")
    try:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            async def scenario():
                await app.select_mcp()
                options = app.interaction.current["options"]
                assert [o["label"] for o in options] == ["none (current)", "+ Add MCP"]
                app._finish_interaction(cancelled=True)
                assert not (tmp_path / "mcp.yaml").exists()
                await app.select_mcp("add")
                draft = app.interaction
                assert not draft.accept("github")
                assert not draft.accept()
                assert draft.accept("https://example.com/mcp")
                await app._complete_mcp_interaction(draft)
                assert app.interaction.kind == "mcp_confirm"
                app.interaction.option_index = 1
                assert app.interaction.accept()
                await app._complete_mcp_interaction(app.interaction)
                assert runner.mcp_config.current == "none"
                assert MCPConfig.load(tmp_path / "mcp.yaml").profiles["github"].url == "https://example.com/mcp"
                await app.select_mcp()
                labels = [o["label"] for o in app.interaction.current["options"]]
                assert labels == ["none (current)", "github", "+ Add MCP"]
                assert sum("(current)" in label for label in labels) == 1
                app._finish_interaction(cancelled=True)
                await app.select_mcp("none")
                assert "github" in runner.mcp_config.profiles
                completions = list(app.slash_completer.get_completions(Document("/mcp g"), None))
                assert [c.text for c in completions] == ["github"]
                await app.select_mcp("add")
                invalid = app.interaction
                invalid.values = {"name": "none", "url": "https://example.com/mcp", "transport": "streamable_http"}
                await app._complete_mcp_interaction(invalid)
                assert app.interaction.kind == "mcp_add" and app.interaction.error
                assert app.interaction.values == invalid.values
            asyncio.run(scenario())
    finally:
        runner.close()


def test_failed_save_retains_draft_and_old_config(tmp_path, monkeypatch):
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    runner.load_mcp(tmp_path / "mcp.yaml")
    try:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            values = {"name": "remote", "url": "https://example.com/mcp", "transport": "streamable_http", "action": "save"}
            app._show_mcp_add(values)
            app.interaction.kind = "mcp_confirm"
            def fail(*args):
                raise OSError("disk failure")
            monkeypatch.setattr(MCPConfig, "save", fail)
            asyncio.run(app._complete_mcp_interaction(app.interaction))
            assert runner.mcp_config == MCPConfig()
            assert app.interaction.values == values
            assert app.interaction.error
    finally:
        runner.close()


def test_approval_provenance_and_secret_redaction():
    card = InteractionController.approval([{
        "name": "remote_tool", "toolCallId": "id", "args": {"token": "very-secret"},
        "mcp": {"profile": "github", "url": "https://example.com/mcp"},
    }])
    assert "MCP Profile: github" in card.question
    assert "https://example.com/mcp" in card.question
    assert "very-secret" not in card.question


def test_edit_preserves_transport_and_url_in_actual_editor():
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    try:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app._show_mcp_add({"name": "legacy", "transport": "sse", "url": "https://example.com/sse"})
            app._submit_buffer("follow_up")
            assert app.interaction.current["id"] == "transport"
            assert app.interaction.option_index == 1
            app._submit_buffer("follow_up")
            assert app.interaction.current["id"] == "url"
            assert app.buffer.text == "https://example.com/sse"
            assert not runner.prepared.graph.get_state(runner._thread_config()).values.get("messages")
    finally:
        runner.close()
