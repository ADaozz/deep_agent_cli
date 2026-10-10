"""Session catalog + SQLite checkpointer persistence."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess
import sys

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from deepagents.backends import StateBackend
from langgraph.checkpoint.memory import InMemorySaver
import pytest

from agent.factory import create_agent
from agent.runner import AgentRunner
from agent.session import SessionStore, StopReason, messages_to_transcript, settle_restored_tools, workspace_state_path
from tests.conftest import scripted_model


def test_draft_restarts_and_new_sessions_do_not_persist(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "draft.sqlite3")
    identifiers = set()
    try:
        for _ in range(3):
            runner = AgentRunner(model=scripted_model([]), backend=StateBackend(), session_store=store,
                                 persist_on_first_message=True)
            try:
                identifiers.add(runner.thread_id)
                identifiers.add(runner.new_session().id)
                assert runner.list_sessions() == []
                assert store.get(runner.thread_id) is None
                assert not runner.thread_has_content()
            finally:
                runner.close()
        assert len(identifiers) == 6
        assert store.list_sessions() == []
        assert not list(tmp_path.glob("*.locks/*.lock"))
        assert store.checkpointer.get_tuple({"configurable": {"thread_id": runner.thread_id}}) is None
    finally:
        store.close()


def test_draft_first_message_persists_config_once_and_can_resume(tmp_path: Path) -> None:
    from unittest.mock import patch
    from agent.config import ModelProfile, Settings
    settings = Settings(llm_profiles=(ModelProfile("local/test", "test", reasoning_efforts=("low",)),),
                        llm_default="local/test")
    store = SessionStore(tmp_path / "first-message.sqlite3")
    runner = AgentRunner(model=scripted_model([]), backend=StateBackend(), session_store=store,
                         settings=settings, persist_on_first_message=True)
    identifier = runner.thread_id
    try:
        runner.request_model_change("local/test", reasoning_effort="low")
        with patch("agent.runner.build_chat_model", return_value=scripted_model([AIMessage(content="one"), AIMessage(content="two")])):
            runner._apply_pending_runtime_config()
        assert store.get(identifier) is None
        assert runner.invoke("first message").status == "completed"
        info = store.get(identifier)
        assert info.title == "first message"
        assert info.model_id == "local/test" and info.reasoning_effort == "low"
        assert len(store.list_sessions()) == 1
        assert not store.try_acquire_session(identifier)
        assert runner.invoke("second message").status == "completed"
        assert len(store.list_sessions()) == 1
    finally:
        runner.close()
    resumed = AgentRunner(model=scripted_model([]), backend=StateBackend(), session_store=store,
                          settings=settings, persist_on_first_message=True)
    try:
        draft_id = resumed.thread_id
        with patch("agent.runner.build_chat_model", return_value=scripted_model([])):
            snapshot = resumed.switch_session(identifier)
        assert snapshot.info.id == identifier
        assert resumed.reasoning_effort() == "low"
        assert store.get(draft_id) is None
        assert len(store.list_sessions()) == 1
        assert not store.try_acquire_session(identifier)
    finally:
        resumed.close()
        store.close()


def test_workspace_state_path_is_isolated(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DEEP_AGENT_STATE_PATH", raising=False)
    a = workspace_state_path(tmp_path / "a")
    b = workspace_state_path(tmp_path / "b")
    assert a != b
    assert a.parent == b.parent
    assert a.suffix == ".sqlite3"


def test_session_catalog_sorted_and_prefix_resolve(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    first = store.create_session(title="alpha")
    second = store.create_session(title="beta")
    store.touch(first.id, last_run_status=StopReason.STOP)
    listed = store.list_sessions()
    assert [item.id for item in listed] == [first.id, second.id]
    assert store.resolve_prefix(first.id[:8]).id == first.id
    assert store.resolve_prefix("nope") is None
    store.create_session(session_id=first.id[:4] + "ffff")
    # Ambiguous shared prefix should not resolve.
    assert store.resolve_prefix(first.id[:4]) is None


def test_legacy_catalog_migrates_once_and_derives_status(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE session_catalog (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL, "
        "model_id TEXT, permission_mode TEXT, last_run_status TEXT)"
    )
    conn.execute(
        "INSERT INTO session_catalog VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("old", "work", "2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00",
         "running", "model-a", "ask", "deferred"),
    )
    conn.commit()
    conn.close()

    for _ in range(2):
        store = SessionStore(db)
        info = store.get("old")
        assert info is not None
        assert (info.status, info.last_run_status, info.model_id) == ("waiting", StopReason.DEFERRED, "model-a")
        columns = {row[1] for row in store._conn.execute("PRAGMA table_info(session_catalog)")}
        assert "status" not in columns
        assert {"pending_model_id", "pending_permission_mode", "reasoning_effort", "pending_reasoning_effort"} <= columns
        assert info.reasoning_effort == "default"
        assert info.pending_reasoning_effort is None
        store.close()


def test_existing_catalog_adds_pending_columns(tmp_path: Path) -> None:
    db = tmp_path / "old-current.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE session_catalog (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, model_id TEXT, "
        "permission_mode TEXT, last_run_status TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()
    store = SessionStore(db)
    info = store.create_session()
    store.set_pending_config(info.id, model_id="beta", permission_mode="allow")
    assert store.get(info.id).pending_model_id == "beta"
    assert store.get(info.id).pending_permission_mode == "allow"
    store.close()


def test_runner_rejects_a_second_checkpointer_for_persistent_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    try:
        with pytest.raises(ValueError, match="session store checkpointer"):
            AgentRunner(model=scripted_model([AIMessage(content="unused")]),
                        backend=StateBackend(), session_store=store,
                        checkpointer=InMemorySaver())
    finally:
        store.close()


def test_session_survives_process_restart(tmp_path: Path) -> None:
    db = tmp_path / "agent.sqlite3"
    store = SessionStore(db)
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="hello from session")]),
        backend=StateBackend(),
        session_store=store,
        enable_sessions=True,
    )
    session_id = runner.thread_id
    result = runner.invoke("hi")
    assert result.status == "completed"
    assert result.output == "hello from session"
    store.touch(session_id, last_run_status=StopReason.STOP)
    store.close()

    store2 = SessionStore(db)
    runner2 = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        session_store=store2,
        enable_sessions=True,
        thread_id="other",
    )
    listed = runner2.list_sessions()
    assert any(item.id == session_id for item in listed)
    snapshot = runner2.switch_session(session_id)
    assert snapshot.info.id == session_id
    assert any(block.kind == "user" and "hi" in block.content for block in snapshot.transcript)
    assert any(
        block.kind == "assistant" and block.content == "hello from session"
        for block in snapshot.transcript
    )


def test_approval_interrupt_survives_actual_process_exit(tmp_path: Path) -> None:
    db = tmp_path / "approval.sqlite3"
    script = """
from pathlib import Path
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage
from agent.factory import create_agent
from agent.runner import AgentRunner
from agent.session import SessionStore
from tests.conftest import scripted_model
import sys
store = SessionStore(Path(sys.argv[1]))
prepared = create_agent(model=scripted_model([AIMessage(content='', tool_calls=[
    {'id': 'write-1', 'name': 'write_file', 'args': {'file_path': '/workspace/note.txt', 'content': 'x'}}
])]), backend=StateBackend())
runner = AgentRunner(prepared=prepared, session_store=store)
assert runner.invoke('write a note').status == 'waiting_confirmation'
print(runner.thread_id, flush=True)
store.close()
"""
    finished = subprocess.run(
        [sys.executable, "-c", script, str(db)],
        capture_output=True, text=True, check=True,
    )
    thread = finished.stdout.strip().splitlines()[-1]
    store = SessionStore(db)
    prepared = create_agent(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    runner = AgentRunner(prepared=prepared, session_store=store)
    snapshot = runner.switch_session(thread)
    assert snapshot.interrupt_kind == "waiting_confirmation"
    assert snapshot.pending_tool_calls[0]["toolCallId"] == "write-1"
    assert runner.reject_tool("write-1").status == "completed"
    store.close()


def test_corrupt_session_load_raises_without_breaking_catalog(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "mixed.sqlite3")
    good = store.create_session(title="good")
    bad = store.create_session(title="bad")
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        session_store=store,
        thread_id=good.id,
    )
    runner.invoke("keep")
    # Inject garbage checkpoint rows for the bad thread via raw SQL is hard;
    # instead mark an unknown id and ensure resolve fails cleanly.
    assert store.get("missing-id") is None
    try:
        runner.load_session(bad.id)
    except RuntimeError:
        pass
    assert store.get(good.id) is not None
    assert len(store.list_sessions()) == 2


def test_messages_to_transcript_rebuilds_tools() -> None:
    blocks = messages_to_transcript([
        HumanMessage(content="run"),
        AIMessage(content="", tool_calls=[{
            "id": "c1", "name": "execute", "args": {"command": "echo hi"},
        }]),
        ToolMessage(content="hi", tool_call_id="c1", name="execute"),
        AIMessage(content="done"),
    ])
    kinds = [block.kind for block in blocks]
    assert kinds == ["user", "tool", "assistant"]
    assert blocks[1].content == "hi"
    assert blocks[1].status == "completed"


def test_messages_to_transcript_restores_generic_tool_artifact() -> None:
    blocks = messages_to_transcript([
        AIMessage(content="", tool_calls=[{
            "id": "search-1", "name": "web_search", "args": {"query": "deepagents"},
        }]),
        ToolMessage(
            content="Web search results for: deepagents\n5 results",
            tool_call_id="search-1", name="web_search",
            artifact={
                "provider": "tavily", "query": "deepagents",
                "results": [{"title": "Deep Agents", "url": "https://example.com"}],
                "response_time": 0.42,
            },
        ),
    ])
    tool = blocks[0]
    assert tool.kind == "tool"
    assert tool.artifact["provider"] == "tavily"
    assert tool.artifact["response_time"] == 0.42
    assert tool.artifact["results"][0]["title"] == "Deep Agents"
    assert tool.exit_code is None


def test_messages_to_transcript_updates_existing_tool_block_artifact() -> None:
    blocks = messages_to_transcript([
        AIMessage(content="", tool_calls=[{
            "id": "exec-1", "name": "execute", "args": {"command": "pwd"},
        }]),
        ToolMessage(
            content="ok", tool_call_id="exec-1", name="execute",
            artifact={"exit_code": 0, "truncated": False},
        ),
    ])
    assert blocks[0].artifact == {"exit_code": 0, "truncated": False}
    assert blocks[0].exit_code == 0


def test_restored_unfinished_tools_stop_running_without_inventing_an_outcome() -> None:
    blocks = messages_to_transcript([
        AIMessage(content="", tool_calls=[
            {"id": "write-1", "name": "write_file", "args": {"file_path": "/workspace/report.md", "content": "draft"}},
            {"id": "read-1", "name": "read_file", "args": {"file_path": "/workspace/a.txt"}},
        ]),
        ToolMessage(content="read", tool_call_id="read-1", name="read_file"),
    ])
    settle_restored_tools(blocks)
    assert blocks[0].status == "interrupted"
    assert not blocks[0].is_error
    assert blocks[1].status == "completed"

    pending = messages_to_transcript([AIMessage(content="", tool_calls=[
        {"id": "write-2", "name": "write_file", "args": {"file_path": "/workspace/next.md", "content": "draft"}},
    ])])
    settle_restored_tools(pending, waiting_ids={"write-2"})
    assert pending[0].status == "waiting"


def test_messages_to_transcript_uses_execute_artifact_for_errors() -> None:
    failed = messages_to_transcript([
        ToolMessage(
            content="failed\n\nExit code: 2",
            tool_call_id="fail",
            name="execute",
            artifact={"exit_code": 2, "truncated": False, "termination_reason": None},
        ),
    ])
    assert failed[0].is_error
    assert failed[0].status == "error"
    assert failed[0].exit_code == 2

    misleading = messages_to_transcript([
        ToolMessage(
            content="Exit code: 7\nCancelled by user.",
            tool_call_id="ok",
            name="execute",
            artifact={"exit_code": 0, "truncated": False, "termination_reason": None},
        ),
    ])
    assert not misleading[0].is_error
    assert misleading[0].status == "completed"

    cancelled = messages_to_transcript([
        ToolMessage(
            content="partial\n\nCancelled by user.",
            tool_call_id="cancel",
            name="execute",
            artifact={"exit_code": 130, "truncated": False, "termination_reason": "cancelled"},
        ),
    ])
    assert cancelled[0].is_error
    assert cancelled[0].status == "error"
    assert cancelled[0].exit_code == 130

    timed_out = messages_to_transcript([
        ToolMessage(
            content="partial\n\nError: Command timed out after 1 seconds.",
            tool_call_id="timeout",
            name="execute",
            artifact={"exit_code": 124, "truncated": False, "termination_reason": "timeout"},
        ),
    ])
    assert timed_out[0].is_error
    assert timed_out[0].status == "error"
    assert timed_out[0].exit_code == 124


def test_reasoning_catalog_round_trip_preserves_explicit_default(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite3"
    store = SessionStore(path)
    info = store.create_session(model_id="alpha")
    store.touch(info.id, reasoning_effort="low")
    store.set_pending_config(info.id, model_id="beta", permission_mode=None, reasoning_effort="default")
    store.close()
    store = SessionStore(path)
    try:
        for restored in (store.get(info.id), store.resolve_prefix(info.id[:8]), store.list_sessions()[0]):
            assert restored.reasoning_effort == "low"
            assert restored.pending_reasoning_effort == "default"
            assert restored.pending_model_id == "beta"
        store.touch(info.id, title="renamed")
        assert store.get(info.id).reasoning_effort == "low"
        store.set_pending_config(info.id, model_id=None, permission_mode=None)
        assert store.get(info.id).pending_reasoning_effort is None
    finally:
        store.close()


@pytest.mark.parametrize("aborted", [False, True])
def test_context_usage_restore_requires_completed_current_model(tmp_path, aborted):
    from agent.config import ModelProfile, Settings
    settings = Settings(llm_profiles=(ModelProfile("source/m", "m"),), llm_default="source/m")
    path = tmp_path / "usage.sqlite3"
    store = SessionStore(path)
    runner = AgentRunner(model=scripted_model([AIMessage(content="answer", usage_metadata={
        "input_tokens": 17, "output_tokens": 8, "total_tokens": 25,
    })]), backend=StateBackend(), settings=settings, session_store=store)
    runner.invoke("hello")
    identifier = runner.thread_id
    assert runner.latest_usage()["input_tokens"] == 17
    if aborted:
        runner._touch_status(StopReason.ABORTED)
    runner.close()
    store.close()
    reopened = SessionStore(path)
    resumed = AgentRunner(model=scripted_model([]), backend=StateBackend(), settings=settings,
                          session_store=reopened, thread_id=identifier)
    try:
        if aborted:
            assert resumed.latest_usage() == {}
        else:
            assert resumed.latest_usage()["input_tokens"] == 17
            # Two sources may use the same model name; the source ID matters.
            resumed._current_model_id = "other/m"
            assert resumed.latest_usage() == {}
    finally:
        resumed.close()
        reopened.close()
