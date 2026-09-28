"""Session switch semantics under thread ownership locks."""
from __future__ import annotations

from pathlib import Path
import threading
import time

from langchain_core.messages import AIMessage
from deepagents.backends import StateBackend
from langgraph.checkpoint.memory import InMemorySaver
import pytest

from agent.config import Settings
from agent.runner import AgentRunner
from agent.session import SessionStore, StopReason
from agent.session_lock import SessionLockBusyError
from tests.conftest import scripted_model


def _runner(store: SessionStore, thread_id: str | None = None) -> AgentRunner:
    return AgentRunner(
        model=scripted_model([AIMessage(content="done")]),
        backend=StateBackend(),
        session_store=store,
        thread_id=thread_id,
    )


def test_sync_switch_to_busy_target_raises_without_detaching(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sync-busy.sqlite3")
    holder = _runner(store)
    waiter = _runner(store)
    target = holder.thread_id

    with pytest.raises(SessionLockBusyError, match="already open"):
        waiter.switch_session(target)

    # A failed synchronous switch keeps the old session usable.
    assert waiter._runtime.lease is not None
    assert waiter.thread_id is not holder.thread_id
    assert waiter.invoke("still mine").status == "completed"
    holder.close()
    waiter.close()
    store.close()


def test_begin_switch_to_busy_target_detaches(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "detach.sqlite3")
    holder = _runner(store)
    waiter = _runner(store)
    old_thread = waiter.thread_id
    target = holder.thread_id

    result = waiter.begin_session_switch(target)
    assert result.busy
    assert result.target_id == target
    assert result.snapshot is None

    # The old thread is released and refuses operations while waiting.
    assert waiter._runtime.lease is None
    assert waiter._runtime.pending_switch == target
    assert waiter.thread_id == old_thread
    with pytest.raises(RuntimeError, match="owns no session"):
        waiter.invoke("blocked")
    with pytest.raises(RuntimeError, match="owns no session"):
        waiter.set_permission_mode("ask")
    # The old thread is free for another window to take.
    assert store.try_acquire_session(old_thread) is not None
    holder.close()
    waiter.close()
    store.close()


def test_complete_switch_takes_over_after_release(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "complete.sqlite3")
    holder = _runner(store)
    waiter = _runner(store)
    target = holder.thread_id
    assert waiter.begin_session_switch(target).busy

    box: dict[str, object] = {}

    def complete() -> None:
        box["snapshot"] = waiter.complete_session_switch(target, cancelled=lambda: False)

    thread = threading.Thread(target=complete)
    thread.start()
    time.sleep(0.05)
    holder.close()  # releases the target thread
    thread.join(5)
    assert not thread.is_alive()

    snapshot = box["snapshot"]
    assert snapshot is not None
    assert snapshot.info.id == target
    assert waiter._runtime.lease is not None
    assert waiter._runtime.pending_switch is None
    assert waiter.invoke("now mine").status == "completed"
    waiter.close()
    store.close()


def test_cancelled_wait_stays_detached(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "cancel.sqlite3")
    holder = _runner(store)
    waiter = _runner(store)
    old_thread = waiter.thread_id
    target = holder.thread_id

    assert waiter.begin_session_switch(target).busy
    assert waiter.complete_session_switch(target, cancelled=lambda: True) is None

    # Esc must not silently re-take the old thread.
    assert waiter._runtime.lease is None
    assert waiter.thread_id == old_thread
    with pytest.raises(RuntimeError, match="owns no session"):
        waiter.invoke("still detached")
    assert store.try_acquire_session(old_thread) is not None
    holder.close()
    waiter.close()
    store.close()


def test_complete_without_pending_switch_returns_none(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "stale.sqlite3")
    runner = _runner(store)
    other = store.create_session(title="other")
    assert runner.complete_session_switch(other.id, cancelled=lambda: False) is None
    runner.close()
    store.close()


def test_busy_target_is_left_untouched(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "untouched.sqlite3")
    holder = _runner(store)
    waiter = _runner(store)
    target = holder.thread_id
    before = store.get(target)
    assert before is not None

    assert waiter.begin_session_switch(target).busy

    after = store.get(target)
    assert after is not None
    assert after.updated_at == before.updated_at
    assert after.model_id == before.model_id
    assert after.permission_mode == before.permission_mode
    holder.close()
    waiter.close()
    store.close()


def test_cross_window_switching_keeps_exactly_one_owner(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "cross.sqlite3")
    left = _runner(store)
    right = _runner(store)
    left_thread = left.thread_id
    right_thread = right.thread_id

    # right moves away first, then left takes right's thread: no lock gap.
    right.new_session()
    assert right._runtime.lease is not None
    result = left.begin_session_switch(right_thread)
    assert not result.busy
    assert left._runtime.lease is not None
    assert left._runtime.lease.thread_id == right_thread
    assert store.try_acquire_session(left_thread) is not None
    assert store.try_acquire_session(right_thread) is None
    left.close()
    right.close()
    store.close()


def test_switch_to_own_thread_reloads_without_releasing(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "reload.sqlite3")
    runner = _runner(store)
    runner.invoke("remember")
    lease = runner._runtime.lease
    snapshot = runner.switch_session(runner.thread_id)
    assert snapshot is not None
    assert any(block.kind == "user" and block.content == "remember" for block in snapshot.transcript)
    assert runner._runtime.lease is lease
    assert store.try_acquire_session(runner.thread_id) is None
    runner.close()
    store.close()


def test_close_is_idempotent_and_releases_the_lease(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "close.sqlite3")
    runner = _runner(store)
    thread = runner.thread_id
    runner.close()
    runner.close()
    assert runner._runtime.lease is None
    assert store.try_acquire_session(thread) is not None
    with pytest.raises(RuntimeError, match="Runner is closed"):
        runner.invoke("after close")
    store.close()


def test_second_runner_on_shared_in_memory_thread_is_rejected() -> None:
    saver = InMemorySaver()

    def shared_runner() -> AgentRunner:
        return AgentRunner(
            model=scripted_model([AIMessage(content="done")]),
            backend=StateBackend(),
            checkpointer=saver,
            thread_id="shared-thread",
        )

    first = shared_runner()
    with pytest.raises(SessionLockBusyError, match="already open"):
        shared_runner()
    first.close()
    second = shared_runner()
    assert second.invoke("mine now").status == "completed"
    second.close()


def test_detached_runner_recovers_via_new_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "recover.sqlite3")
    holder = _runner(store)
    waiter = _runner(store)
    target = holder.thread_id
    assert waiter.begin_session_switch(target).busy

    info = waiter.new_session(title="fresh start")
    assert waiter._runtime.lease is not None
    assert waiter._runtime.pending_switch is None
    assert waiter.thread_id == info.id
    assert waiter.invoke("recovered").status == "completed"
    assert waiter.complete_session_switch(target, cancelled=lambda: False) is None
    holder.close()
    waiter.close()
    store.close()


def test_waiting_runner_cannot_approve_or_resume(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "waiting-guard.sqlite3")
    holder = _runner(store)
    waiter = _runner(store)
    target = holder.thread_id
    assert waiter.begin_session_switch(target).busy

    with pytest.raises(RuntimeError, match="owns no session"):
        waiter.approve_tool("any-call")
    with pytest.raises(RuntimeError, match="owns no session"):
        waiter.continue_run()
    with pytest.raises(RuntimeError, match="owns no session"):
        waiter.submit_human_input({"text": "x"})
    holder.close()
    waiter.close()
    store.close()


def test_closed_runner_aborts_an_active_wait(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "abort-wait.sqlite3")
    holder = _runner(store)
    waiter = _runner(store)
    target = holder.thread_id
    assert waiter.begin_session_switch(target).busy

    box: dict[str, object] = {}

    def complete() -> None:
        box["result"] = waiter.complete_session_switch(target, cancelled=lambda: False)

    thread = threading.Thread(target=complete)
    thread.start()
    time.sleep(0.05)
    waiter.close()  # must unblock the in-flight wait
    thread.join(5)
    assert not thread.is_alive()
    assert box["result"] is None
    holder.close()
    store.close()


def test_close_timeout_never_releases_the_lease_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "close-timeout.sqlite3")
    entered = threading.Event()
    release = threading.Event()
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="done")]),
        backend=StateBackend(),
        session_store=store,
    )
    thread = runner.thread_id

    def on_event(event: object) -> None:
        if getattr(event, "type", "") == "run_started":
            entered.set()
            assert release.wait(5)

    results: list[object] = []
    worker = threading.Thread(target=lambda: results.append(
        runner.invoke("hold", on_event=on_event)
    ))
    worker.start()
    assert entered.wait(5)

    monkeypatch.setattr("agent.runner.CLOSE_DRAIN_SECONDS", 0.2)
    with pytest.raises(RuntimeError, match="timed out"):
        runner.close()

    # A timed-out close must not hand the thread to another window while the
    # stuck run can still write it.
    assert runner._runtime.lease is not None
    assert store.try_acquire_session(thread) is None

    release.set()
    worker.join(5)
    # close() requested cancel before timing out, so the drained run is aborted.
    assert getattr(results[0], "status", "") == "cancelled"

    # Retrying close once the run drained releases the lease properly.
    runner.close()
    assert runner._runtime.lease is None
    assert store.try_acquire_session(thread) is not None
    store.close()


def test_switch_load_failure_releases_target_and_keeps_old_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "rollback-load.sqlite3")
    holder = _runner(store)
    waiter = _runner(store)
    target = holder.thread_id
    old_thread = waiter.thread_id
    holder.close()  # free the target so the switch reaches the load phase

    def broken_load(session_id: str) -> object:
        raise RuntimeError("load failed")

    monkeypatch.setattr(waiter, "load_session", broken_load)
    with pytest.raises(RuntimeError, match="load failed"):
        waiter.begin_session_switch(target)

    # The target lease was let go; the old session is fully intact.
    assert waiter._runtime.lease is not None
    assert waiter._runtime.lease.thread_id == old_thread
    assert waiter.thread_id == old_thread
    assert waiter._runtime.pending_switch is None
    assert store.try_acquire_session(target) is not None
    assert waiter.invoke("still mine").status == "completed"
    waiter.close()
    store.close()


def test_switch_restore_failure_rolls_back_model_and_keeps_old_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings.from_mapping({
        "llm": {
            "default": "local/alpha",
            "models": {"local": {"models": {
                "alpha": {"model": "qwen3.5-plus"},
                "beta": {"model": "qwen3.5-max"},
            }}},
        },
    })
    store = SessionStore(tmp_path / "rollback-restore.sqlite3")
    holder = AgentRunner(
        model=scripted_model([AIMessage(content="done")]),
        backend=StateBackend(), session_store=store, settings=settings,
    )
    store.touch(holder.thread_id, model_id="local/beta")
    holder.close()  # free the target so the switch reaches the restore phase
    waiter = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), session_store=store, settings=settings,
    )
    target = holder.thread_id
    old_thread = waiter.thread_id

    def broken_apply(plan: object) -> None:
        raise RuntimeError("rebuild failed")

    monkeypatch.setattr(waiter, "_apply_restore_plan", broken_apply)
    with pytest.raises(RuntimeError, match="rebuild failed"):
        waiter.begin_session_switch(target)

    assert waiter._current_model_id == "local/alpha"
    assert waiter.thread_id == old_thread
    assert waiter._runtime.lease is not None
    assert waiter._runtime.lease.thread_id == old_thread
    assert store.try_acquire_session(target) is not None
    assert waiter.invoke("still mine").status == "completed"
    waiter.close()
    store.close()
