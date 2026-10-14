"""HTTP contract, concurrency, interruption and restart tests without real models."""
from __future__ import annotations

from pathlib import Path
from threading import Event
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from agent.runner import InterruptKind, InterruptState, RunEvent, RunResult
from agent.server.app import create_app
from agent.server.config import ServerConfig
from agent.server.runtime import RunManager
from agent.server.store import ServiceStore, TERMINAL

AUTH = {"Authorization": "Bearer service-secret"}


class FakeRunner:
    session_store = None

    def __init__(self):
        self.cancelled = Event()
        self.state = None
        self.closed = False

    def current_interrupt(self):
        return self.state

    def invoke(self, text, **callbacks):
        callbacks["on_event"](RunEvent(type="run_started"))
        callbacks["on_delta"]("assistant", "hello")
        if text == "block":
            self.cancelled.wait(5)
            return RunResult(status="cancelled")
        if text == "approve":
            self.state = InterruptState(InterruptKind.WAITING_CONFIRMATION, {}, (
                {"toolCallId": "a", "name": "write_file"}, {"toolCallId": "b", "name": "execute"},
            ))
            return RunResult(status="waiting_confirmation", pending_tool_calls=list(self.state.pending_tools))
        if text == "human":
            self.state = InterruptState(InterruptKind.WAITING_HUMAN, {"question": "Which directory?"})
            return RunResult(status="waiting_human", human_input=self.state.payload)
        return RunResult(status="completed", output="hello")

    def decide_tools(self, decisions, **callbacks):
        self.state = None
        return RunResult(status="completed", output=",".join(item["decision"] for item in decisions))

    def submit_human_input(self, values, **callbacks):
        if "text" not in values:
            raise ValueError("Invalid answer")
        self.state = None
        return RunResult(status="completed", output=values["text"])

    def request_cancel(self):
        self.cancelled.set()

    def close(self):
        self.closed = True


@pytest.fixture
def config(tmp_path):
    projects = {}
    for project in ("one", "two", "three"):
        path = tmp_path / project
        path.mkdir()
        projects[project] = path
    return ServerConfig(projects, tmp_path / "model.yaml", tmp_path / "state", "service-secret")


@pytest.fixture
def factory():
    return lambda config, project_id, session_id: FakeRunner()


@pytest.fixture
def client(config, factory):
    with TestClient(create_app(config, factory=factory)) as client:
        yield client


def session(client, project="one"):
    response = client.post("/v1/sessions", json={"project_id": project}, headers=AUTH)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def submit(client, sid, text):
    response = client.post(f"/v1/sessions/{sid}/runs", json={"text": text}, headers=AUTH)
    assert response.status_code == 202, response.text
    return response.json()["id"]


def wait(client, rid, status):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = client.get(f"/v1/runs/{rid}", headers=AUTH).json()
        if run["status"] == status:
            return run
        time.sleep(0.005)
    pytest.fail(f"Run did not reach {status}: {run}")


def test_auth_projects_and_validation(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/v1/projects").status_code == 401
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/v1/projects", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/v1/projects", headers=AUTH).json() == {"projects": [{"id": "one"}, {"id": "two"}, {"id": "three"}]}
    assert client.post("/v1/sessions", json={"project_id": "missing"}, headers=AUTH).status_code == 404
    assert client.post("/v1/sessions", json={"project_id": "one", "workspace": "/tmp"}, headers=AUTH).status_code == 422
    assert client.get("/v1/runs/missing", headers=AUTH).status_code == 404
    sid = session(client)
    assert client.get(f"/v1/sessions/{sid}", headers=AUTH).json()["id"] == sid
    assert client.get("/v1/sessions?project_id=one", headers=AUTH).json()["sessions"][0]["id"] == sid
    for body in ({}, {"text": " "}, {"text": "hello", "resume": True}, {"text": "x", "model": "other"}):
        assert client.post(f"/v1/sessions/{sid}/runs", json=body, headers=AUTH).status_code == 422


def test_completion_sse_and_replay(client):
    rid = submit(client, session(client), "hello")
    assert wait(client, rid, "completed")["result"]["output"] == "hello"
    response = client.get(f"/v1/runs/{rid}/events", headers=AUTH)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: delta" in response.text and "event: run_finished" in response.text
    ids = [int(line.split(": ")[1]) for line in response.text.splitlines() if line.startswith("id:")]
    assert ids == sorted(set(ids))
    replay = client.get(f"/v1/runs/{rid}/events", headers={**AUTH, "Last-Event-ID": str(ids[-2])})
    assert "event: run_finished" in replay.text and "event: delta" not in replay.text
    assert client.get(f"/v1/runs/{rid}/events?after={ids[-1]}", headers=AUTH).text == ""
    assert client.get(f"/v1/runs/{rid}/events", headers={**AUTH, "Last-Event-ID": "bad"}).status_code == 422


def test_approval_batch_stale_and_project_lock(client):
    sid = session(client)
    rid = submit(client, sid, "approve")
    run = wait(client, rid, "waiting_confirmation")
    assert client.post(f"/v1/sessions/{session(client)}/runs", json={"text": "hello"}, headers=AUTH).status_code == 409
    url = f"/v1/runs/{rid}/approvals"
    body = {"interrupt_id": run["interrupt_id"], "decisions": [{"tool_call_id": "a", "decision": "approve"}]}
    assert client.post(url, json=body, headers=AUTH).status_code == 409
    body["decisions"].append({"tool_call_id": "b", "decision": "reject", "message": "No network"})
    assert client.post(url, json={**body, "interrupt_id": "stale"}, headers=AUTH).status_code == 409
    assert client.post(url, json=body, headers=AUTH).status_code == 202
    assert wait(client, rid, "completed")["result"]["output"] == "approve,reject"
    assert client.post(url, json=body, headers=AUTH).status_code == 409
    next_run = submit(client, sid, "hello")
    wait(client, next_run, "completed")


def test_human_input_validation_and_cancel(client):
    rid = submit(client, session(client), "human")
    run = wait(client, rid, "waiting_human")
    url = f"/v1/runs/{rid}/human-input"
    assert client.post(url, json={"interrupt_id": run["interrupt_id"], "values": {}}, headers=AUTH).status_code == 202
    current = wait(client, rid, "waiting_human")
    assert current["interrupt_id"] != run["interrupt_id"]
    assert client.post(url, json={"interrupt_id": run["interrupt_id"], "values": {"text": "old"}}, headers=AUTH).status_code == 409
    assert client.post(url, json={"interrupt_id": current["interrupt_id"], "values": {"text": "src"}}, headers=AUTH).status_code == 202
    assert wait(client, rid, "completed")["result"]["output"] == "src"
    rid = submit(client, session(client), "human")
    wait(client, rid, "waiting_human")
    assert client.post(f"/v1/runs/{rid}/cancel", headers=AUTH).status_code == 202
    wait(client, rid, "cancelled")
    assert client.post(f"/v1/runs/{rid}/cancel", headers=AUTH).status_code == 409


def test_capacity_cross_project_and_running_cancel(client):
    first = submit(client, session(client, "one"), "block")
    second = submit(client, session(client, "two"), "block")
    third_session = session(client, "three")
    assert client.post(f"/v1/sessions/{third_session}/runs", json={"text": "hello"}, headers=AUTH).status_code == 429
    assert client.get("/health").status_code == 200
    for rid in (first, second):
        assert client.post(f"/v1/runs/{rid}/cancel", headers=AUTH).status_code == 202
        wait(client, rid, "cancelled")
    wait(client, submit(client, third_session, "hello"), "completed")


def test_shutdown_cancels_waiting_worker_and_releases_lease(config, factory):
    with TestClient(create_app(config, factory=factory)) as client:
        rid = submit(client, session(client), "human")
        wait(client, rid, "waiting_human")
    with TestClient(create_app(config, factory=factory)) as client:
        assert client.get(f"/v1/runs/{rid}", headers=AUTH).json()["status"] == "cancelled"
        wait(client, submit(client, session(client), "hello"), "completed")


def test_restart_marks_unfinished_run_and_replays(config, factory):
    config.state_dir.mkdir()
    store = ServiceStore(config.state_dir / "service.sqlite3")
    sid = store.create_session("session-id", "one")
    store.create_run("run-id", sid)
    store.emit("run-id", "delta", {"text": "before crash"})
    store.close()
    with TestClient(create_app(config, factory=factory)) as client:
        run = client.get("/v1/runs/run-id", headers=AUTH).json()
        assert run["status"] == "interrupted"
        events = client.get("/v1/runs/run-id/events", headers=AUTH).text
        assert "before crash" in events and "run_finished" in events
        assert client.get("/v1/sessions/session-id", headers=AUTH).status_code == 200


def test_one_service_process(config, factory):
    first = RunManager(config, factory)
    try:
        with pytest.raises(RuntimeError, match="Only one service"):
            RunManager(config, factory)
    finally:
        first.close()


def test_config_rejects_overlap_and_storage_in_workspace(config):
    root = config.projects["one"]
    for kwargs in ({"token": ""}, {"max_runs": 0}, {"projects": {"p": root}, "state_dir": root / "state"},
                   {"projects": {"a": root, "b": root}}, {"projects": {"a": root, "b": root.parent}},
                   {"projects": {"bad/path": root}}):
        values = {"projects": config.projects, "model_config": config.model_config,
                  "state_dir": config.state_dir, "token": config.token}
        values.update(kwargs)
        with pytest.raises(ValueError):
            ServerConfig(**values)


def test_config_load_paths_and_env(config, tmp_path, monkeypatch):
    path = tmp_path / "service.yaml"
    path.write_text("projects:\n  one: one\nmodel_config: model.yaml\nstate_dir: state\n")
    monkeypatch.setenv("DEEP_AGENT_SERVICE_TOKEN", "service-secret")
    loaded = ServerConfig.load(path)
    assert loaded.projects == {"one": tmp_path / "one"}
    assert loaded.max_runs == 2
    assert "service-secret" not in repr(loaded)
    monkeypatch.delenv("DEEP_AGENT_SERVICE_TOKEN")
    with pytest.raises(ValueError, match="token"):
        ServerConfig.load(path)


def test_initialization_failure_is_generic_and_project_released(config):
    def failing(*args):
        raise RuntimeError("secret-model-key")
    with TestClient(create_app(config, factory=failing)) as client:
        rid = submit(client, session(client), "hello")
        run = wait(client, rid, "failed")
        assert "secret-model-key" not in str(run)
        assert "secret-model-key" not in client.get(f"/v1/runs/{rid}/events", headers=AUTH).text
        wait(client, submit(client, session(client), "hello"), "failed")


def test_restored_interruption_requires_explicit_resume(config):
    def restored(*args):
        runner = FakeRunner()
        runner.state = InterruptState(InterruptKind.WAITING_HUMAN, {"question": "Restored question"})
        return runner
    with TestClient(create_app(config, factory=restored)) as client:
        sid = session(client)
        wait(client, submit(client, sid, "hello"), "failed")
        response = client.post(f"/v1/sessions/{sid}/runs", json={"resume": True}, headers=AUTH)
        assert response.status_code == 202
        rid = response.json()["id"]
        run = wait(client, rid, "waiting_human")
        assert client.post(f"/v1/runs/{rid}/human-input", json={"interrupt_id": run["interrupt_id"], "values": {"text": "answer"}}, headers=AUTH).status_code == 202
        wait(client, rid, "completed")


def test_real_runner_checkpoint_restore_and_http_approval(config):
    from deepagents.backends import StateBackend
    from langchain_core.messages import AIMessage
    from agent.runner import AgentRunner
    from agent.session import SessionStore
    from tests.conftest import scripted_model

    calls = 0
    def real_factory(config, project_id, session_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            messages = [AIMessage(content="", tool_calls=[{
                "id": "write", "name": "write_file",
                "args": {"file_path": "/workspace/restored.txt", "content": "restored"},
            }])]
        else:
            messages = [AIMessage(content="Resumed once")]
        store = SessionStore(config.state_dir / f"{project_id}.sqlite3")
        return AgentRunner(model=scripted_model(messages), backend=StateBackend(),
                           session_store=store, thread_id=session_id)

    with TestClient(create_app(config, factory=real_factory)) as client:
        sid = session(client)
        rid = submit(client, sid, "write a file")
        wait(client, rid, "waiting_confirmation")
        client.post(f"/v1/runs/{rid}/cancel", headers=AUTH)
        wait(client, rid, "cancelled")
    with TestClient(create_app(config, factory=real_factory)) as client:
        response = client.post(f"/v1/sessions/{sid}/runs", json={"resume": True}, headers=AUTH)
        rid = response.json()["id"]
        run = wait(client, rid, "waiting_confirmation")
        assert run["result"]["pending_tool_calls"][0]["toolCallId"] == "write"
        response = client.post(f"/v1/runs/{rid}/approvals", headers=AUTH, json={
            "interrupt_id": run["interrupt_id"],
            "decisions": [{"tool_call_id": "write", "decision": "approve"}],
        })
        assert response.status_code == 202
        assert wait(client, rid, "completed")["result"]["output"] == "Resumed once"
    assert calls == 2


@pytest.mark.integration
def test_disconnect_does_not_cancel_run(config, factory):
    import socket
    from threading import Thread
    import httpx
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(create_app(config, factory=factory), log_level="error", lifespan="on"))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=5, trust_env=False) as client:
            rid = submit(client, session(client), "human")
            waiting = wait(client, rid, "waiting_human")
            with client.stream("GET", f"/v1/runs/{rid}/events", headers=AUTH) as response:
                for line in response.iter_lines():
                    if line == "event: interaction_requested":
                        break
            assert client.get(f"/v1/runs/{rid}", headers=AUTH).json()["status"] == "waiting_human"
            client.post(f"/v1/runs/{rid}/human-input", headers=AUTH, json={
                "interrupt_id": waiting["interrupt_id"], "values": {"text": "after disconnect"},
            })
            run = wait(client, rid, "completed")
            assert run["result"]["output"] == "after disconnect"
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
    assert not thread.is_alive()


def test_event_and_result_redact_service_credentials(config, factory):
    class LeakyRunner(FakeRunner):
        def invoke(self, text, **callbacks):
            callbacks["on_delta"]("assistant", "service-secret")
            return RunResult(status="failed", error="service-secret")
    with TestClient(create_app(config, factory=lambda *args: LeakyRunner())) as client:
        rid = submit(client, session(client), "hello")
        run = wait(client, rid, "failed")
        assert run["result"]["error"] == "[REDACTED]"
        assert "service-secret" not in client.get(f"/v1/runs/{rid}/events", headers=AUTH).text


def test_service_startup_requires_sandbox_even_if_model_config_allows_fallback(config, monkeypatch):
    from agent.config import Settings, SandboxConfig
    from agent.sandbox import SandboxUnavailableError
    import agent.server.runtime as runtime

    config.model_config.write_text("sandbox:\n  allow_unsandboxed: true\n")
    # Settings.load normally also requires model configuration; isolate the sandbox policy here.
    monkeypatch.setattr(Settings, "load", lambda *args, **kwargs: Settings(sandbox=SandboxConfig(allow_unsandboxed=True)))
    calls = []
    def unavailable(sandbox):
        calls.append(sandbox)
        assert sandbox.allow_unsandboxed is False
        raise SandboxUnavailableError("unavailable", kind="not_installed")
    monkeypatch.setattr(runtime, "select_backend", unavailable)
    with pytest.raises(SandboxUnavailableError):
        with TestClient(create_app(config)):
            pytest.fail("Service must not start without a sandbox")
    assert len(calls) == 1
    # Failed startup must release the service process lease.
    with TestClient(create_app(config, factory=lambda *args: FakeRunner())) as client:
        assert client.get("/health").status_code == 200


def _human_runner_factory(config, project_id, session_id, *, restored=False):
    from deepagents.backends import StateBackend
    from langchain_core.messages import AIMessage
    from agent.runner import AgentRunner
    from agent.session import SessionStore
    from tests.conftest import scripted_model

    store = SessionStore(config.state_dir / f"{project_id}.sqlite3")
    messages = [
        AIMessage(content="", tool_calls=[{
            "id": "human-required", "name": "request_human_input",
            "args": {"reason": "Need answer", "question": "Answer?", "fields": [
                {"id": "answer", "type": "text", "required": True},
                {"id": "confirmed", "type": "boolean", "required": True},
                {"id": "note", "type": "textarea", "required": False},
            ]},
        }]), AIMessage(content="Accepted"),
    ]
    return AgentRunner(model=scripted_model(messages[1:] if restored else messages),
                       backend=StateBackend(), session_store=store, thread_id=session_id)


@pytest.mark.parametrize("answer", [None, "", " \t\n", []])
def test_http_real_runner_rejects_required_input_before_admission(config, answer):
    from unittest.mock import patch

    with TestClient(create_app(config, factory=_human_runner_factory)) as client:
        sid = session(client)
        rid = submit(client, sid, "Ask me")
        waiting = wait(client, rid, "waiting_human")
        manager = client.app.state.manager
        runner = manager.active[rid].runner
        checkpoint = runner.prepared.graph.get_state(runner._thread_config()).config
        events = manager.store.events(rid, 0)
        url = f"/v1/runs/{rid}/human-input"
        with patch.object(runner, "_resume", wraps=runner._resume) as resume:
            for values in [{"answer": answer, "confirmed": False},
                           {"confirmed": False, "schema": {"fields": []}}]:
                response = client.post(url, headers=AUTH, json={
                    "interrupt_id": waiting["interrupt_id"], "values": values,
                })
                assert response.status_code == 422, response.text
                resume.assert_not_called()
                current = client.get(f"/v1/runs/{rid}", headers=AUTH).json()
                assert current == waiting
                assert manager.store.events(rid, 0) == events
                assert manager.active[rid].commands.empty()
                assert runner.prepared.graph.get_state(runner._thread_config()).config == checkpoint
            assert client.post(f"/v1/sessions/{sid}/runs", headers=AUTH,
                               json={"text": "other"}).status_code == 409
            response = client.post(url, headers=AUTH, json={
                "interrupt_id": waiting["interrupt_id"],
                "values": {"answer": "fixed", "confirmed": False, "note": " \t\n"},
            })
            assert response.status_code == 202
            assert wait(client, rid, "completed")["result"]["output"] == "Accepted"
            assert resume.call_count == 1
        stream = client.get(f"/v1/runs/{rid}/events", headers=AUTH).text
        assert stream.count("event: interaction_submitted\n") == 1
        assert stream.count("event: run_finished\n") == 1


def test_http_real_human_input_validation_survives_restart(config):
    calls = 0

    def restored_factory(*args):
        nonlocal calls
        calls += 1
        return _human_runner_factory(*args, restored=calls > 1)

    with TestClient(create_app(config, factory=restored_factory)) as client:
        sid = session(client)
        rid = submit(client, sid, "Ask me")
        waiting = wait(client, rid, "waiting_human")
        declared = waiting["result"]["human_input"]
        assert client.post(f"/v1/runs/{rid}/human-input", headers=AUTH, json={
            "interrupt_id": waiting["interrupt_id"], "values": {},
        }).status_code == 422
        # Service shutdown cancels the run while retaining the waiting checkpoint.
    with TestClient(create_app(config, factory=restored_factory)) as client:
        response = client.post(f"/v1/sessions/{sid}/runs", headers=AUTH, json={"resume": True})
        assert response.status_code == 202
        rid = response.json()["id"]
        waiting = wait(client, rid, "waiting_human")
        assert waiting["result"]["human_input"] == declared
        url = f"/v1/runs/{rid}/human-input"
        assert client.post(url, headers=AUTH, json={
            "interrupt_id": waiting["interrupt_id"], "values": {"answer": " \n", "confirmed": False},
        }).status_code == 422
        assert wait(client, rid, "waiting_human")["interrupt_id"] == waiting["interrupt_id"]
        assert client.post(url, headers=AUTH, json={
            "interrupt_id": waiting["interrupt_id"], "values": {"answer": "fixed", "confirmed": False},
        }).status_code == 202
        assert wait(client, rid, "completed")["result"]["output"] == "Accepted"


def test_server_settings_validate_project_instead_of_launch_directory(config, monkeypatch):
    from agent.config import ConfigError, Settings
    import yaml

    config.model_config.write_text(yaml.safe_dump({
        "llm": {"default": "test/model", "models": {"test": {"models": {"model": {}}}}},
        "sandbox": {"workspace": "."},
        "paths": {"config_dir": str(config.model_config.parent / "agent-config")},
    }))
    monkeypatch.chdir(config.model_config.parent)
    # CLI still protects its cwd; only service-owned project selection overrides it.
    with pytest.raises(ConfigError, match="outside workspace"):
        Settings.load(config.model_config)
    settings = config.settings_for("one")
    assert settings.sandbox.workspace == config.projects["one"]
    assert settings.sandbox.allow_unsandboxed is False
    assert settings.state_path == config.state_dir / "projects" / "one" / "sessions.sqlite3"


@pytest.mark.parametrize("unsafe_project", ["one", "two"])
def test_server_settings_still_reject_config_directory_in_any_project(config, unsafe_project, monkeypatch):
    import yaml

    config.model_config.write_text(yaml.safe_dump({
        "llm": {"default": "test/model", "models": {"test": {"models": {"model": {}}}}},
        "paths": {"config_dir": str(config.projects[unsafe_project] / "agent-config")},
    }))
    monkeypatch.chdir(config.model_config.parent)
    with pytest.raises(ValueError, match="outside workspace"):
        config.settings_for("one")


def _events(client, rid):
    import json as _json
    response = client.get(f"/v1/runs/{rid}/events", headers=AUTH)
    events = []
    kind = None
    for line in response.text.splitlines():
        if line.startswith("event: "):
            kind = line[7:]
        elif line.startswith("data: "):
            events.append((kind, _json.loads(line[6:])))
    return events


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
def test_http_cancel_of_silent_model_stream_reaches_cancelled(config, api):
    """C12: real Runner and SSE model; cancel -> cancelling -> cancelled, one run_finished."""
    import json as _json

    from deepagents.backends import StateBackend
    from agent.config import ModelProfile, Settings
    from agent.llm_stream import live_stream_count
    from agent.runner import AgentRunner
    from agent.session import SessionStore
    from tests.model_stream_server import ScriptedSSEServer, Turn

    with ScriptedSSEServer(api, [Turn(text="partial then silence", mode="hang", chunks_before_hang=1),
                                 Turn(text="next run ok")]) as model:
        settings = Settings(llm_profiles=(ModelProfile("local/s", "s", base_url=model.base_url, api=api),),
                            llm_default="local/s")

        def real_factory(config, project_id, session_id):
            store = SessionStore(config.state_dir / f"{project_id}.sqlite3")
            return AgentRunner(settings=settings, backend=StateBackend(), session_store=store, thread_id=session_id)

        with TestClient(create_app(config, factory=real_factory)) as client:
            sid = session(client)
            rid = submit(client, sid, "start")
            connection = model.wait_for_connection(0)
            assert connection.hanging.wait(5)
            started = time.monotonic()
            first = client.post(f"/v1/runs/{rid}/cancel", headers=AUTH)
            assert first.status_code == 202 and first.json()["status"] in {"cancelling", "cancelled"}
            again = client.post(f"/v1/runs/{rid}/cancel", headers=AUTH)
            assert again.status_code in {202, 409}
            run = wait(client, rid, "cancelled")
            assert time.monotonic() - started < 5
            assert run["result"]["status"] == "cancelled" and not run["result"].get("error")
            assert connection.closed.wait(2) and connection.closed_by_client_at is not None
            assert live_stream_count() == 0
            assert client.post(f"/v1/runs/{rid}/cancel", headers=AUTH).status_code == 409
            events = _events(client, rid)
            kinds = [kind if kind != "agent_event" else data["type"] for kind, data in events]
            assert kinds.count("run_finished") == 1 and kinds[-1] == "run_finished"
            assert kinds.count("run_cancelling") == 1
            assert kinds.count("run_cancelled") == 1
            assert "run_failed" not in kinds and "run_completed" not in kinds
            assert kinds.index("run_cancelled") < kinds.index("run_finished")
            store = SessionStore(config.state_dir / "one.sqlite3")
            try:
                assert store.get(sid).last_run_status.value == "aborted"
            finally:
                store.close()
            # C07 over HTTP: the session accepts new work once the old worker drained.
            follow = submit(client, sid, "next")
            assert wait(client, follow, "completed")["result"]["output"] == "next run ok"
            assert "start" in _json.dumps(model.connections[1].body, ensure_ascii=False)
        assert model.open_connections() == []


def _terminal(client, rid, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/v1/runs/{rid}", headers=AUTH).json()
        if run["status"] in TERMINAL:
            return run
        time.sleep(0.005)
    pytest.fail(f"Run did not finish: {run}")


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
def test_http_cancel_racing_completion_reports_the_executed_outcome(config, api):
    """Cancel swept across an approved write_file, the final answer and completion."""
    from deepagents.backends import StateBackend
    from langchain_core.messages import AIMessage, ToolMessage
    from agent.config import ModelProfile, Settings
    from agent.runner import AgentRunner
    from agent.session import SessionStore
    from agent.stream import message_text
    from tests.model_stream_server import ScriptedSSEServer, Turn

    rounds = 12
    with ScriptedSSEServer(api) as model:
        settings = Settings(llm_profiles=(ModelProfile("local/s", "s", base_url=model.base_url, api=api),),
                            llm_default="local/s")
        database = config.state_dir / "one.sqlite3"

        def real_factory(config, project_id, session_id):
            return AgentRunner(settings=settings, backend=StateBackend(),
                               session_store=SessionStore(database), thread_id=session_id)

        def checkpoint(sid):
            store = SessionStore(database)
            reader = AgentRunner(settings=settings, backend=StateBackend(), session_store=store, thread_id=sid)
            try:
                values = reader.prepared.graph.get_state(reader._thread_config()).values
                return values, store.get(sid).last_run_status.value
            finally:
                reader.close()
                store.close()

        outcomes = []
        with TestClient(create_app(config, factory=real_factory)) as client:
            for index in range(rounds):
                # A cancel that beats the approval leaves that interrupt pending,
                # so every round uses its own session.
                sid = session(client)
                call = {"id": f"write-{index}", "name": "write_file",
                        "args": {"file_path": f"/round-{index}.txt", "content": f"round {index}"}}
                model.replace_turns(Turn(tool_calls=[call]), Turn(text=f"done {index} a b c d e", interval=0.003))
                rid = submit(client, sid, f"write round {index}")
                waiting = wait(client, rid, "waiting_confirmation")
                call_id = waiting["result"]["pending_tool_calls"][0]["toolCallId"]
                assert client.post(f"/v1/runs/{rid}/approvals", headers=AUTH, json={
                    "interrupt_id": waiting["interrupt_id"],
                    "decisions": [{"tool_call_id": call_id, "decision": "approve"}],
                }).status_code == 202
                time.sleep(0.012 * index)
                cancel = client.post(f"/v1/runs/{rid}/cancel", headers=AUTH)
                assert cancel.status_code in {202, 409}
                run = _terminal(client, rid)
                kinds = [kind if kind != "agent_event" else data["type"] for kind, data in _events(client, rid)]
                assert kinds.count("run_finished") == 1
                values, catalog = checkpoint(sid)
                messages = values["messages"]
                start = max(i for i, m in enumerate(messages) if message_text(m) == f"write round {index}")
                added = messages[start:]
                tool_results = [m for m in added if isinstance(m, ToolMessage) and m.tool_call_id == call_id]
                answered = any(isinstance(m, AIMessage) and message_text(m).startswith(f"done {index} ")
                               for m in added)
                written = f"/round-{index}.txt" in (values.get("files") or {})
                # A file the tool wrote is always recorded once in the checkpoint.
                assert len(tool_results) == (1 if written else 0)
                if run["status"] == "completed":
                    assert answered and written and catalog == "stop"
                    assert "run_cancelled" not in kinds
                else:
                    assert run["status"] == "cancelled", run
                    assert cancel.status_code == 202 and not run["result"].get("error")
                    assert not answered and catalog == "aborted"
                    assert kinds.count("run_cancelled") == 1
                outcomes.append((run["status"], written))
        assert model.open_connections() == [] and not model.errors
        print("http-race-outcomes", outcomes)
        assert {"completed", "cancelled"} <= {status for status, _ in outcomes}, outcomes


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
def test_http_cancel_after_stop_persisted_keeps_completed(config, api):
    """HTTP cancel accepted after the Runner wrote STOP but before its cleanup ran."""
    from deepagents.backends import StateBackend
    from agent.config import ModelProfile, Settings
    from agent.runner import AgentRunner
    from agent.session import SessionStore, StopReason
    from tests.model_stream_server import ScriptedSSEServer, Turn

    holder = {}
    with ScriptedSSEServer(api, [Turn(text="finished"), Turn(text="after")]) as model:
        settings = Settings(llm_profiles=(ModelProfile("local/s", "s", base_url=model.base_url, api=api),),
                            llm_default="local/s")
        database = config.state_dir / "one.sqlite3"

        def real_factory(config, project_id, session_id):
            runner = AgentRunner(settings=settings, backend=StateBackend(),
                                 session_store=SessionStore(database), thread_id=session_id)
            original_touch = runner._touch_status

            def touch_then_cancel(reason):
                original_touch(reason)
                if reason is StopReason.STOP and "cancel" not in holder:
                    manager = holder["manager"]
                    rid = next(iter(manager.active))
                    holder["cancel"] = manager.cancel(rid)["status"]
                    holder["catalog"] = runner.session_store.get(runner.thread_id).last_run_status

            runner._touch_status = touch_then_cancel
            return runner

        with TestClient(create_app(config, factory=real_factory)) as client:
            holder["manager"] = client.app.state.manager
            sid = session(client)
            rid = submit(client, sid, "answer")
            run = _terminal(client, rid)
            assert holder["cancel"] == "cancelling"
            assert holder["catalog"] is StopReason.STOP
            assert run["status"] == "completed" and run["result"]["output"] == "finished"
            kinds = [kind if kind != "agent_event" else data["type"] for kind, data in _events(client, rid)]
            assert kinds.count("run_finished") == 1
            assert "run_completed" in kinds and "run_cancelled" not in kinds and "run_failed" not in kinds
            store = SessionStore(database)
            try:
                assert store.get(sid).last_run_status is StopReason.STOP
            finally:
                store.close()
            follow = submit(client, sid, "next")
            assert wait(client, follow, "completed")["result"]["output"] == "after"
            import json as _json
            assert "[Previous run status]" not in _json.dumps(model.connections[1].body, ensure_ascii=False)


def test_cancel_does_not_signal_runner_under_manager_lock(config):
    import threading

    observed = {}
    holder = {}

    class LockProbeRunner(FakeRunner):
        def request_cancel(self):
            def probe():
                acquired = holder["manager"].lock.acquire(timeout=1)
                observed["free"] = acquired
                if acquired:
                    holder["manager"].lock.release()
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(2)
            super().request_cancel()

    with TestClient(create_app(config, factory=lambda *_: LockProbeRunner())) as client:
        holder["manager"] = client.app.state.manager
        rid = submit(client, session(client), "block")
        deadline = time.monotonic() + 5
        while holder["manager"].active.get(rid) is None or holder["manager"].active[rid].runner is None:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert client.post(f"/v1/runs/{rid}/cancel", headers=AUTH).status_code == 202
        wait(client, rid, "cancelled")
    assert observed == {"free": True}


def test_cancel_racing_run_start_is_replayed(config):
    """Cancel accepted after the worker published its runner but before begin_run."""
    gate = Event()
    entered = Event()
    runners = []

    class SlowStartRunner(FakeRunner):
        def invoke(self, text, **callbacks):
            entered.set()
            assert gate.wait(5)
            callbacks["on_event"](RunEvent(type="run_started"))
            if self.cancelled.wait(2):
                return RunResult(status="cancelled")
            return RunResult(status="completed", output="raced")

        def request_cancel(self):
            # Like begin_run, the first signal is lost before the run starts.
            if entered.is_set() and not gate.is_set():
                return
            super().request_cancel()

    def factory(*_args):
        runners.append(SlowStartRunner())
        return runners[-1]

    with TestClient(create_app(config, factory=factory)) as client:
        rid = submit(client, session(client), "go")
        assert entered.wait(5)
        assert client.post(f"/v1/runs/{rid}/cancel", headers=AUTH).status_code == 202
        gate.set()
        run = wait(client, rid, "cancelled")
        assert run["result"]["status"] == "cancelled"
        assert runners[0].cancelled.is_set()
        kinds = [kind for kind, _data in _events(client, rid)]
        assert kinds.count("run_finished") == 1
