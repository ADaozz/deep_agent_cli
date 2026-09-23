"""Regression checks for static assembly, catalog recovery, and slash candidates."""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from deepagents.backends import StateBackend
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from prompt_toolkit.buffer import CompletionState
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent.attachments import ATTACHMENT_META_KEY, AttachmentStore, image_attachment_from_bytes
from agent.cli.app import CliApplication, SlashCompleter
from agent.cli.commands import command_table
from agent.config import ModelProfile, SandboxConfig, Settings
from agent.control import RunController
from agent.factory import AgentSpec, build_agent, create_agent
from agent.middleware.attachments import (
    AttachmentMaterializationMiddleware, reset_attachment_store, set_attachment_store,
)
from agent.permission import PermissionMode
from agent.runner import AgentRunner
from agent.sandbox import ExecutionMode
from agent.session import SessionStore, StopReason
from tests.conftest import scripted_model


def _settings(workspace: Path) -> Settings:
    return Settings(
        llm_profiles=(
            ModelProfile("alpha", "model-a"),
            ModelProfile("beta", "model-b", provider="openai-compatible", input=("text", "image")),
        ),
        llm_default="alpha",
        sandbox=SandboxConfig(workspace=workspace),
    )


@tool
def custom_lookup(query: str) -> str:
    """Return a custom lookup result."""
    return query


def test_spec_rebuild_keeps_static_inputs_and_rereads_project_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "AGENTS.md").write_text("规则一", encoding="utf-8")
    spec = AgentSpec(
        instructions="中文优先", tools=(custom_lookup,), skills=(),
        backend=StateBackend(), sandbox=SandboxConfig(workspace=tmp_path),
    )
    prepared = build_agent(spec, scripted_model([AIMessage(content="ok")]), PermissionMode.ASK,
                           None, RunController(), lambda: False)
    runner = AgentRunner(prepared=prepared, settings=_settings(tmp_path))
    assert "# User Instructions\n中文优先" in prepared.system_prompt
    assert "# Project Instructions\n规则一" in prepared.system_prompt
    assert "custom_lookup" in prepared.exposed_tool_names
    (tmp_path / "AGENTS.md").write_text("规则二", encoding="utf-8")
    runner.switch_model("beta")
    assert "model-b" in runner.prepared.system_prompt
    assert "规则二" in runner.prepared.system_prompt
    assert "规则一" not in runner.prepared.system_prompt
    assert "custom_lookup" in runner.prepared.exposed_tool_names
    assert runner.prepared.spec is spec
    (tmp_path / "AGENTS.md").write_text("规则三", encoding="utf-8")
    monkeypatch.setattr("agent.runner.allow_mode_available", lambda _mode: True)
    runner.set_permission_mode("allow")
    assert "custom_lookup" in runner.prepared.exposed_tool_names
    assert "中文优先" in runner.prepared.system_prompt
    assert "规则三" in runner.prepared.system_prompt
    assert runner.prepared.spec.skills == ()


def test_legacy_catalog_migrates_and_resume_restores_model(tmp_path: Path) -> None:
    db = tmp_path / "sessions.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE session_catalog (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL)")
    conn.execute("INSERT INTO session_catalog VALUES ('legacy', 'old', '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00', 'completed')")
    conn.commit()
    conn.close()
    store = SessionStore(db)
    assert store.get("legacy").last_run_status is StopReason.PENDING
    settings = _settings(tmp_path)
    runner = AgentRunner(model=scripted_model([AIMessage(content="ok")]), backend=StateBackend(),
                         settings=settings, session_store=store)
    thread = runner.thread_id
    runner.switch_model("beta")
    assert store.get(thread).model_id == "beta"
    store.close()
    reopened = SessionStore(db)
    runner2 = AgentRunner(model=scripted_model([AIMessage(content="unused")]), backend=StateBackend(),
                          settings=settings, session_store=reopened)
    runner2.switch_session(thread)
    assert runner2.current_model().id == "beta"
    reopened.close()


def test_resume_permission_fallback_and_stop_status(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "resume.sqlite3")
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend(),
                         settings=_settings(tmp_path), session_store=store)
    thread = runner.thread_id
    assert runner.invoke("hello").status == "completed"
    assert store.get(thread).last_run_status is StopReason.STOP
    store.touch(thread, permission_mode="allow", last_run_status=StopReason.ABORTED)
    second = AgentRunner(model=scripted_model([AIMessage(content="unused")]), backend=StateBackend(),
                         settings=_settings(tmp_path), session_store=store)
    snapshot = second.switch_session(thread)
    assert second.permission_mode() is PermissionMode.ASK
    assert store.get(thread).permission_mode == "ask"
    assert snapshot.info.last_run_status is StopReason.ABORTED
    assert "上一轮" in second._resume_notice
    assert any("unavailable" in notice for notice in snapshot.notices)
    store.close()


def test_resume_restores_saved_allow_when_sandbox_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent.factory.select_backend", lambda _config: SimpleNamespace(
        backend=StateBackend(), mode=ExecutionMode.SANDBOXED, warning="",
    ))
    db = tmp_path / "allow.sqlite3"
    store = SessionStore(db)
    runner = AgentRunner(model=scripted_model([AIMessage(content="unused")]),
                         settings=_settings(tmp_path), session_store=store)
    thread = runner.thread_id
    runner.set_permission_mode("allow")
    assert store.get(thread).permission_mode == "allow"
    store.close()
    reopened = SessionStore(db)
    second = AgentRunner(model=scripted_model([AIMessage(content="unused")]),
                         settings=_settings(tmp_path), session_store=reopened)
    second.switch_session(thread)
    assert second.permission_mode() is PermissionMode.ALLOW
    reopened.close()


def test_resume_missing_model_falls_back_and_updates_catalog(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "missing-model.sqlite3")
    runner = AgentRunner(model=scripted_model([AIMessage(content="unused")]),
                         backend=StateBackend(), settings=_settings(tmp_path), session_store=store)
    thread = runner.thread_id
    store.touch(thread, model_id="removed-model")
    snapshot = runner.switch_session(thread)
    assert runner.current_model().id == "alpha"
    assert store.get(thread).model_id == "alpha"
    assert any("removed-model" in notice for notice in snapshot.notices)
    store.close()


@pytest.mark.parametrize("finish_reason,expected", [
    ("length", StopReason.LENGTH), ("tool_use", StopReason.TOOL_USE),
])
def test_explicit_model_stop_reason_is_catalogued(
    tmp_path: Path, finish_reason: str, expected: StopReason,
) -> None:
    store = SessionStore(tmp_path / f"{finish_reason}.sqlite3")
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="partial", response_metadata={"finish_reason": finish_reason})]),
        backend=StateBackend(), session_store=store,
    )
    assert runner.invoke("answer").status == "completed"
    assert store.get(runner.thread_id).last_run_status is expected
    store.close()


def test_cancel_marks_aborted_before_process_exit(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "cancel.sqlite3")
    runner = AgentRunner(model=scripted_model([AIMessage(content="unused")]),
                         backend=StateBackend(), session_store=store)
    runner._busy = True
    runner.request_cancel()
    assert store.get(runner.thread_id).last_run_status is StopReason.ABORTED
    store.close()


@pytest.mark.parametrize("kind", ["waiting_confirmation", "paused"])
def test_restart_restores_checkpoint_interrupt(tmp_path: Path, kind: str) -> None:
    db = tmp_path / f"{kind}.sqlite3"
    store = SessionStore(db)
    response = AIMessage(content="", tool_calls=[{
        "id": "mail-1", "name": "send_email", "args": {"to": "a@example.com", "subject": "s", "body": "b"},
    }]) if kind == "waiting_confirmation" else AIMessage(content="done")
    runner = AgentRunner(model=scripted_model([response]), backend=StateBackend(), session_store=store)
    if kind == "paused":
        runner.request_pause()
    thread = runner.thread_id
    assert runner.invoke("continue").status == kind
    assert store.get(thread).last_run_status is StopReason.DEFERRED
    store.close()

    restored = SessionStore(db)
    runner2 = AgentRunner(model=scripted_model([AIMessage(content="done")]),
                          backend=StateBackend(), session_store=restored)
    snapshot = runner2.switch_session(thread)
    assert snapshot.interrupt_kind == kind
    assert snapshot.info.last_run_status is StopReason.DEFERRED
    restored.close()


def test_openai_compatible_attachment_is_only_materialized_for_model_request(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "images")
    ref = store.put(image_attachment_from_bytes(b"\x89PNG\r\n\x1a\nimage", filename="image.png"))
    message = HumanMessage(content="describe", additional_kwargs={ATTACHMENT_META_KEY: [ref.to_dict()]})
    request = ModelRequest(model=scripted_model([AIMessage(content="unused")]), messages=[message])
    token = set_attachment_store(store)
    try:
        result = AttachmentMaterializationMiddleware().wrap_model_call(request, lambda item: item)
    finally:
        reset_attachment_store(token)
    assert result.messages[0].content[0]["text"] == "describe"
    assert result.messages[0].content[1]["type"] == "image"
    assert message.content == "describe"
    assert message.additional_kwargs[ATTACHMENT_META_KEY] == [ref.to_dict()]


def test_slash_candidates_are_fuzzy_and_accept_without_submit(tmp_path: Path) -> None:
    completer = SlashCompleter(command_table())
    names = [item.text for item in completer.get_completions(Document("/rsm"), CompleteEvent())]
    assert "/resume" in names
    exact = [item.text for item in completer.get_completions(Document("/quit"), CompleteEvent())]
    assert exact[0] == "/quit"
    runner = AgentRunner(model=scripted_model([AIMessage(content="unused")]), backend=StateBackend(),
                         thread_id="completion")
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.buffer.text = "/rsm"
        app.buffer.cursor_position = len(app.buffer.text)
        completions = list(app.slash_completer.get_completions(app.buffer.document, CompleteEvent()))
        app.buffer.complete_while_typing = lambda: False
        app.buffer.complete_state = CompletionState(app.buffer.document, completions)
        assert app._accept_command_completion(app.buffer)
        assert app.buffer.text == "/resume"
        assert app.buffer.complete_state is None
        assert list(app.slash_completer.get_completions(app.buffer.document, CompleteEvent())) == []


def test_files_state_entrypoint_is_removed() -> None:
    with pytest.raises(TypeError, match="files"):
        create_agent(files={"/workspace/a": "x"})  # type: ignore[call-arg]


def test_tui_enter_accepts_candidate_before_running_command() -> None:
    async def scenario() -> None:
        runner = AgentRunner(model=scripted_model([AIMessage(content="unused")]), backend=StateBackend())
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            task = asyncio.create_task(app.run_async())
            await asyncio.sleep(0.03)
            pipe.send_text("/hlp")
            await asyncio.sleep(0.08)
            pipe.send_text("\r")
            await asyncio.sleep(0.08)
            assert app.buffer.text == "/help"
            assert not any("Keyboard\n" in getattr(block, "content", "") for block in app.state.blocks)
            pipe.send_text("\r")
            await asyncio.sleep(0.08)
            assert any("Keyboard\n" in getattr(block, "content", "") for block in app.state.blocks)
            app.exit()
            await task

    asyncio.run(scenario())
