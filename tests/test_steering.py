"""Mid-turn steering and follow-up semantics."""
from __future__ import annotations

from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from deepagents.backends import StateBackend
import pytest
from unittest.mock import patch

from agent.control import RunController
from agent.middleware.steering import SteeringMiddleware
from agent.runner import AgentRunner
from agent.factory import create_agent
from agent.tools.examples import build_example_tools
from tests.conftest import scripted_model


def test_steering_middleware_consumes_one_message_per_boundary() -> None:
    controller = RunController()
    middleware = SteeringMiddleware(controller)
    controller.steer("first")
    controller.steer("second")
    update = middleware.before_model({"messages": []}, None)
    assert update is not None
    assert isinstance(update["messages"][0], HumanMessage)
    assert update["messages"][0].content == "first"
    assert controller.pending_steering_count() == 1
    update2 = middleware.after_agent({"messages": []}, None)
    assert update2 is not None
    assert update2["jump_to"] == "model"
    assert update2["messages"][0].content == "second"
    assert controller.pending_steering_count() == 0


def test_steering_before_tools_skips_proposed_tools_before_replanning() -> None:
    controller = RunController()
    middleware = SteeringMiddleware(controller)
    controller.steer("change direction")
    response = middleware.wrap_model_call(None, lambda _: ModelResponse(result=[AIMessage(
        content="", tool_calls=[
            {"id": "a", "name": "write_file", "args": {}},
            {"id": "b", "name": "execute", "args": {}},
        ],
    )]))
    assert response.model_response.result[0].tool_calls == []
    assert response.command.update["jump_to"] == "model"
    assert response.command.update["messages"][0].content == "change direction"


def test_steer_during_model_skips_tool_and_reaches_next_model() -> None:
    events: list[str] = []
    model = scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "call-1", "name": "write_file",
                "args": {"file_path": "/workspace/should-not-exist.txt", "content": "stale"},
            }]),
            AIMessage(content="after correction"),
        ])
    prepared = create_agent(model=model, backend=StateBackend())
    runner = AgentRunner(
        prepared=prepared,
        thread_id="steer-tools",
    )

    def on_event(event) -> None:  # type: ignore[no-untyped-def]
        events.append(event.type)

    original_generate = type(model)._generate
    generated = 0

    def generate(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal generated
        result = original_generate(self, *args, **kwargs)
        generated += 1
        if generated == 1:
            runner.steer("mid course correction")
        return result

    with patch.object(type(model), "_generate", generate):
        result = runner.invoke("start", on_event=on_event)
    assert result.status == "completed"
    state = runner.prepared.graph.get_state(runner._thread_config())
    messages = state.values.get("messages", [])
    assert "/workspace/should-not-exist.txt" not in state.values.get("files", {})
    human_texts = [m.content for m in messages if isinstance(m, HumanMessage)]
    assert "start" in human_texts
    assert "mid course correction" in human_texts
    assert not [m for m in messages if isinstance(m, ToolMessage)]
    assert not [call for m in messages if isinstance(m, AIMessage) for call in m.tool_calls]
    assert "steering_queued" in events
    assert "steering_applied" in events


def test_steer_during_started_tool_waits_for_its_result() -> None:
    prepared = create_agent(model=scripted_model([
        AIMessage(content="", tool_calls=[{
            "id": "call-1", "name": "lookup_docs", "args": {"query": "x"},
        }]),
        AIMessage(content="after tools"),
    ]), backend=StateBackend(), extra_tools=build_example_tools())
    runner = AgentRunner(prepared=prepared, thread_id="steer-active-tool")

    def on_event(event) -> None:  # type: ignore[no-untyped-def]
        if event.type == "tool_started":
            runner.steer("mid course correction")

    result = runner.invoke("start", on_event=on_event)
    assert result.status == "completed"
    messages = runner.prepared.graph.get_state(runner._thread_config()).values["messages"]
    tool_index = next(i for i, item in enumerate(messages) if isinstance(item, ToolMessage))
    steering_index = next(
        i for i, item in enumerate(messages)
        if isinstance(item, HumanMessage) and item.content == "mid course correction"
    )
    assert tool_index < steering_index


def test_follow_up_not_consumed_as_steering() -> None:
    controller = RunController()
    controller.follow_up("later")
    assert controller.pop_steering() is None
    assert controller.pop_follow_up() == "later"


def test_runner_consumes_follow_up_without_tui() -> None:
    runner = AgentRunner(model=scripted_model([
        AIMessage(content="first reply"), AIMessage(content="second reply"),
    ]), backend=StateBackend())
    runner.follow_up("second task")
    events = []
    result = runner.invoke("first task", on_event=events.append)
    assert result.status == "completed"
    assert result.output == "second reply"
    assert runner.control.pending_follow_up_count() == 0
    messages = runner.prepared.graph.get_state(runner._thread_config()).values["messages"]
    assert [message.content for message in messages if isinstance(message, HumanMessage)] == [
        "first task", "second task",
    ]
    assert [event.content for event in events if event.type == "turn_completed"] == [
        "first reply", "second reply",
    ]


def test_defer_steering_during_interaction() -> None:
    controller = RunController()
    controller.steer("wait")
    controller.set_defer_steering(True)
    assert controller.pop_steering() is None
    controller.set_defer_steering(False)
    assert controller.pop_steering() == "wait"


def test_alt_up_takes_unapplied_only() -> None:
    controller = RunController()
    controller.follow_up("b")
    controller.steer("a")
    taken = controller.take_unapplied()
    assert [item.text for item in taken] == ["b", "a"]
    assert controller.pending_steering_count() == 0
    assert controller.pending_follow_up_count() == 0


def test_new_session_requires_reclaiming_unapplied_input() -> None:
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    old_thread = runner.thread_id
    runner.follow_up("old instruction")
    with pytest.raises(RuntimeError, match="Unapplied input"):
        runner.new_session()
    assert runner.thread_id == old_thread
    assert runner.take_unapplied_messages() == ["old instruction"]
    runner.new_session()
    assert runner.thread_id != old_thread
    assert runner.control.pending_follow_up_count() == 0
