"""Multi-model switch on AgentRunner and CLI /model."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent.cli.app import CliApplication
from agent.config import ModelProfile, Settings
from agent.runner import AgentRunner
from tests.conftest import scripted_model


def _settings_two_models() -> Settings:
    return Settings(
        llm_profiles=(
            ModelProfile(id="alpha", model="model-a", api_key="ka", base_url="http://a/v1"),
            ModelProfile(id="beta", model="model-b", api_key="kb", base_url="http://b/v1"),
        ),
        llm_default="alpha",
    )


def test_switch_model_keeps_thread_and_rebuilds_graph() -> None:
    settings = _settings_two_models()
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        settings=settings,
        thread_id="model-thread",
    )
    assert runner.current_model() is not None
    assert runner.current_model().id == "alpha"
    graph_before = runner.prepared.graph
    thread = runner.thread_id
    profile = runner.switch_model("be")
    assert profile.id == "beta"
    assert runner.thread_id == thread
    assert runner.prepared.graph is not graph_before
    assert runner.current_model().model == "model-b"


def test_switch_model_rejected_while_busy() -> None:
    settings = _settings_two_models()
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        settings=settings,
        thread_id="busy-model",
    )
    runner._busy = True
    with pytest.raises(RuntimeError, match="in progress"):
        runner.switch_model("beta")


def test_cli_model_prefix_switch() -> None:
    settings = _settings_two_models()
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        settings=settings,
        thread_id="cli-model",
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await app.select_model("beta")
            assert app.runner.current_model().id == "beta"
            assert any("Switched model to model-b" in getattr(block, "content", "") for block in app.state.blocks)

    asyncio.run(scenario())


def test_cli_model_picker_displays_only_actual_model_names() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        settings=_settings_two_models(),
        thread_id="cli-model-labels",
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await app.select_model()
            assert app.interaction is not None
            labels = [option["label"] for option in app.interaction.current["options"]]
            assert labels == ["model-a · current", "model-b"]
            assert all("alpha" not in label and "beta" not in label for label in labels)

    asyncio.run(scenario())


def test_cli_model_blocked_while_running() -> None:
    settings = _settings_two_models()
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        settings=settings,
        thread_id="cli-model-run",
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app.state.running = True
            await app.select_model("beta")
            assert app.runner.current_model().id == "alpha"
            assert "Cancel the active run" in app.state.status

    asyncio.run(scenario())


def test_cli_ctrl_p_cycles_model() -> None:
    settings = _settings_two_models()
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        settings=settings,
        thread_id="cli-model-cycle",
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        assert app.runner.current_model().id == "alpha"
        app.cycle_model(delta=1)
        assert app.runner.current_model().id == "beta"
        app.cycle_model(delta=1)
        assert app.runner.current_model().id == "alpha"
        app.cycle_model(delta=-1)
        assert app.runner.current_model().id == "beta"
