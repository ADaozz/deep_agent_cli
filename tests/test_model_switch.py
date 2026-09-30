"""Multi-model switch on AgentRunner and CLI /model."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.document import Document
from prompt_toolkit.output import DummyOutput

from agent.cli.app import CliApplication
from agent.config import ModelProfile, Settings
from agent.runner import AgentRunner
from agent.runner import RunEvent
from agent.session import SessionStore
from tests.conftest import scripted_model


def _settings_two_models() -> Settings:
    return Settings(
        llm_profiles=(
            ModelProfile(id="alpha", model="model-a", api_key="ka", base_url="http://a/v1"),
            ModelProfile(id="beta", model="model-b", api_key="kb", base_url="http://b/v1"),
        ),
        llm_default="alpha",
    )


def _settings_grouped_models() -> Settings:
    return Settings.from_mapping({"llm": {
        "default": "token-plan/auto",
        "models": {
            "local": {"models": {"qwen-plus": {"model": "qwen3.5-plus"}}},
            "token-plan": {"provider": "openai-compatible", "models": {
                "auto": {"context_window": "1m"},
                "qwen3.8-max": {"context_window": "128k"},
            }},
        },
    }})


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


def test_deferred_model_switch_waits_for_old_tool_then_uses_new_model() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="", tool_calls=[{
            "id": "write-old", "name": "write_file",
            "args": {"file_path": "/workspace/old.txt", "content": "old"},
        }])]),
        backend=StateBackend(), settings=_settings_two_models(),
    )
    waiting = runner.invoke("write")
    assert waiting.status == "waiting_confirmation"
    runner.request_model_change("beta")
    assert runner.current_model().id == "alpha"
    with patch("agent.runner.build_chat_model", return_value=scripted_model([AIMessage(content="new model")])):
        events = []
        result = runner.approve_tool("write-old", on_event=events.append)
    assert result.status == "completed"
    assert result.output == "new model"
    assert runner.current_model().id == "beta"
    assert [event.type for event in events].index("tool_completed") < [event.type for event in events].index("runtime_config_applied")


def test_pending_model_survives_restart_and_applies_on_next_invoke(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    store = SessionStore(path)
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]), backend=StateBackend(),
        settings=_settings_two_models(), session_store=store,
    )
    thread_id = runner.thread_id
    runner.request_model_change("beta")
    assert store.get(thread_id).pending_model_id == "beta"
    runner.close()
    store.close()

    restored_store = SessionStore(path)
    restored = AgentRunner(
        model=scripted_model([AIMessage(content="old")]), backend=StateBackend(),
        settings=_settings_two_models(), session_store=restored_store, thread_id=thread_id,
    )
    assert restored.pending_model().id == "beta"
    with patch("agent.runner.build_chat_model", return_value=scripted_model([AIMessage(content="new")])):
        result = restored.invoke("hello")
    assert result.output == "new"
    assert restored.current_model().id == "beta"
    assert restored_store.get(thread_id).pending_model_id is None
    restored.close()
    restored_store.close()


def test_pending_model_is_scoped_to_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]), backend=StateBackend(),
        settings=_settings_two_models(), session_store=store,
    )
    first = runner.thread_id
    runner.request_model_change("beta")
    second = runner.new_session().id
    assert runner.pending_model() is None
    runner.switch_session(first)
    assert runner.pending_model().id == "beta"
    runner.switch_session(second)
    assert runner.pending_model() is None
    runner.close()
    store.close()


def test_pending_model_failure_keeps_current_and_reports_error() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="old")]), backend=StateBackend(),
        settings=_settings_two_models(),
    )
    runner.request_model_change("beta")
    events = []
    with patch("agent.runner.build_chat_model", side_effect=RuntimeError("model unavailable")):
        result = runner.invoke("hello", on_event=events.append)
    assert result.output == "old"
    assert runner.current_model().id == "alpha"
    assert runner.pending_model() is None
    assert any(event.type == "runtime_config_failed" and "model unavailable" in event.content for event in events)


def test_internal_config_boundary_can_resume_after_interrupted_apply() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="", tool_calls=[{
            "id": "write-old", "name": "write_file",
            "args": {"file_path": "/workspace/old.txt", "content": "old"},
        }])]), backend=StateBackend(), settings=_settings_two_models(),
    )
    assert runner.invoke("write").status == "waiting_confirmation"
    runner.request_model_change("beta")
    with patch.object(runner, "_apply_pending_runtime_config", side_effect=RuntimeError("interrupted")):
        with pytest.raises(RuntimeError, match="interrupted"):
            runner.approve_tool("write-old")
    assert runner.current_interrupt().kind.value == "runtime_config_boundary"
    with patch("agent.runner.build_chat_model", return_value=scripted_model([AIMessage(content="recovered")])):
        assert runner.resume_runtime_config().output == "recovered"
    assert runner.current_model().id == "beta"


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
            assert app.runner.current_model().id == "alpha"
            assert app.runner.pending_model().id == "beta"
            assert app.state.usage["total_tokens"] == 50_000
            assert any("Model switch queued" in getattr(block, "content", "") for block in app.state.blocks)
            app.show_status()
            assert any("model-a → model-b (pending)" in getattr(block, "content", "") for block in app.state.blocks)
            with patch("agent.runner.build_chat_model", return_value=scripted_model([AIMessage(content="new")])):
                assert runner.invoke("hello", on_event=app._apply_event).output == "new"
            assert app.runner.current_model().id == "beta"
            assert app.state.usage == {}
            assert any("Runtime config applied: model: model-b" in getattr(block, "content", "") for block in app.state.blocks)

    asyncio.run(scenario())


def test_model_argument_completion_lists_sources_and_model_ids() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), settings=_settings_grouped_models(),
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        complete = lambda value: list(app.slash_completer.get_completions(Document(value), None))
        assert [item.text for item in complete("/model token-")] == [
            "token-plan", "token-plan/auto", "token-plan/qwen3.8-max",
        ]
        selected = complete("/model token-plan/qwen")[0]
        assert selected.text == "token-plan/qwen3.8-max"
        assert selected.start_position == -len("token-plan/qwen")
        assert complete("/model token-plan/qwen extra") == []


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


def test_cli_grouped_model_picker_selects_source_then_model_and_goes_back() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(), settings=_settings_grouped_models(),
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await app.select_model()
            assert app.interaction is not None and app.interaction.kind == "model_source"
            assert [item["value"] for item in app.interaction.current["options"]] == ["local", "token-plan"]
            assert app.interaction.option_index == 1
            assert app.interaction.accept("") is True
            app._finish_interaction()
            assert app.interaction is not None and app.interaction.kind == "model"
            assert [item["label"] for item in app.interaction.current["options"]] == ["auto · current", "qwen3.8-max"]
            app._finish_interaction(cancelled=True)
            assert app.interaction is not None and app.interaction.kind == "model_source"
            assert app.interaction.accept("") is True
            app._finish_interaction()
            assert app.interaction is not None
            app.interaction.option_index = 1
            assert app.interaction.accept("") is True
            app._finish_interaction()
            assert app.interaction is None
            assert runner.current_model().id == "token-plan/auto"
            assert runner.pending_model().id == "token-plan/qwen3.8-max"
            assert runner.context_window() == 1_000_000

    asyncio.run(scenario())


def test_cli_model_source_arg_opens_group_and_full_id_switches() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(), settings=_settings_grouped_models(),
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await app.select_model("local")
            assert app.interaction is not None and app.interaction.kind == "model"
            assert [item["label"] for item in app.interaction.current["options"]] == ["qwen-plus"]
            assert app.interaction.current["options"][0]["description"] == "qwen3.5-plus"
            app._finish_interaction(cancelled=True)
            assert app.interaction is not None and app.interaction.kind == "model_source"
            app._finish_interaction(cancelled=True)
            assert app.interaction is None
            await app.select_model("local/qwen-plus")
            assert runner.current_model().id == "token-plan/auto"
            assert runner.pending_model().id == "local/qwen-plus"

    asyncio.run(scenario())


def test_cli_model_queued_while_running() -> None:
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
            assert app.runner.pending_model().id == "beta"
            assert "switch queued" in app.state.status

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
        assert app.runner.pending_model().id == "beta"
        app.cycle_model(delta=1)
        assert app.runner.pending_model() is None
        app.cycle_model(delta=-1)
        assert app.runner.pending_model().id == "beta"
