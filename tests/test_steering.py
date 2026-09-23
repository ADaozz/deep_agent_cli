"""Mid-turn steering and follow-up semantics."""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage
from deepagents.backends import StateBackend

from agent.control import RunController
from agent.middleware.steering import SteeringMiddleware
from agent.runner import AgentRunner
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


def test_steer_during_tools_lands_after_tool_messages() -> None:
    events: list[str] = []
    runner = AgentRunner(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "call-1", "name": "lookup_docs", "args": {"query": "x"},
            }]),
            AIMessage(content="after tools"),
        ]),
        backend=StateBackend(),
        thread_id="steer-tools",
    )

    def on_event(event) -> None:  # type: ignore[no-untyped-def]
        events.append(event.type)
        if event.type == "tool_started":
            runner.steer("mid course correction")

    result = runner.invoke("start", on_event=on_event)
    assert result.status == "completed"
    state = runner.prepared.graph.get_state(runner._thread_config())
    messages = state.values.get("messages", [])
    human_texts = [m.content for m in messages if isinstance(m, HumanMessage)]
    assert "start" in human_texts
    assert "mid course correction" in human_texts
    # Steering human must appear after the tool message that was in-flight.
    seen_tool = False
    steering_after_tool = False
    for message in messages:
        if type(message).__name__ == "ToolMessage":
            seen_tool = True
        if isinstance(message, HumanMessage) and message.content == "mid course correction":
            steering_after_tool = seen_tool
    assert steering_after_tool
    assert "steering_queued" in events
    assert "steering_applied" in events


def test_follow_up_not_consumed_as_steering() -> None:
    controller = RunController()
    controller.follow_up("later")
    assert controller.pop_steering() is None
    assert controller.pop_follow_up() == "later"


def test_defer_steering_during_interaction() -> None:
    controller = RunController()
    controller.steer("wait")
    controller.set_defer_steering(True)
    assert controller.pop_steering() is None
    controller.set_defer_steering(False)
    assert controller.pop_steering() == "wait"


def test_alt_up_takes_unapplied_only() -> None:
    controller = RunController()
    controller.steer("a")
    controller.follow_up("b")
    taken = controller.take_unapplied()
    assert [item.text for item in taken] == ["a", "b"]
    assert controller.pending_steering_count() == 0
    assert controller.pending_follow_up_count() == 0
