from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import sqlite3

import httpx
import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage
from prompt_toolkit.output import DummyOutput

from agent.bootstrap.config import Settings
from agent.cli.app import CliApplication
from agent.models.profile import ModelProfile
from agent.models.client import build_chat_model
from agent.models.session_summary import generate_session_summary
from agent.runtime.runner import AgentRunner
from agent.sessions.store import SessionStore
from agent.sessions.summary import (
    SessionSummaryRequest, SummaryEligibility, redact_summary_input, validate_summary,
)
from tests.conftest import scripted_model
from tests.test_protocol_api import make_model, response_body, sse


TITLE = "修复登录流程中的用户状态同步异常"
OTHER_TITLE = "优化会话列表中的中文摘要显示效果"


def test_summary_migration_preserves_catalog_and_checkpoint(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    store = SessionStore(path)
    info = store.create_session(title="Original title")
    config = {"configurable": {"thread_id": info.id}}
    runner = AgentRunner(model=scripted_model([AIMessage(content="historic answer")]),
                         backend=StateBackend(), session_store=store, thread_id=info.id)
    runner.invoke("historic request")
    runner.close()
    store.touch(info.id, title="Original title")
    info = store.get(info.id)
    checkpoint = store.checkpointer.get_tuple(config).checkpoint
    # The current supported schema is identical except for the nullable column.
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE session_catalog DROP COLUMN summary")
    store = SessionStore(path)
    try:
        assert store.get(info.id) == info
        assert store.get(info.id).summary is None
        assert store.set_summary_if_empty(info.id, TITLE)
        assert not store.set_summary_if_empty(info.id, OTHER_TITLE)
        for saved in (store.get(info.id), store.resolve_prefix(info.id[:8]), store.list_sessions()[0]):
            assert saved.summary == TITLE
            assert saved.title == "Original title"
            assert saved.updated_at == info.updated_at
        assert store.checkpointer.get_tuple(config).checkpoint == checkpoint
    finally:
        store.close()
    reopened = SessionStore(path)
    assert reopened.get(info.id).summary == TITLE
    reopened.close()


def test_only_empty_new_sessions_become_eligible_and_attempt_once():
    policy = SummaryEligibility()
    policy.begin("historical", "existing", has_content=True, has_summary=False)
    assert policy.complete("historical", "answer", "a") is None
    assert not policy.needs_inspection("historical")
    assert not policy.accepts_request("historical")
    policy.begin("saved", "request", has_content=False, has_summary=True)
    assert policy.complete("saved", "answer", "a") is None
    policy.begin("new", "request", has_content=False, has_summary=False)
    # A tool pause does not consume eligibility; the caller completes only after an answer.
    request = policy.complete("new", "result", "a")
    assert request == SessionSummaryRequest("new", "request", "result", "a")
    policy.begin("new", "later task", has_content=True, has_summary=False)
    assert policy.complete("new", "later answer", "b") is None
    assert not policy.accepts_request("new")


@pytest.mark.parametrize("unsafe", [
    "/home/neo/private/config.yaml", r"C:\Users\neo\config.env", "src/private/config.py",
    "https://user:password@example.test/private", "api_key=secret-value", "sk-personal-abc123",
    "密钥：private-value", "Bearer private-token", "password=\"two word secret\"",
    r"src\private\config.py", "OPENAI_API_KEY=private-value",
])
def test_redaction_removes_paths_and_credentials(unsafe):
    assert unsafe not in redact_summary_input(f"修复问题 {unsafe}")
    assert validate_summary(TITLE + " " + unsafe) is None


@pytest.mark.parametrize("invalid", ["", "修复登录", "关于" + TITLE, TITLE + "\n解释", "```" + TITLE,
                                       TITLE + " /private/key", TITLE + "\x1b[0m", "# " + TITLE, "1. " + TITLE])
def test_invalid_titles_are_not_saved(invalid):
    assert validate_summary(invalid) is None


def test_titles_keep_technical_names_and_filter_known_secrets():
    title = "修复 React 应用登录流程中的状态同步异常"
    assert validate_summary(title) == title
    assert validate_summary(TITLE + " configured-secret", ("configured-secret",)) is None
    assert "configured-secret" not in redact_summary_input("key: configured-secret", ("configured-secret",))


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
def test_title_generator_uses_isolated_nonstreaming_protocol_request(api, monkeypatch):
    requests = []
    models = []
    def serve(request):
        body = json.loads(request.content)
        requests.append(body)
        if api == "chat_completions":
            return httpx.Response(200, json={
                "id": "title", "object": "chat.completion", "created": 0, "model": "arbitrary-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": TITLE}, "finish_reason": "stop"}],
            })
        body = response_body(None)
        body["output"] = [{"type": "message", "id": "msg_title", "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": TITLE, "annotations": []}]}]
        return httpx.Response(200, json=body)

    def builder(profile, **kwargs):
        assert profile.max_retries == 0
        assert kwargs["streaming"] is False
        assert kwargs["timeout"] <= 10
        model = build_chat_model(profile, **kwargs)
        model.http_client._transport = httpx.MockTransport(serve)
        model.http_async_client._transport = httpx.MockTransport(serve)
        models.append(model)
        return model

    monkeypatch.setattr("agent.models.session_summary.build_chat_model", builder)
    profile = ModelProfile("s/arbitrary", "arbitrary-model", api=api, api_key="configured-secret")
    request = SessionSummaryRequest("session", "修复 React 登录 /private/config.py configured-secret", "已完成修复", profile.id)
    assert asyncio.run(generate_session_summary(request, profile)) == TITLE
    assert len(requests) == 1
    payload = json.dumps(requests[0], ensure_ascii=False)
    assert "configured-secret" not in payload and "/private/config.py" not in payload
    assert not requests[0].get("stream") and "tools" not in requests[0]
    assert all(model.http_client.is_closed and model.http_async_client.is_closed for model in models)


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
def test_first_completed_turn_emits_one_bound_summary_request(api, tmp_path):
    def serve(request):
        if api == "chat_completions":
            return sse([{"id": "answer", "object": "chat.completion.chunk", "created": 0, "model": "arbitrary-model",
                         "choices": [{"index": 0, "delta": {"content": "已修复登录异常"}, "finish_reason": "stop"}]}], done=True)
        return sse([
            {"type": "response.output_text.delta", "sequence_number": 0, "output_index": 0,
             "content_index": 0, "item_id": "answer", "delta": "已修复登录异常"},
            {"type": "response.completed", "sequence_number": 1, "response": response_body(None)},
        ])
    profile = ModelProfile("s/arbitrary", "arbitrary-model", api=api, api_key="configured-secret")
    model = make_model(api, serve, profile=profile)
    store = SessionStore(tmp_path / "sessions.sqlite3")
    runner = AgentRunner(model=model, backend=StateBackend(), session_store=store,
                         settings=Settings(llm_profiles=(profile,), llm_default=profile.id))
    events = []
    try:
        assert runner.invoke("修复登录 /private/config.env configured-secret", on_event=events.append).status == "completed"
        summaries = [e.result for e in events if e.type == "session_summary_requested"]
        assert len(summaries) == 1
        assert summaries[0].thread_id == runner.thread_id
        assert summaries[0].model_id == profile.id
        assert "/private" not in summaries[0].user_request and "configured-secret" not in summaries[0].user_request
        assert summaries[0].agent_result == "已修复登录异常"
        runner.invoke("继续优化", on_event=events.append)
        assert len([e for e in events if e.type == "session_summary_requested"]) == 1
        assert store.get(runner.thread_id).summary is None  # No CLI subscriber, no extra model request.
        identifier = runner.thread_id
        runner.close()
        historical = AgentRunner(
            model=make_model(api, serve, profile=profile), backend=StateBackend(), session_store=store,
            settings=Settings(llm_profiles=(profile,), llm_default=profile.id), thread_id=identifier,
        )
        restored_events = []
        try:
            historical.invoke("恢复后的新请求", on_event=restored_events.append)
            assert not [e for e in restored_events if e.type == "session_summary_requested"]
        finally:
            historical.close()
    finally:
        runner.close()
        store.close()


def make_app(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    runner = AgentRunner(model=scripted_model([AIMessage(content="result")]), backend=StateBackend(),
                         session_store=store, settings=Settings())
    app = CliApplication(runner, output=DummyOutput())
    return store, runner, app


def test_background_summary_stays_bound_across_session_switch_and_persists(tmp_path):
    async def scenario():
        store, runner, app = make_app(tmp_path)
        first = runner.thread_id
        ready, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def generate(request):
            calls.append(request)
            ready.set()
            await release.wait()
            return TITLE
        app.session_summaries.generate = generate
        req = SessionSummaryRequest(first, "request", "result", "default")
        try:
            app.session_summaries.request(req)
            app.session_summaries.request(req)
            await ready.wait()
            app.sessions.new_session()
            second = runner.thread_id
            assert app.session_summaries.label(second) == second
            release.set()
            await asyncio.gather(*(task for task, _ in app.lifecycle._tracked))
            assert len(calls) == 1
            assert store.get(first).summary == TITLE
            assert app.session_summaries.label(first) == TITLE
            assert app.session_summaries.label(second) == second
            # A stale snapshot must not overwrite a completed cache entry.
            app.session_summaries.remember(replace(store.get(first), summary=None))
            assert app.session_summaries.label(first) == TITLE
        finally:
            await app.lifecycle.shutdown()
            runner.close()
            store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("result", [None, "标题：" + TITLE, TITLE + " /private/path"])
def test_bad_background_result_falls_back_without_affecting_run(tmp_path, result):
    async def scenario():
        store, runner, app = make_app(tmp_path)
        async def generate(_):
            return result
        app.session_summaries.generate = generate
        try:
            app.session_summaries.request(SessionSummaryRequest(runner.thread_id, "request", "answer", "default"))
            await asyncio.gather(*(task for task, _ in app.lifecycle._tracked))
            assert store.get(runner.thread_id).summary is None
            assert app.session_summaries.label(runner.thread_id) == runner.thread_id
            assert app.state.status == "Ready"
        finally:
            await app.lifecycle.shutdown()
            runner.close()
            store.close()
    asyncio.run(scenario())


def test_exit_cancels_title_request_and_does_not_save(tmp_path):
    async def scenario():
        store, runner, app = make_app(tmp_path)
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def generate(_):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        app.session_summaries.generate = generate
        try:
            app.session_summaries.request(SessionSummaryRequest(runner.thread_id, "request", "answer", "default"))
            await started.wait()
            await app.lifecycle.shutdown(task_timeout=1)
            assert cancelled.is_set()
            assert store.get(runner.thread_id).summary is None
        finally:
            runner.close()
            store.close()
    asyncio.run(scenario())


def test_resume_uses_summary_or_full_id_and_preserves_identity(tmp_path):
    async def scenario():
        store, runner, app = make_app(tmp_path)
        first = store.get(runner.thread_id)
        second = store.create_session(title="Raw user prompt /private/path")
        third = store.create_session()
        store.set_summary_if_empty(first.id, TITLE)
        store.set_summary_if_empty(third.id, TITLE)
        app.sessions._sessions_with_content = lambda **_: store.list_sessions()
        try:
            await app.sessions.resume()
            options = app.dialogs.current.fields[0]["options"]
            labels = {o["value"]: o["label"] for o in options}
            assert labels == {first.id: TITLE, second.id: second.id, third.id: TITLE}
            assert first.id != third.id
            app.session_summaries.remember(store.get(first.id))
            app._width = lambda: 100
            footer = "".join(text for _, text in app._footer_text())
            assert TITLE in footer
            # Redraw must use the label cache, not perform another SQLite read.
            store.get = lambda *_: (_ for _ in ()).throw(AssertionError("uncached draw"))
            app._footer_text()
        finally:
            await app.lifecycle.shutdown()
            runner.close()
            store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "error"])
def test_summary_failure_does_not_retry_or_change_coding_state(tmp_path, monkeypatch, failure):
    async def scenario():
        store, runner, app = make_app(tmp_path)
        calls = []
        async def generate(request):
            calls.append(request)
            if failure == "error":
                raise RuntimeError("title service unavailable")
            await asyncio.Event().wait()
        monkeypatch.setattr("agent.cli.session_summaries.SUMMARY_TIMEOUT_SECONDS", 0.01)
        app.session_summaries.generate = generate
        request = SessionSummaryRequest(runner.thread_id, "request", "answer", "default")
        try:
            app.session_summaries.request(request)
            await asyncio.gather(*(task for task, _ in app.lifecycle._tracked))
            app.session_summaries.request(request)
            assert len(calls) == 1
            assert app.state.status == "Ready"
            assert store.get(runner.thread_id).summary is None
        finally:
            await app.lifecycle.shutdown()
            runner.close()
            store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_model_title_timeout_and_cancellation_close_owned_clients(monkeypatch, cancel):
    async def scenario():
        started = asyncio.Event()
        class Client:
            def __init__(self):
                self.closed = False
            def close(self):
                self.closed = True
            async def aclose(self):
                self.closed = True
        class Model:
            http_client = Client()
            http_async_client = Client()
            async def ainvoke(self, *args, **kwargs):
                started.set()
                await asyncio.Event().wait()
        model = Model()
        monkeypatch.setattr("agent.models.session_summary.build_chat_model", lambda *a, **kw: model)
        monkeypatch.setattr("agent.models.session_summary.SUMMARY_TIMEOUT_SECONDS", 0.01 if not cancel else 10)
        task = asyncio.create_task(generate_session_summary(
            SessionSummaryRequest("id", "request", "answer", "default"), ModelProfile("default", "test"),
        ))
        await started.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
        assert model.http_client.closed and model.http_async_client.closed
    asyncio.run(scenario())


def test_queued_turn_does_not_replace_first_request_or_first_result(tmp_path):
    replies = iter(("first answer", "queued answer"))
    def serve(request):
        return sse([{
            "id": "answer", "object": "chat.completion.chunk", "created": 0, "model": "arbitrary-model",
            "choices": [{"index": 0, "delta": {"content": next(replies)}, "finish_reason": "stop"}],
        }], done=True)
    profile = ModelProfile("s/arbitrary", "arbitrary-model", api="chat_completions")
    store = SessionStore(tmp_path / "queued.sqlite3")
    runner = AgentRunner(model=make_model(profile.api, serve, profile=profile), backend=StateBackend(),
                         session_store=store, settings=Settings(llm_profiles=(profile,), llm_default=profile.id))
    events = []
    def observe(event):
        events.append(event)
        if event.type == "run_started":
            runner.follow_up("queued goal")
    try:
        result = runner.invoke("initial goal", on_event=observe)
        assert result.output == "queued answer"
        summaries = [event.result for event in events if event.type == "session_summary_requested"]
        assert len(summaries) == 1
        assert summaries[0].user_request == "initial goal"
        assert summaries[0].agent_result == "first answer"
    finally:
        runner.close()
        store.close()


def test_delayed_summary_event_is_not_dropped_by_a_new_ui_epoch(tmp_path):
    from agent.runtime.contracts import RunEvent
    async def scenario():
        store, runner, app = make_app(tmp_path)
        first = runner.thread_id
        async def generate(_):
            return TITLE
        app.session_summaries.generate = generate
        try:
            app.sessions.new_session()
            second = runner.thread_id
            app.run_view.epoch = 2
            app._apply_run_event(RunEvent(
                type="session_summary_requested",
                result=SessionSummaryRequest(first, "initial goal", "first result", "default"),
            ), 1)
            await asyncio.gather(*(task for task, _ in app.lifecycle._tracked))
            assert store.get(first).summary == TITLE
            assert app.session_summaries.label(second) == second
            assert app.state.status == "Ready"
        finally:
            await app.lifecycle.shutdown()
            runner.close()
            store.close()
    asyncio.run(scenario())


def test_summary_without_a_running_ui_loop_is_optional(tmp_path):
    store, runner, app = make_app(tmp_path)
    try:
        app.session_summaries.request(SessionSummaryRequest(runner.thread_id, "request", "answer", "default"))
        assert not app.lifecycle._tracked
        assert app.session_summaries.label(runner.thread_id) == runner.thread_id
    finally:
        app._io_executor.shutdown(wait=False)
        runner.close()
        store.close()
