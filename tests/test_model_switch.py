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
from agent.runner import RunEvent
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


def test_switch_model_updates_deepagents_compaction_window() -> None:
    from deepagents.middleware.summarization import compute_summarization_defaults

    settings = Settings(
        llm_profiles=(
            ModelProfile(id="small", model="model-a", context_window=128_000),
            ModelProfile(id="large", model="model-b", context_window=1_000_000),
        ),
        llm_default="small",
    )
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), settings=settings,
    )
    for model_id, window in (("large", 1_000_000), ("small", 128_000)):
        runner.switch_model(model_id)
        assert runner.prepared.model.profile["max_input_tokens"] == window
        assert compute_summarization_defaults(runner.prepared.model)["trigger"] == ("fraction", 0.85)


def test_switch_model_keeps_in_memory_checkpoint() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="remembered")]),
        backend=StateBackend(), settings=_settings_two_models(),
    )
    assert runner.invoke("keep this").status == "completed"
    saver = runner.prepared.checkpointer
    runner.switch_model("beta")
    assert runner.prepared.checkpointer is saver
    state = runner.prepared.graph.get_state(runner._thread_config())
    assert [message.content for message in state.values["messages"]][-2:] == ["keep this", "remembered"]


def test_switch_model_rejected_while_busy() -> None:
    settings = _settings_two_models()
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        settings=settings,
        thread_id="busy-model",
    )
    with runner._operation_lock:
        with pytest.raises(RuntimeError, match="active operation"):
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
            app.state.apply(RunEvent(type="usage", result={"total_tokens": 50_000}))
            await app.select_model("beta")
            assert app.runner.current_model().id == "beta"
            assert app.state.usage == {}
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


def test_cli_model_picker_distinguishes_model_sources() -> None:
    settings = Settings(
        llm_profiles=(
            ModelProfile("local", "qwen3.6-flash", source="Local gateway"),
            ModelProfile("token-plan", "qwen3.6-flash", source="Token Plan"),
        ),
        llm_default="local",
    )
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(), settings=settings,
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await app.select_model()
            assert app.interaction is not None
            labels = [option["label"] for option in app.interaction.current["options"]]
            assert labels == [
                "qwen3.6-flash · Local gateway · current",
                "qwen3.6-flash · Token Plan",
            ]

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
