"""Configured request headers and the runtime ``{session_id}`` placeholder.

These exercise the real OpenAI SDK over a real socket. Header values must stay
bound to the conversation identity through turns, retries, background summary
requests and session switches; concurrent SubAgents must never share one.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import HumanMessage

from agent.bootstrap.config import Settings
from agent.models.client import build_chat_model
from agent.models.profile import ModelProfile
from agent.models.session_summary import generate_session_summary
from agent.runtime.runner import AgentRunner
from agent.sessions.store import SessionStore
from agent.sessions.summary import SessionSummaryRequest
from tests.model_stream_server import ScriptedSSEServer, Turn

pytestmark = pytest.mark.integration
APIS = ["chat_completions", "responses"]


def _profile(api: str, base_url: str) -> ModelProfile:
    return ModelProfile(
        "local/headers", "header-model", api_key="local-test-key", base_url=base_url, api=api,
        headers={
            "User-Agent": "deep-agent-test/1",
            "x-opencode-session": "{session_id}",
        },
    )


def _settings(api: str, base_url: str) -> Settings:
    return Settings(llm_profiles=(_profile(api, base_url),), llm_default="local/headers")


def _session_header(connection) -> str:
    return connection.headers["x-opencode-session"]


@pytest.mark.parametrize("api", APIS)
def test_root_turns_and_new_session_use_the_current_thread_id(api, tmp_path: Path) -> None:
    with ScriptedSSEServer(api, [Turn(text="one"), Turn(text="two"), Turn(text="three")]) as server:
        store = SessionStore(tmp_path / "headers.sqlite3")
        runner = AgentRunner(
            settings=_settings(api, server.base_url), backend=StateBackend(),
            session_store=store, thread_id="session-a",
        )
        try:
            assert runner.invoke("first").output == "one"
            assert runner.invoke("second").output == "two"
            assert [_session_header(item) for item in server.connections] == ["session-a", "session-a"]
            assert server.connections[0].headers["user-agent"] == "deep-agent-test/1"

            new_id = runner.new_session().id
            assert new_id and new_id != "session-a"
            assert runner.invoke("third").output == "three"
            assert _session_header(server.connections[2]) == new_id
        finally:
            runner.close()
            store.close()


@pytest.mark.parametrize("api", APIS)
def test_retries_keep_one_session_header(api) -> None:
    with ScriptedSSEServer(api, [Turn(status=500), Turn(text="ok")]) as server:
        runner = AgentRunner(
            settings=_settings(api, server.base_url), backend=StateBackend(), thread_id="retry-session",
        )
        try:
            assert runner.invoke("hello").output == "ok"
            assert len(server.connections) == 2
            assert {_session_header(item) for item in server.connections} == {"retry-session"}
        finally:
            runner.close()


@pytest.mark.parametrize("api", APIS)
def test_switching_back_rebinds_the_target_session_header(api, tmp_path: Path) -> None:
    with ScriptedSSEServer(api, [Turn(text="first"), Turn(text="back")]) as server:
        store = SessionStore(tmp_path / "switch.sqlite3")
        runner = AgentRunner(
            settings=_settings(api, server.base_url), backend=StateBackend(),
            session_store=store, thread_id="session-a",
        )
        try:
            assert runner.invoke("first").output == "first"
            new_id = runner.new_session().id
            assert new_id != "session-a"
            assert runner.switch_session("session-a").info.id == "session-a"
            assert runner.invoke("back").output == "back"
            assert [_session_header(item) for item in server.connections] == ["session-a", "session-a"]
        finally:
            runner.close()
            store.close()


@pytest.mark.parametrize("api", APIS)
def test_resumed_session_keeps_the_same_session_header(api, tmp_path: Path) -> None:
    with ScriptedSSEServer(api, [Turn(text="one"), Turn(text="two")]) as server:
        store = SessionStore(tmp_path / "resume.sqlite3")
        first = AgentRunner(
            settings=_settings(api, server.base_url), backend=StateBackend(),
            session_store=store, thread_id="resumed-session",
        )
        try:
            assert first.invoke("one").output == "one"
        finally:
            first.close()
        second = AgentRunner(
            settings=_settings(api, server.base_url), backend=StateBackend(),
            session_store=store, thread_id="resumed-session",
        )
        try:
            assert second.invoke("two").output == "two"
            assert [_session_header(item) for item in server.connections] == ["resumed-session"] * 2
        finally:
            second.close()
            store.close()


@pytest.mark.parametrize("api", APIS)
def test_session_summary_uses_the_thread_it_summarizes(api) -> None:
    with ScriptedSSEServer(api) as server:
        profile = _profile(api, server.base_url)
        title = asyncio.run(generate_session_summary(
            SessionSummaryRequest("thread-summary", "fix the login flow", profile.id), profile,
        ))
        assert title
        assert len(server.summary_connections) == 1
        assert _session_header(server.summary_connections[0]) == "thread-summary"
        assert server.summary_connections[0].headers["user-agent"] == "deep-agent-test/1"


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("asynchronous", [False, True])
def test_nonstreaming_sync_and_async_requests_carry_headers(api, asynchronous) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["x-opencode-session"] == "session-sync"
        assert request.headers["user-agent"] == "deep-agent-test/1"
        body = json.loads(request.content)
        assert body["model"] == "header-model"
        if api == "chat_completions":
            return httpx.Response(200, json={
                "id": "chat_1", "object": "chat.completion", "created": 0, "model": "header-model",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "ok"}}],
            })
        return httpx.Response(200, json={
            "id": "resp_1", "object": "response", "created_at": 0, "status": "completed",
            "model": "header-model", "usage": None,
            "output": [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
        })

    model = build_chat_model(
        _profile(api, "https://headers.example/v1"), streaming=False, session_id="session-sync",
    )
    sync_client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    sync_sdk = getattr(model.root_client, "_target", model.root_client)
    async_sdk = getattr(model.root_async_client, "_target", model.root_async_client)
    sync_sdk._client = sync_client
    async_sdk._client = async_client
    try:
        if asynchronous:
            message = asyncio.run(model.ainvoke([HumanMessage(content="hi")]))
        else:
            message = model.invoke([HumanMessage(content="hi")])
        assert message.text == "ok"
        assert len(seen) == 1
    finally:
        sync_client.close()
        asyncio.run(async_client.aclose())


def test_injected_custom_model_is_not_replaced(tmp_path: Path) -> None:
    from tests.conftest import scripted_model

    settings = _settings("chat_completions", "http://127.0.0.1:1/v1")
    custom = scripted_model([])
    runner = AgentRunner(
        model=custom, settings=settings, backend=StateBackend(), thread_id="custom-session",
    )
    try:
        assert runner._chat_model is custom
        runner.new_session()
        assert runner._chat_model is custom
    finally:
        runner.close()


@pytest.mark.parametrize("api", APIS)
def test_concurrent_subagents_receive_distinct_stable_session_ids(api, tmp_path, monkeypatch) -> None:
    import agent.bootstrap.factory as factory
    from agent.bootstrap.factory import create_agent
    from agent.execution.backend import BackendSelection
    from agent.execution.types import ExecutionMode

    calls = [
        {"id": "d1", "name": "start_task", "args": {"description": "child one"}},
        {"id": "d2", "name": "start_task", "args": {"description": "child two"}},
    ]
    child_sessions: list[str | None] = []
    real_build = factory.build_chat_model

    def build_child(profile, **kwargs):
        child_sessions.append(kwargs.get("session_id"))
        return real_build(profile, **kwargs)

    with ScriptedSSEServer(api, [Turn(tool_calls=calls), Turn(text="child one done"),
                                 Turn(text="child two done"), Turn(text="parent done")]) as server:
        settings = _settings(api, server.base_url)
        settings = Settings(
            llm_profiles=settings.llm_profiles, llm_default=settings.llm_default, config_dir=tmp_path,
        )
        monkeypatch.setattr(
            factory, "select_backend", lambda cfg: BackendSelection(StateBackend(), ExecutionMode.SANDBOXED),
        )
        monkeypatch.setattr(factory, "build_chat_model", build_child)
        parent_model = build_chat_model(settings.active_profile, session_id="root-session")
        runner = AgentRunner(
            prepared=create_agent(model=parent_model, settings=settings, skills=[], permission_mode="allow"),
            settings=settings, thread_id="root-session",
        )
        try:
            assert runner.invoke("delegate").output == "parent done"
            assert len(child_sessions) == 2
            assert all(child_sessions)
            assert len(set(child_sessions)) == 2
            assert "root-session" not in child_sessions
            child_connections = server.connections[1:3]
            assert len(child_connections) == 2
            assert {_session_header(item) for item in child_connections} == set(child_sessions)
            assert _session_header(server.connections[0]) == "root-session"
            assert _session_header(server.connections[3]) == "root-session"
        finally:
            runner.close()
            from agent.models.stream_lifecycle import close_model_http_clients
            close_model_http_clients(parent_model)
