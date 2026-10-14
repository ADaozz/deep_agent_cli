"""Model-stream cancellation over real HTTP SSE (no external API, no transport mocks).

Each test drives the real ``build_chat_model`` client, the real OpenAI SDK and
a real socket against ``ScriptedSSEServer``. A cancellation test only passes
when the Runner returned, the server saw the client close the connection, no
bridge task is alive and the checkpoint stops changing.
"""
from __future__ import annotations

import gc
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.config import ModelProfile, Settings
from agent.runner import AgentRunner, RunEvent, RunResult
from agent.session import SessionStore, StopReason
from tests.model_stream_server import ScriptedSSEServer, Turn

pytestmark = pytest.mark.integration

APIS = ["chat_completions", "responses"]
CANCEL_BUDGET = 5.0


def _live_bridges() -> int | None:
    try:
        from agent.llm_stream import live_stream_count
    except ImportError:
        return None
    return live_stream_count()


def settings_for(server: ScriptedSSEServer) -> Settings:
    return Settings(llm_profiles=(ModelProfile(
        "local/stream", "stream-model", api_key="local-test-key", base_url=server.base_url, api=server.api,
    ),), llm_default="local/stream")


class Harness:
    """Run one Runner operation on a worker thread and timestamp its events."""

    def __init__(self, runner: AgentRunner) -> None:
        self.runner = runner
        self.events: list[tuple[float, RunEvent]] = []
        self.result: RunResult | None = None
        self.error: BaseException | None = None
        self.finished_at: float | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def on_event(self, event: RunEvent) -> None:
        with self._lock:
            self.events.append((time.monotonic(), event))

    def start(self, call: Callable[..., RunResult], *args: Any) -> "Harness":
        def work() -> None:
            try:
                self.result = call(*args, on_event=self.on_event)
            except BaseException as exc:  # noqa: BLE001
                self.error = exc
            finally:
                self.finished_at = time.monotonic()
        self._thread = threading.Thread(target=work, name="runner-op", daemon=True)
        self._thread.start()
        return self

    def join(self, timeout: float) -> bool:
        assert self._thread is not None
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def types(self, since: float = 0.0) -> list[str]:
        with self._lock:
            return [event.type for stamp, event in self.events if stamp >= since]

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        raise AssertionError(f"condition not reached; events={self.types()}")


@pytest.fixture
def make_runner(tmp_path: Path):
    created: list[tuple[AgentRunner, SessionStore]] = []

    def build(server: ScriptedSSEServer, thread_id: str = "stream-cancel") -> AgentRunner:
        store = SessionStore(tmp_path / "state.sqlite3")
        runner = AgentRunner(settings=settings_for(server), backend=StateBackend(),
                             session_store=store, thread_id=thread_id)
        created.append((runner, store))
        return runner

    yield build
    for runner, store in created:
        try:
            runner.close()
        finally:
            store.close()


def _checkpoint_count(runner: AgentRunner) -> int:
    return len(list(runner._checkpointer.list({"configurable": {"thread_id": runner.thread_id}})))


def _cancel_and_measure(harness: Harness, server: ScriptedSSEServer, connection_index: int) -> dict[str, Any]:
    connection = server.connections[connection_index]
    requested = time.monotonic()
    harness.runner.request_cancel()
    finished = harness.join(CANCEL_BUDGET)
    elapsed = time.monotonic() - requested
    closed = connection.closed.wait(max(0.0, CANCEL_BUDGET - elapsed))
    report = {
        "runner_finished": finished,
        "cancel_to_runner_end_s": round((harness.finished_at or time.monotonic()) - requested, 3),
        "server_saw_close": closed and connection.closed_by_client_at is not None,
        "cancel_to_server_close_s": (
            round(connection.closed_by_client_at - requested, 3)
            if connection.closed_by_client_at is not None else None
        ),
        "live_bridges": _live_bridges(),
        "threads": sorted(thread.name for thread in threading.enumerate()),
        "result": harness.result.status if harness.result else None,
        "error": repr(harness.error) if harness.error else None,
    }
    print("cancel-report", json.dumps(report, ensure_ascii=False))
    return report


def _assert_clean_cancel(harness: Harness, server: ScriptedSSEServer, report: dict[str, Any]) -> None:
    assert report["runner_finished"], f"Runner still blocked {CANCEL_BUDGET}s after cancel: {report}"
    assert harness.error is None, report
    assert harness.result is not None and harness.result.status == "cancelled", report
    assert report["server_saw_close"], f"model HTTP connection still open: {report}"
    assert report["live_bridges"] == 0, report
    assert server.open_connections() == []
    types = harness.types()
    assert types.count("run_cancelled") == 1, types
    assert "run_failed" not in types and "run_completed" not in types, types


def _assert_session_safe(runner: AgentRunner, before: int) -> None:
    time.sleep(0.3)
    assert _checkpoint_count(runner) == before, "checkpoint changed after the run ended"
    info = runner.session_store.get(runner.thread_id)
    assert info is not None and info.last_run_status is StopReason.ABORTED


# ---------------------------------------------------------------------------
# C01 / C02 / C04 / C14 / C15: silent first chunk, silent mid-stream, continuous
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("scenario", ["before_first_chunk", "silent_mid_stream", "continuous"])
def test_cancel_stops_model_stream(api: str, scenario: str, make_runner) -> None:
    turn = {
        "before_first_chunk": Turn(mode="hang"),
        "silent_mid_stream": Turn(text="partial answer then silence", mode="hang", chunks_before_hang=2,
                                  reasoning="thinking"),
        "continuous": Turn(mode="forever", interval=0.01),
    }[scenario]
    with ScriptedSSEServer(api, [turn, Turn(text="fresh answer")]) as server:
        runner = make_runner(server)
        harness = Harness(runner).start(runner.invoke, "start")
        connection = server.wait_for_connection(0)
        if scenario == "before_first_chunk":
            connection.hanging.wait(5)
        elif scenario == "silent_mid_stream":
            connection.hanging.wait(5)
            harness.wait_for(lambda: "assistant_delta" in harness.types())
        else:
            harness.wait_for(lambda: harness.types().count("assistant_delta") >= 5)
        report = _cancel_and_measure(harness, server, 0)
        _assert_clean_cancel(harness, server, report)
        checkpoints = _checkpoint_count(runner)
        after_return = len(harness.events)
        sent = connection.chunks_sent
        time.sleep(0.3)
        assert len(harness.events) == after_return, "events kept arriving after the run returned"
        assert connection.chunks_sent == sent
        _assert_session_safe(runner, checkpoints)
        state = runner.prepared.graph.get_state(runner._thread_config())
        messages = state.values.get("messages", [])
        # The interrupted response is never committed as a finished AIMessage.
        assert not any(isinstance(item, AIMessage) for item in messages), messages

        # C07 / C10: the same runner and session accept the next task.
        follow = Harness(runner).start(runner.invoke, "next")
        assert follow.join(10), follow.types()
        assert follow.error is None and follow.result.status == "completed", (follow.error, follow.types())
        assert follow.result.output == "fresh answer"
        assert "run_cancelled" not in follow.types()
        recovery = json.dumps(server.connections[1].body)
        assert "aborted by the user" in recovery


# ---------------------------------------------------------------------------
# C03: a slow but healthy reasoning stream is never killed by a chunk timeout.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("api", APIS)
def test_long_silent_reasoning_gap_is_not_cancelled(api: str, make_runner, monkeypatch) -> None:
    # LangChain's async path applies this per-chunk timeout; the sync path never did.
    monkeypatch.setenv("LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S", "0.5")
    with ScriptedSSEServer(api, [Turn(reasoning="long think", gap_before_text=1.5, text="done thinking")]) as server:
        runner = make_runner(server)
        harness = Harness(runner).start(runner.invoke, "think hard")
        assert harness.join(15), harness.types()
        assert harness.error is None, harness.error
        assert harness.result.status == "completed" and harness.result.output == "done thinking"
        assert _live_bridges() == 0


# ---------------------------------------------------------------------------
# C05: repeated / concurrent cancellation is idempotent.
# ---------------------------------------------------------------------------
def test_repeated_cancel_is_idempotent(make_runner) -> None:
    with ScriptedSSEServer("chat_completions", [Turn(mode="hang")]) as server:
        runner = make_runner(server)
        harness = Harness(runner).start(runner.invoke, "start")
        server.wait_for_connection(0).hanging.wait(5)
        threads = [threading.Thread(target=runner.request_cancel) for _ in range(10)]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        assert not any(thread.is_alive() for thread in threads), "request_cancel blocked"
        assert harness.join(CANCEL_BUDGET)
        assert time.monotonic() - started < CANCEL_BUDGET
        assert harness.result.status == "cancelled"
        assert harness.types().count("run_cancelled") == 1
        assert server.connections[0].closed.wait(2)
        for _ in range(3):
            runner.request_cancel()
        assert _live_bridges() == 0


# ---------------------------------------------------------------------------
# C06: cancellation racing natural completion yields exactly one terminal state.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("api", APIS)
def test_cancel_racing_completion_has_single_outcome(api: str, make_runner) -> None:
    rounds = 16
    with ScriptedSSEServer(api, [Turn(text="a b c d e f", interval=0.004) for _ in range(rounds)]) as server:
        runner = make_runner(server)
        outcomes = []
        for index in range(rounds):
            harness = Harness(runner).start(runner.invoke, f"round {index}")
            server.wait_for_connection(index)
            # Sweep the cancel from mid-stream to after the graph finished.
            time.sleep(0.008 * index)
            runner.request_cancel()
            assert harness.join(CANCEL_BUDGET), harness.types()
            assert harness.error is None, harness.error
            types = harness.types()
            terminal = [item for item in types if item in {"run_completed", "run_cancelled", "run_failed"}]
            assert len(terminal) == 1, types
            assert harness.result.status in {"completed", "cancelled"}
            assert terminal[0] == f"run_{harness.result.status}"
            assert server.connections[index].closed.wait(CANCEL_BUDGET)
            outcomes.append(harness.result.status)
            if harness.result.status == "completed":
                assert harness.result.output == "a b c d e f"
        assert _live_bridges() == 0
        print("race-outcomes", outcomes)
        assert {"completed", "cancelled"} <= set(outcomes), outcomes


# ---------------------------------------------------------------------------
# C09: cancelling after an approved tool ran does not re-run or fake that tool.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("api", APIS)
def test_cancel_after_approved_tool_keeps_single_tool_result(api: str, make_runner) -> None:
    call = {"id": "write-once", "name": "write_file",
            "args": {"file_path": "/once.txt", "content": "written once"}}
    turns = [Turn(tool_calls=[call]), Turn(mode="hang"), Turn(text="all good")]
    with ScriptedSSEServer(api, turns) as server:
        runner = make_runner(server)
        waiting = runner.invoke("write it")
        assert waiting.status == "waiting_confirmation"
        # Cancelling while idle at the approval prompt must not execute the tool.
        runner.request_cancel()
        state = runner.prepared.graph.get_state(runner._thread_config())
        assert "/once.txt" not in (state.values.get("files") or {})
        harness = Harness(runner).start(runner.approve_tool, "write-once")
        server.wait_for_connection(1).hanging.wait(5)
        report = _cancel_and_measure(harness, server, 1)
        _assert_clean_cancel(harness, server, report)
        state = runner.prepared.graph.get_state(runner._thread_config())
        tool_results = [item for item in state.values["messages"]
                        if isinstance(item, ToolMessage) and item.tool_call_id == "write-once"]
        assert len(tool_results) == 1
        assert state.values["files"]["/once.txt"]["content"] == "written once"
        result = runner.invoke("continue")
        assert result.status == "completed" and result.output == "all good"
        state = runner.prepared.graph.get_state(runner._thread_config())
        tool_results = [item for item in state.values["messages"]
                        if isinstance(item, ToolMessage) and item.tool_call_id == "write-once"]
        assert len(tool_results) == 1


# ---------------------------------------------------------------------------
# C11 / C13: close after cancel and repeated start/cancel cycles leak nothing.
# ---------------------------------------------------------------------------
def test_repeated_start_cancel_cycles_do_not_leak(make_runner, tmp_path: Path) -> None:
    cycles = 10
    with ScriptedSSEServer("chat_completions", [Turn(mode="hang") for _ in range(cycles)]) as server:
        runner = make_runner(server)
        # Warm up the shared stream loop so it is not counted as a leak.
        warm = Harness(runner).start(runner.invoke, "warm")
        server.wait_for_connection(0).hanging.wait(5)
        runner.request_cancel()
        assert warm.join(CANCEL_BUDGET)
        gc.collect()
        baseline_threads = {thread.ident for thread in threading.enumerate()}
        for index in range(1, cycles):
            harness = Harness(runner).start(runner.invoke, f"cycle {index}")
            server.wait_for_connection(index).hanging.wait(5)
            runner.request_cancel()
            assert harness.join(CANCEL_BUDGET), index
            assert harness.result.status == "cancelled"
            assert server.connections[index].closed.wait(CANCEL_BUDGET)
        gc.collect()
        time.sleep(0.2)
        extra = [thread.name for thread in threading.enumerate()
                 if thread.ident not in baseline_threads and thread.name != "runner-op"]
        assert extra == [], extra
        assert server.open_connections() == []
        assert _live_bridges() == 0
        thread_id = runner.thread_id
        runner.close()
        # C11: the session lease is released once the runner closed cleanly.
        other_store = SessionStore(tmp_path / "state.sqlite3")
        try:
            other = AgentRunner(settings=settings_for(server), backend=StateBackend(),
                                session_store=other_store, thread_id=thread_id)
            other.close()
        finally:
            other_store.close()


# ---------------------------------------------------------------------------
# C16: a cancel issued after the caller dispatched a run, but before begin_run,
# must not be cleared by begin_run.
# ---------------------------------------------------------------------------
def test_cancel_before_begin_run_is_not_lost(make_runner) -> None:
    with ScriptedSSEServer("chat_completions", [Turn(text="should not run")]) as server:
        runner = make_runner(server)
        reservation = runner.reserve_run()
        runner.request_cancel()
        try:
            result = runner.invoke("dispatched then cancelled")
        finally:
            runner.release_run_reservation(reservation)
        assert result.status == "cancelled"
        assert server.connections == []
        # The consumed cancellation does not leak into the next run.
        server.add(Turn(text="next run"))
        assert runner.invoke("again").status == "completed"


def test_idle_cancel_does_not_affect_next_run(make_runner) -> None:
    with ScriptedSSEServer("chat_completions", [Turn(text="ran")]) as server:
        runner = make_runner(server)
        runner.request_cancel()
        result = runner.invoke("hello")
        assert result.status == "completed" and result.output == "ran"


# ---------------------------------------------------------------------------
# Bridge and controller units
# ---------------------------------------------------------------------------
def _bridge(factory, **kwargs):
    from agent.llm_stream import CancellableStreamBridge
    return CancellableStreamBridge(factory, **kwargs)


def test_bridge_buffer_is_bounded_and_preserves_order() -> None:
    import asyncio

    produced = []

    async def source():
        for index in range(200):
            produced.append(index)
            yield index
            await asyncio.sleep(0)

    bridge = _bridge(source, max_buffered=4)
    received = []
    for item in bridge.iter_chunks():
        received.append(item)
        if len(received) == 1:
            time.sleep(0.2)
            # The consumer stalled: the producer may only run ahead by the buffer size.
            assert len(produced) <= 1 + 4 + 1, len(produced)
    assert received == list(range(200))
    assert bridge.closed and _live_bridges() == 0


def test_bridge_propagates_errors_and_natural_completion_is_not_cancel() -> None:
    async def failing():
        yield 1
        raise ValueError("upstream broke")

    bridge = _bridge(failing)
    items = []
    with pytest.raises(ValueError, match="upstream broke"):
        for item in bridge.iter_chunks():
            items.append(item)
    assert items == [1] and bridge.closed

    async def finite():
        yield "a"
        yield "b"

    bridge = _bridge(finite)
    assert list(bridge.iter_chunks()) == ["a", "b"]
    bridge.cancel()  # late cancel after completion changes nothing
    assert bridge.closed and _live_bridges() == 0


def test_bridge_cancel_interrupts_a_blocked_await_from_another_thread() -> None:
    import asyncio

    from agent.llm_stream import ModelStreamCancelled

    cleaned = threading.Event()

    async def blocked():
        try:
            yield "first"
            await asyncio.sleep(3600)
            yield "never"
        finally:
            cleaned.set()

    bridge = _bridge(blocked)
    iterator = bridge.iter_chunks()
    assert next(iterator) == "first"
    timer = threading.Timer(0.1, bridge.cancel)
    timer.start()
    started = time.monotonic()
    with pytest.raises(ModelStreamCancelled) as error:
        next(iterator)
    assert time.monotonic() - started < 2
    assert error.value.closed and cleaned.is_set() and bridge.closed
    assert _live_bridges() == 0


def test_bridge_cancelled_before_start_never_runs() -> None:
    from agent.llm_stream import ModelStreamCancelled

    started = threading.Event()

    async def source():
        started.set()
        yield 1

    bridge = _bridge(source)
    bridge.cancel()
    with pytest.raises(ModelStreamCancelled):
        list(bridge.iter_chunks())
    assert not started.is_set() and bridge.closed


def test_bridge_abandoned_consumer_closes_request() -> None:
    import asyncio

    cleaned = threading.Event()

    async def endless():
        try:
            while True:
                yield 1
                await asyncio.sleep(0.001)
        finally:
            cleaned.set()

    bridge = _bridge(endless)
    iterator = bridge.iter_chunks()
    next(iterator)
    iterator.close()
    assert cleaned.wait(2) and bridge.closed and _live_bridges() == 0


class _FakeStream:
    def __init__(self) -> None:
        self.cancelled = 0
        self.closed = False

    def cancel(self) -> None:
        self.cancelled += 1
        self.closed = True

    def wait_closed(self, timeout: float) -> bool:
        return self.closed


def test_controller_stream_registration_is_scoped_to_the_active_run() -> None:
    from agent.control import RunController

    events = []
    controller = RunController(on_control_event=lambda kind, _payload: events.append(kind))
    idle = _FakeStream()
    assert not controller.register_model_stream(idle), "no run is active"
    controller.begin_run()
    first = _FakeStream()
    assert controller.register_model_stream(first)
    controller.unregister_model_stream(first)
    controller.end_run()
    controller.begin_run()
    second = _FakeStream()
    assert controller.register_model_stream(second)
    for _ in range(5):
        controller.cancel()
    assert events.count("run_cancelling") == 1
    assert second.cancelled == 1
    assert first.cancelled == 0, "a stream of an earlier run was cancelled"
    late = _FakeStream()
    assert not controller.register_model_stream(late), "cancelled run accepted a new request"
    controller.unregister_model_stream(second)
    controller.end_run()
    controller.begin_run()
    assert not controller.cancel_requested
    fresh = _FakeStream()
    assert controller.register_model_stream(fresh)
    controller.unregister_model_stream(fresh)
    controller.end_run()


def test_controller_reservation_keeps_early_cancel_and_isolates_generations() -> None:
    from agent.control import RunController

    controller = RunController()
    token = controller.reserve_run()
    controller.cancel()
    run_control = controller.begin_run()
    assert controller.cancel_requested
    assert controller.run_generation == token
    assert run_control.drain_requested
    controller.end_run()
    controller.begin_run()
    assert not controller.cancel_requested
    controller.end_run()
    # A released reservation does not cancel the next run.
    token = controller.reserve_run()
    controller.release_reservation(token)
    controller.cancel()
    controller.begin_run()
    assert not controller.cancel_requested
    controller.end_run()


def test_controller_unclosed_stream_is_reported() -> None:
    from agent.control import RunController

    controller = RunController()
    controller.begin_run()
    stuck = _FakeStream()
    assert controller.register_model_stream(stuck)
    controller.unregister_model_stream(stuck)
    assert controller.unclosed_model_streams() == 1
    assert not controller.wait_model_streams_closed(0.01)
    stuck.closed = True
    assert controller.wait_model_streams_closed(0.01)
    controller.end_run()


def test_runner_close_keeps_lease_while_model_stream_unclosed(make_runner, tmp_path: Path) -> None:
    from agent.session_lock import SessionLockBusyError

    with ScriptedSSEServer("chat_completions", []) as server:
        runner = make_runner(server)
        runner.control.begin_run()
        stuck = _FakeStream()
        assert runner.control.register_model_stream(stuck)
        runner.control.unregister_model_stream(stuck)
        runner.control.end_run()
        with pytest.raises(RuntimeError, match="model request"):
            runner.close()
        other_store = SessionStore(tmp_path / "state.sqlite3")
        try:
            with pytest.raises(SessionLockBusyError):
                AgentRunner(settings=settings_for(server), backend=StateBackend(),
                            session_store=other_store, thread_id=runner.thread_id)
        finally:
            other_store.close()
        stuck.closed = True
        runner.close()


def test_unclosed_stream_reports_cleanup_failure(make_runner, monkeypatch) -> None:
    import agent.runner as runner_module

    with ScriptedSSEServer("chat_completions", [Turn(mode="hang")]) as server:
        runner = make_runner(server)
        monkeypatch.setattr(runner_module, "MODEL_STREAM_CLOSE_SECONDS", 0.05)
        stuck = _FakeStream()
        original_cancel = runner.control.cancel

        def cancel_with_stuck_stream():
            runner.control.register_model_stream(stuck)
            runner.control.unregister_model_stream(stuck)
            original_cancel()
            stuck.closed = False

        harness = Harness(runner).start(runner.invoke, "start")
        server.wait_for_connection(0).hanging.wait(5)
        monkeypatch.setattr(runner.control, "cancel", cancel_with_stuck_stream)
        runner.request_cancel()
        assert harness.join(CANCEL_BUDGET)
        assert harness.result.status == "cancelled"
        assert harness.result.error == runner_module.MODEL_STREAM_UNCLOSED
        cancelled = [event for _stamp, event in harness.events if event.type == "run_cancelled"]
        assert cancelled and cancelled[0].is_error
        stuck.closed = True


def test_unregistered_model_still_streams_outside_a_run(make_runner) -> None:
    with ScriptedSSEServer("chat_completions", [Turn(text="plain call")]) as server:
        runner = make_runner(server)
        message = runner._chat_model.invoke([HumanMessage(content="hi")])
        assert message.content == "plain call"
        assert _live_bridges() == 0
