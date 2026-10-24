"""Real HTTP/SDK/Agent regressions for request retries and stream rollback."""
import time

import httpx
import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from agent.cli.state import CliState, MessageBlock
from agent.config import ModelProfile, Settings
from agent.factory import create_agent
from agent.llm import build_chat_model
from agent.llm_stream import live_stream_count
from agent.runner import AgentRunner
from agent.session import SessionStore
from agent.stream import StreamDeltaCallback
from tests.model_stream_server import ScriptedSSEServer, Turn
from tests.test_model_stream_cancel import Harness

pytestmark = pytest.mark.integration
APIS = ["responses", "chat_completions"]


@pytest.fixture
def runner_factory(tmp_path):
    runners = []
    def build(server, *, retries=5, hidden=(), tools=(), use_prepared=True):
        settings = Settings(
            llm_profiles=(ModelProfile("local/retry", "test", base_url=server.base_url,
                                       api=server.api, max_retries=retries),),
            llm_default="local/retry", hidden_tools=hidden,
        )
        backend = StateBackend()
        prepared = create_agent(settings=settings, backend=backend, extra_tools=list(tools)) if use_prepared else None
        store = SessionStore(tmp_path / f"state-{len(runners)}.sqlite3")
        runner = AgentRunner(settings=settings, prepared=prepared, backend=backend if prepared is None else None, session_store=store,
                             thread_id=f"retry-{len(runners)}")
        runners.append((runner, store))
        return runner
    yield build
    for runner, store in runners:
        runner.close()
        store.close()


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("status", [429, 500, 503])
def test_request_retries_twice_then_agent_completes(api, status, runner_factory):
    with ScriptedSSEServer(api, [Turn(status=status), Turn(status=status), Turn(text="done")]) as server:
        runner = runner_factory(server)
        result = runner.invoke("hello")
        assert result.status == "completed" and result.output == "done"
        assert len(server.connections) == 3
        assert all(c.body == server.connections[0].body for c in server.connections)


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("mode", ["disconnect", "transport_disconnect"])
def test_midstream_text_and_reasoning_are_replaced(api, mode, runner_factory):
    failed = Turn(text="broken partial answer", reasoning="broken thought", mode=mode,
                  chunks_before_hang=3 if api == "responses" else 2)
    with ScriptedSSEServer(api, [failed, Turn(text="fresh answer", reasoning="fresh thought")]) as server:
        runner = runner_factory(server)
        deltas, events = [], []
        result = runner.invoke("hello", on_delta=lambda kind, text: deltas.append((kind, text)),
                               on_event=events.append)
        assert result.status == "completed" and result.output == "fresh answer"
        assert len(server.connections) == 2
        assert server.connections[0].body == server.connections[1].body
        assert ("assistant", "") in deltas and ("reasoning", "") in deltas
        assert deltas[-1] == ("assistant", "fresh answer")
        assert all("broken" not in text for kind, text in deltas[deltas.index(("assistant", "")):])
        completed = [e for e in events if e.type == "assistant_completed"]
        assert len(completed) == 1 and completed[0].content == "fresh answer"
        rates = [e.result["tps"] for e in events if e.type == "model_metrics"]
        assert len(rates) == 1 and rates[0] > 0
        assert runner.model_metrics_history() == [e.result for e in events if e.type == "model_metrics"]
        state = CliState()
        for event in events:
            state.apply(event)
        blocks = [b for b in state.blocks if isinstance(b, MessageBlock) and b.kind == "assistant"]
        assert len(blocks) == 1
        assert blocks[0].content == "fresh answer" and blocks[0].thinking == "fresh thought"
        values = runner.prepared.graph.get_state(runner._thread_config()).values
        assert len([m for m in values["messages"] if isinstance(m, AIMessage)]) == 1
        assert live_stream_count() == 0


@pytest.mark.parametrize("api", APIS)
def test_partial_tool_arguments_are_discarded_and_tool_executes_once(api, runner_factory):
    calls = []
    @tool
    def record(value: str) -> str:
        """Record a value once."""
        calls.append(value)
        return value
    call = {"id": "call-1", "name": "record", "args": {"value": "complete"}}
    with ScriptedSSEServer(api, [Turn(tool_calls=[call], mode="disconnect_tool"),
                                 Turn(tool_calls=[call]), Turn(text="done")]) as server:
        runner = runner_factory(server, tools=[record])
        result = runner.invoke("record")
        assert result.status == "completed" and result.output == "done"
        assert calls == ["complete"]
        assert len(server.connections) == 3
        values = runner.prepared.graph.get_state(runner._thread_config()).values
        results = [m for m in values["messages"] if isinstance(m, ToolMessage)]
        assert len(results) == 1 and results[0].content == "complete"


@pytest.mark.parametrize("api", APIS)
def test_retry_does_not_reexecute_completed_tool(api, runner_factory):
    calls = []
    @tool
    def record() -> str:
        """Record a completed action."""
        calls.append(1)
        return "recorded"
    call = {"id": "call-1", "name": "record", "args": {}}
    with ScriptedSSEServer(api, [Turn(text="calling tool", tool_calls=[call]),
                                 Turn(text="broken text", mode="disconnect", chunks_before_hang=2),
                                 Turn(text="done", reasoning="thinking briefly")]) as server:
        events = []
        result = runner_factory(server, tools=[record]).invoke("record", on_event=events.append)
        rates = [e.result["tps"] for e in events if e.type == "model_metrics"]
        assert len(rates) == 2 and all(rate > 0 for rate in rates)
        state = CliState()
        tools_seen = 0
        for event in events:
            state.apply(event)
            if event.type in {"tool_started", "tool_completed"}:
                tools_seen += 1
                assert state.model_metrics_history[-1]["tps"] == rates[0]
                from agent.performance import model_metric_groups
                group = model_metric_groups(state.model_metrics_history)[0]
                assert group["tps_low"] == group["tps_top"] == group["tps_average"] == rates[0]
        assert tools_seen >= 2
        assert len(state.model_metrics_history) == 2
        assert result.status == "completed" and result.output == "done"
        assert calls == [1] and len(server.connections) == 3
        assert server.connections[1].body == server.connections[2].body


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("phase", ["sdk", "body"])
def test_cancel_during_retry_stops_requests(api, phase, runner_factory):
    failed = (Turn(status=500, retry_after="60") if phase == "sdk" else
              Turn(text="partial answer", mode="disconnect", chunks_before_hang=2))
    with ScriptedSSEServer(api, [failed, Turn(text="must not run")]) as server:
        runner = runner_factory(server)
        harness = Harness(runner).start(runner.invoke, "hello")
        if phase == "sdk":
            assert server.wait_for_connection(0).closed.wait(5)
        else:
            harness.wait_for(lambda: any(e.type == "assistant_delta" and not e.content
                                         for _, e in harness.events))
        runner.request_cancel()
        assert harness.join(3) and harness.error is None
        assert harness.result.status == "cancelled"
        assert len(server.connections) == 1 and live_stream_count() == 0


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("phase", ["sdk", "body"])
def test_retry_exhaustion_is_a_clear_failure(api, phase, runner_factory):
    failed = (Turn(status=503) if phase == "sdk" else
              Turn(text="partial answer", mode="disconnect", chunks_before_hang=2))
    with ScriptedSSEServer(api, [failed, failed, failed]) as server:
        result = runner_factory(server, retries=2).invoke("hello")
        assert result.status == "failed" and result.error
        assert ("503" if phase == "sdk" else "after 3 attempts") in result.error
        assert len(server.connections) == 3


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("status", [400, 401, 403])
def test_nonretryable_request_errors_are_not_retried(api, status, runner_factory):
    with ScriptedSSEServer(api, [Turn(status=status)]) as server:
        result = runner_factory(server).invoke("hello")
        assert result.status == "failed" and str(status) in result.error
        assert len(server.connections) == 1


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("hidden", [(), ("request_human_input",), ("compact_conversation",),
                                     ("request_human_input", "ls", "read_file", "compact_conversation")])
@pytest.mark.parametrize("use_prepared", [False, True])
def test_hidden_tools_are_absent_from_real_requests(api, hidden, use_prepared, runner_factory):
    with ScriptedSSEServer(api, [Turn(text="done")]) as server:
        runner = runner_factory(server, hidden=hidden, use_prepared=use_prepared)
        result = runner.invoke("hello")
        assert result.status == "completed"
        body = server.connections[0].body
        names = {t.get("function", t)["name"] for t in body["tools"]}
        if hidden:
            assert not (set(hidden) & names)
            if "request_human_input" in hidden:
                assert "request_human_input" not in str(body.get("instructions", body.get("messages", body.get("input"))))
        else:
            assert {"request_human_input", "ls", "read_file", "compact_conversation"} <= names
        assert "write_file" in names
        assert set(runner.prepared.model_tool_names) == names
        from agent.cli.app import CliApplication
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app.show_status()
            listed = ", ".join(runner.prepared.model_tool_names)
            assert f"Tools: {len(names)} · {listed}" in app.state.blocks[-1].content
        # Hiding is a schema choice; the ASK rules are unchanged.
        from agent.permission import PermissionMode, interrupt_on_for_mode
        assert runner.prepared.interrupt_on == interrupt_on_for_mode(PermissionMode.ASK)


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_nonstreaming_sdk_retries_connection_and_timeout(error):
    requests = []
    def respond(request):
        requests.append(request)
        if len(requests) < 3:
            raise error("temporary failure", request=request)
        return httpx.Response(200, json={"id": "c", "object": "chat.completion", "created": 0,
            "model": "test", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "done"}}]})
    model = build_chat_model(ModelProfile("test", "test", api="chat_completions"), streaming=False)
    # Keep SDK's retry loop; shorten only its test backoff.
    model.root_client._calculate_retry_timeout = lambda *a: 0
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model.root_client._client = client
        assert model.invoke([HumanMessage(content="hello")]).content == "done"
    assert len(requests) == 3


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("text", ["", "partial answer"])
def test_stream_body_timeout_retries_without_replaying_tools(api, text, runner_factory):
    with ScriptedSSEServer(api, [Turn(text=text, mode="hang", chunks_before_hang=1),
                                 Turn(text="done")]) as server:
        runner = runner_factory(server)
        client = runner._chat_model.root_async_client
        getattr(client, "_target", client).timeout = httpx.Timeout(0.15)
        result = runner.invoke("hello")
        assert result.status == "completed" and result.output == "done"
        assert len(server.connections) == 2
        assert server.connections[0].closed.wait(2)
        assert live_stream_count() == 0


@pytest.mark.parametrize("api", APIS)
def test_async_model_retry_resets_callback_and_accumulator(api):
    import asyncio
    with ScriptedSSEServer(api, [Turn(text="broken partial", mode="disconnect", chunks_before_hang=2),
                                 Turn(text="done")]) as server:
        model = build_chat_model(ModelProfile("test", "test", base_url=server.base_url, api=api))
        deltas = []
        callback = StreamDeltaCallback(lambda kind, text: deltas.append((kind, text)))
        async def invoke():
            try:
                return await model.ainvoke([HumanMessage(content="hi")], config={"callbacks": [callback]})
            finally:
                await model.root_async_client.close()
        message = asyncio.run(invoke())
        assert message.text == "done"
        assert ("assistant", "") in deltas and deltas[-1] == ("assistant", "done")
        assert len(server.connections) == 2


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("outcome", ["success", "http_failure", "stream_failure"])
def test_mixed_failures_share_six_request_budget(api, asynchronous, outcome, runner_factory, monkeypatch):
    import asyncio
    import agent.llm as llm_module
    import agent.llm_stream as stream_module

    # Verify the request budget without spending time on exponential backoff.
    monkeypatch.setattr(llm_module, "stream_retry_delay", lambda attempt: 0)
    monkeypatch.setattr(stream_module, "stream_retry_delay", lambda attempt: 0)
    broken = Turn(text="broken partial answer", mode="disconnect", chunks_before_hang=2)
    final = {"success": Turn(text="done"), "http_failure": Turn(status=503),
             "stream_failure": broken}[outcome]
    # Attempt 2 fails in the transport before reaching the server. Keep a
    # seventh success queued: it must never be consumed after budget exhaustion.
    with ScriptedSSEServer(api, [Turn(status=503), broken, Turn(status=503), broken,
                                 final, Turn(text="must not run")]) as server:
        runner = runner_factory(server)
        model = runner._chat_model
        sdk = getattr(model.root_async_client, "_target", model.root_async_client)
        requests = []
        transport = httpx.AsyncHTTPTransport()
        async def respond(request):
            requests.append(request)
            if len(requests) == 2:
                raise httpx.ConnectTimeout("scripted connection timeout", request=request)
            return await transport.handle_async_request(request)
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)
        sdk._client = client
        async def invoke():
            try:
                return await model.ainvoke([HumanMessage(content="hello")])
            finally:
                await client.aclose()
                await transport.aclose()
        try:
            if asynchronous:
                if outcome == "success":
                    assert asyncio.run(invoke()).text == "done"
                else:
                    with pytest.raises(RuntimeError, match="after 6 attempts"):
                        asyncio.run(invoke())
            else:
                result = runner.invoke("hello")
                if outcome == "success":
                    assert result.status == "completed" and result.output == "done"
                else:
                    assert result.status == "failed" and "after 6 attempts" in result.error
            assert len(requests) == 6
            assert len(server.connections) == 5
            assert all(request.headers["x-stainless-retry-count"] == "0" for request in requests)
            # The call-local override must not mutate shared SDK policy.
            assert sdk.max_retries == model.root_client.max_retries == 5
            assert live_stream_count() == 0
        finally:
            if not asynchronous:
                asyncio.run(client.aclose())
                asyncio.run(transport.aclose())


def _no_backoff(monkeypatch) -> None:
    import agent.llm as llm_module
    import agent.llm_stream as stream_module

    monkeypatch.setattr(llm_module, "stream_retry_delay", lambda _attempt: 0)
    monkeypatch.setattr(stream_module, "stream_retry_delay", lambda _attempt: 0)


def _invoke(runner, prompt: str, *, timeout: float = 6):
    harness = Harness(runner).start(runner.invoke, prompt)
    if not harness.join(timeout):
        runner.request_cancel()
        harness.join(2)
        pytest.fail(f"run still going after {timeout}s; events={harness.types()}")
    assert harness.error is None, harness.error
    return harness


@pytest.mark.parametrize("api", APIS)
def test_build_pins_idle_timeout_even_when_env_is_short(api, monkeypatch):
    from agent.llm import STREAM_CHUNK_TIMEOUT_SECONDS

    monkeypatch.setenv("LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S", "0.5")
    model = build_chat_model(ModelProfile("local/idle", "test", api=api))
    assert model.stream_chunk_timeout == STREAM_CHUNK_TIMEOUT_SECONDS == 120.0


@pytest.mark.parametrize("api", APIS)
def test_idle_after_first_chunk_retries_the_same_model_request(api, runner_factory, monkeypatch):
    _no_backoff(monkeypatch)
    stalled = Turn(text="hello there", mode="hang", chunks_before_hang=1, keepalive_interval=0.05)
    with ScriptedSSEServer(api, [stalled, Turn(text="done")]) as server:
        runner = runner_factory(server)
        runner._chat_model.stream_chunk_timeout = 0.2
        harness = _invoke(runner, "hello")
        assert harness.result.status == "completed" and harness.result.output == "done"
        assert len(server.connections) == 2
        assert server.connections[0].body == server.connections[1].body
        assert server.connections[0].keepalives_sent >= 1
        assert server.connections[0].closed.wait(2)
        values = runner.prepared.graph.get_state(runner._thread_config()).values
        answers = [message for message in values["messages"] if isinstance(message, AIMessage)]
        assert len(answers) == 1 and answers[0].text == "done"
        assert live_stream_count() == 0


@pytest.mark.parametrize("api", APIS)
def test_idle_mid_tool_call_discards_partial_arguments(api, runner_factory, monkeypatch):
    _no_backoff(monkeypatch)
    calls = []

    @tool
    def record(value: str) -> str:
        """Record a value once."""
        calls.append(value)
        return value

    call = {"id": "call-1", "name": "record", "args": {"value": "complete"}}
    stalled = Turn(tool_calls=[call], mode="hang_tool", keepalive_interval=0.05)
    with ScriptedSSEServer(api, [stalled, Turn(tool_calls=[call]), Turn(text="done")]) as server:
        runner = runner_factory(server, tools=[record])
        runner._chat_model.stream_chunk_timeout = 0.2
        harness = _invoke(runner, "record", timeout=8)
        assert harness.result.status == "completed" and harness.result.output == "done"
        assert calls == ["complete"]
        assert len(server.connections) == 3
        assert server.connections[0].body == server.connections[1].body
        assert server.connections[0].keepalives_sent >= 1
        values = runner.prepared.graph.get_state(runner._thread_config()).values
        results = [message for message in values["messages"] if isinstance(message, ToolMessage)]
        assert len(results) == 1 and results[0].content == "complete"


@pytest.mark.parametrize("api", APIS)
def test_idle_after_completed_tool_does_not_rerun_it(api, runner_factory, monkeypatch):
    _no_backoff(monkeypatch)
    calls = []

    @tool
    def record() -> str:
        """Record a completed action."""
        calls.append(1)
        return "recorded"

    call = {"id": "call-1", "name": "record", "args": {}}
    stalled = Turn(text="partial later", mode="hang", chunks_before_hang=1, keepalive_interval=0.05)
    with ScriptedSSEServer(api, [Turn(text="calling tool", tool_calls=[call]), stalled,
                                 Turn(text="done")]) as server:
        runner = runner_factory(server, tools=[record])
        runner._chat_model.stream_chunk_timeout = 0.2
        harness = _invoke(runner, "record", timeout=8)
        assert harness.result.status == "completed" and harness.result.output == "done"
        assert calls == [1]
        assert len(server.connections) == 3
        assert server.connections[1].body == server.connections[2].body
        assert server.connections[1].keepalives_sent >= 1
        values = runner.prepared.graph.get_state(runner._thread_config()).values
        results = [message for message in values["messages"] if isinstance(message, ToolMessage)]
        assert len(results) == 1 and results[0].content == "recorded"


@pytest.mark.parametrize("api", APIS)
def test_steady_chunks_are_not_cut_by_the_idle_timeout(api, runner_factory):
    text = " ".join(f"w{i}" for i in range(10))
    with ScriptedSSEServer(api, [Turn(text=text, interval=0.05)]) as server:
        runner = runner_factory(server)
        runner._chat_model.stream_chunk_timeout = 0.2
        started = time.monotonic()
        harness = _invoke(runner, "hello", timeout=6)
        elapsed = time.monotonic() - started
        assert harness.result.status == "completed"
        assert harness.result.output == text
        assert len(server.connections) == 1
        assert elapsed > 0.2


@pytest.mark.parametrize("api", APIS)
def test_cancel_during_idle_stream_does_not_retry(api, runner_factory):
    stalled = Turn(text="partial answer", mode="hang", chunks_before_hang=1)
    with ScriptedSSEServer(api, [stalled, Turn(text="must not run")]) as server:
        runner = runner_factory(server)
        assert runner._chat_model.stream_chunk_timeout == 120.0
        harness = Harness(runner).start(runner.invoke, "hello")
        assert server.wait_for_connection(0).hanging.wait(5)
        runner.request_cancel()
        assert harness.join(3) and harness.error is None
        assert harness.result.status == "cancelled"
        time.sleep(0.3)
        assert len(server.connections) == 1
        assert live_stream_count() == 0


@pytest.mark.parametrize("api", APIS)
def test_idle_timeout_exhausts_the_retry_budget(api, runner_factory, monkeypatch):
    _no_backoff(monkeypatch)
    stalled = Turn(text="partial answer", mode="hang", chunks_before_hang=1, keepalive_interval=0.05)
    with ScriptedSSEServer(api, [stalled, stalled, stalled, Turn(text="must not run")]) as server:
        runner = runner_factory(server, retries=2)
        runner._chat_model.stream_chunk_timeout = 0.15
        harness = _invoke(runner, "hello")
        assert harness.result.status == "failed"
        assert "after 3 attempts" in harness.result.error
        assert "idle timeout" in harness.result.error
        assert len(server.connections) == 3
        assert all(connection.keepalives_sent >= 1 for connection in server.connections)
