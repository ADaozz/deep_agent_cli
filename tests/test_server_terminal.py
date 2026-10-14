"""Deterministic publication races at the terminal-state/event boundary."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from threading import Event, Thread, current_thread, local

import pytest

pytest.importorskip("fastapi")
from fastapi import APIRouter

from agent.runner import RunResult
from agent.server.app import create_app
from agent.server.runtime import RunManager
from agent.server.store import ServiceStore, TERMINAL
from tests.test_server import FakeRunner, config  # Reuse the service fixture.


CANCEL_SCENARIOS = {"cancelled", "cancel_after_completion", "cancel_cleanup_unconfirmed"}


@pytest.mark.parametrize("scenario, status", [
    ("completed", "completed"),
    ("returned_failure", "failed"),
    ("execution_exception", "failed"),
    ("initialization_exception", "failed"),
    ("cancelled", "cancelled"),
    ("cancel_after_completion", "completed"),
    ("cancel_cleanup_unconfirmed", "failed"),
    ("cleanup_exception", "failed"),
    ("worker_start_exception", "failed"),
])
def test_sse_waits_for_atomic_terminal_publication(config, monkeypatch, scenario, status):
    # Gate the producer, then pause its terminal snapshot before event insertion.
    # The same hook exposes the original split update/emit race without relying
    # on scheduling luck: run() used to execute after update() released its lock.
    producer_ready = Event()
    release_producer = Event()
    writer_paused = Event()
    release_writer = Event()
    run_created = Event()
    submit_done = Event()
    first_snapshot = Event()
    allow_competing_snapshot = Event()
    snapshot_attempted = Event()
    snapshot_blocked = Event()
    chunks = []
    reader_errors = []
    submit_errors = []
    created = {}

    class ControlledRunner(FakeRunner):
        def invoke(self, text, **callbacks):
            producer_ready.set()
            assert release_producer.wait(10)
            if scenario == "execution_exception":
                raise RuntimeError("execution failed")
            if scenario == "cancelled":
                return RunResult(status="cancelled")
            if scenario == "cancel_cleanup_unconfirmed":
                return RunResult(status="cancelled", error="model request did not confirm it stopped")
            return RunResult(status="failed" if scenario == "returned_failure" else "completed", output="done")

        def close(self):
            if scenario == "cleanup_exception":
                raise RuntimeError("cleanup failed")
            super().close()

    def factory(*args):
        if scenario == "initialization_exception":
            producer_ready.set()
            assert release_producer.wait(10)
            raise RuntimeError("initialization failed")
        return ControlledRunner()

    manager = RunManager(config, factory)
    session = manager.store.create_session("session", "one")
    store = manager.store
    original_run = store.run
    original_create = store.create_run
    original_events = store.events
    original_lock = store.lock

    def create(*args):
        run = original_create(*args)
        created.update(run)
        run_created.set()
        return run

    def pause_terminal_snapshot(run_id):
        run = original_run(run_id)
        if run["status"] in TERMINAL and not writer_paused.is_set():
            writer_paused.set()
            assert release_writer.wait(10)
        return run

    def observe_events(*args):
        events = original_events(*args)
        if not writer_paused.is_set():
            first_snapshot.set()
        return events

    class ObservedLock:
        def __init__(self):
            self.depth = local()

        def __enter__(self):
            depth = getattr(self.depth, "value", 0)
            if depth == 0 and first_snapshot.is_set() and current_thread().name.startswith("asyncio"):
                # Hold the next snapshot outside the lock until the writer is
                # paused; otherwise it could block before we observe its attempt.
                assert allow_competing_snapshot.wait(10)
            if writer_paused.is_set() and current_thread().name.startswith("asyncio"):
                acquired = original_lock.acquire(blocking=False)
                if not acquired:
                    snapshot_blocked.set()
                snapshot_attempted.set()
                if acquired:
                    self.depth.value = depth + 1
                    return self
            original_lock.acquire()
            self.depth.value = depth + 1
            return self

        def __exit__(self, *args):
            self.depth.value -= 1
            original_lock.release()

    monkeypatch.setattr(store, "create_run", create)
    monkeypatch.setattr(store, "run", pause_terminal_snapshot)
    monkeypatch.setattr(store, "events", observe_events)
    monkeypatch.setattr(store, "lock", ObservedLock())

    if scenario == "worker_start_exception":
        class UnstartableThread:
            def __init__(self, **kwargs):
                pass

            def start(self):
                producer_ready.set()
                assert release_producer.wait(10)
                raise RuntimeError("unable to start")
        monkeypatch.setattr("agent.server.runtime.Thread", UnstartableThread)

    def submit():
        try:
            manager.submit(session["id"], "task", False)
        except Exception as exc:
            submit_errors.append(exc)
        finally:
            submit_done.set()

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    # Exercise the actual SSE generator, including its terminal close condition.
    # Calling the endpoint directly lets us control the snapshot timing without
    # test-client buffering or HTTP polling obscuring the race.
    endpoints = []
    original_add_route = APIRouter.add_api_route

    def capture_route(router, path, endpoint, *args, **kwargs):
        if path == "/runs/{run_id}/events":
            endpoints.append(endpoint)
        return original_add_route(router, path, endpoint, *args, **kwargs)

    monkeypatch.setattr(APIRouter, "add_api_route", capture_route)
    create_app(config, factory=factory)
    endpoint = endpoints[0]

    def subscribe():
        async def consume():
            response = await endpoint(created["id"], ConnectedRequest(), after=0,
                                      last_event_id=None, service=manager)
            async for chunk in response.body_iterator:
                chunks.append(chunk)
        try:
            asyncio.run(consume())
        except Exception as exc:
            reader_errors.append(exc)

    submitter = Thread(target=submit, daemon=True)
    reader = Thread(target=subscribe, daemon=True)
    try:
        submitter.start()
        assert run_created.wait(5)
        assert producer_ready.wait(5)
        reader.start()
        assert first_snapshot.wait(5)
        if scenario in CANCEL_SCENARIOS:
            assert submit_done.wait(5)
            manager.cancel(created["id"])
        release_producer.set()
        assert writer_paused.wait(5)
        allow_competing_snapshot.set()
        assert snapshot_attempted.wait(5)
        # Prove the subscriber has attempted the competing read and is blocked.
        assert snapshot_blocked.is_set()
        # A separate SQLite reader must also see the old committed state, not
        # a terminal state with a missing event (the restart/crash boundary).
        with sqlite3.connect(config.state_dir / "service.sqlite3") as observer:
            observed_status = observer.execute("SELECT status FROM runs WHERE id=?", (created["id"],)).fetchone()[0]
            assert observed_status == ("cancelling" if scenario in CANCEL_SCENARIOS else "running")
            assert observer.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND type='run_finished'",
                                    (created["id"],)).fetchone()[0] == 0
        release_writer.set()
        reader.join(5)
        submitter.join(5)
        assert not reader.is_alive() and not submitter.is_alive()
        assert not reader_errors
        assert len(submit_errors) == (1 if scenario == "worker_start_exception" else 0)
        finished = [json.loads(chunk.split("data: ", 1)[1]) for chunk in chunks
                    if "event: run_finished\n" in chunk]
        assert len(finished) == 1
        assert finished[0] == manager.run(created["id"])
        assert finished[0]["status"] == status
        if scenario == "cancel_cleanup_unconfirmed":
            assert finished[0]["result"]["error"] == "model request did not confirm it stopped"
    finally:
        release_producer.set()
        release_writer.set()
        allow_competing_snapshot.set()
        if submitter.ident is not None:
            submitter.join(5)
        if reader.ident is not None:
            reader.join(5)
        if scenario == "cleanup_exception":
            # Production deliberately retains the reservation and lease on
            # cleanup failure; verify that behavior before releasing test state.
            with pytest.raises(RuntimeError, match="cleanup failed"):
                manager.close()
            store.close()
            manager.lease.close()
        else:
            manager.close()


def test_restart_terminal_publication_is_one_transaction(config, monkeypatch):
    config.state_dir.mkdir()
    path = config.state_dir / "service.sqlite3"
    original = ServiceStore(path)
    session = original.create_session("session", "one")
    original.create_run("run", session)
    original.close()
    writer_paused = Event()
    release_writer = Event()
    restarted = []
    errors = []
    original_run = ServiceStore.run

    def pause_terminal_snapshot(self, run_id):
        run = original_run(self, run_id)
        if run["status"] == "interrupted" and not writer_paused.is_set():
            writer_paused.set()
            assert release_writer.wait(10)
        return run

    monkeypatch.setattr(ServiceStore, "run", pause_terminal_snapshot)

    def restart():
        try:
            restarted.append(ServiceStore(path))
        except Exception as exc:
            errors.append(exc)

    writer = Thread(target=restart, daemon=True)
    try:
        writer.start()
        assert writer_paused.wait(5)
        with sqlite3.connect(path) as observer:
            assert observer.execute("SELECT status FROM runs WHERE id='run'").fetchone()[0] == "running"
            assert observer.execute("SELECT COUNT(*) FROM events WHERE type='run_finished'").fetchone()[0] == 0
        release_writer.set()
        writer.join(5)
        assert not writer.is_alive() and not errors
        store = restarted[0]
        events = store.events("run", 0)
        assert len(events) == 1 and events[0]["type"] == "run_finished"
        assert events[0]["data"] == store.run("run")
        assert events[0]["data"]["status"] == "interrupted"
        store.close()
        # Reopening a completed recovery must not publish a second event.
        store = ServiceStore(path)
        restarted.append(store)
        assert store.events("run", 0) == events
    finally:
        release_writer.set()
        writer.join(5)
        for store in restarted:
            store.close()


@pytest.mark.parametrize("status", sorted(TERMINAL))
def test_terminal_event_insert_failure_rolls_back_status_and_result(tmp_path, status):
    store = ServiceStore(tmp_path / "service.sqlite3", ("secret",))
    try:
        session = store.create_session("session", "one")
        store.create_run("run", session)
        store.update("run", "waiting_human", {"question": "Continue?"}, "interaction")
        before = store.run("run")
        with store.conn:
            store.conn.execute("""
                CREATE TRIGGER reject_finished BEFORE INSERT ON events
                WHEN NEW.type='run_finished'
                BEGIN SELECT RAISE(ABORT, 'injected event insert failure'); END
            """)
        with pytest.raises(sqlite3.IntegrityError, match="injected event insert failure"):
            store.finish("run", status, RunResult(status=status, output="secret"))
        assert store.run("run") == before
        assert store.events("run", 0) == []
        with store.conn:
            store.conn.execute("DROP TRIGGER reject_finished")
        store.finish("run", status, RunResult(status=status, output="secret"))
        finished = store.events("run", 0)
        assert len(finished) == 1
        assert finished[0]["data"] == store.run("run")
        assert finished[0]["data"]["result"]["output"] == "[REDACTED]"
        assert finished[0]["data"]["interrupt_id"] is None
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["human-input", "approvals", "continue"])
def test_submission_event_is_committed_before_worker_is_woken(config, monkeypatch, kind):
    from agent.runner import InterruptKind, InterruptState, RunEvent
    import time

    publication_entered = Event()
    release_publication = Event()
    enqueued = Event()
    published_at_enqueue = []
    resumed = Event()
    errors = []

    class Runner(FakeRunner):
        def invoke(self, text, **callbacks):
            if kind == "continue":
                self.state = InterruptState(InterruptKind.PAUSED, {})
                return RunResult(status="paused")
            return super().invoke(text, **callbacks)

        def processed(self, callbacks):
            resumed.set()
            callbacks["on_event"](RunEvent(type="answer_processed"))
            self.state = None
            return RunResult(status="completed", output="done")

        def submit_human_input(self, values, **callbacks):
            return self.processed(callbacks)

        def decide_tools(self, decisions, **callbacks):
            return self.processed(callbacks)

        def continue_run(self, **callbacks):
            return self.processed(callbacks)

    manager = RunManager(config, lambda *args: Runner())
    responder = None
    try:
        session = manager.store.create_session("causal-session", "one")
        text = {"human-input": "human", "approvals": "approve", "continue": "paused"}[kind]
        run = manager.submit(session["id"], text, False)
        rid = run["id"]
        deadline = time.monotonic() + 5
        while manager.run(rid)["interrupt_id"] is None and time.monotonic() < deadline:
            time.sleep(0.005)
        waiting = manager.run(rid)
        assert waiting["interrupt_id"]
        original_emit = manager.store.emit
        original_put = manager.active[rid].commands.put

        def gated_emit(run_id, event, data):
            if event == "interaction_submitted":
                publication_entered.set()
                assert release_publication.wait(5)
            return original_emit(run_id, event, data)

        def observed_put(command):
            enqueued.set()
            # This check runs at the precise wake-up boundary, not after a sleep.
            published_at_enqueue.append(any(e["type"] == "interaction_submitted" for e in manager.store.events(rid, 0)))
            return original_put(command)

        monkeypatch.setattr(manager.store, "emit", gated_emit)
        monkeypatch.setattr(manager.active[rid].commands, "put", observed_put)
        payload = [{"tool_call_id": tool, "decision": "reject"} for tool in ("a", "b")] if kind == "approvals" else {"text": "answer"}

        def respond():
            try:
                manager.respond(rid, kind, waiting["interrupt_id"], payload)
            except BaseException as exc:
                errors.append(exc)

        responder = Thread(target=respond)
        responder.start()
        assert publication_entered.wait(5)
        assert not enqueued.is_set()
        assert not resumed.is_set()
        release_publication.set()
        responder.join(5)
        assert not responder.is_alive() and not errors
        assert resumed.wait(5)
        assert published_at_enqueue == [True]
        deadline = time.monotonic() + 5
        while manager.run(rid)["status"] not in TERMINAL and time.monotonic() < deadline:
            time.sleep(0.005)
        assert manager.run(rid)["status"] == "completed"
        events = manager.store.events(rid, 0)
        submitted = next(e["id"] for e in events if e["type"] == "interaction_submitted")
        processed = next(e["id"] for e in events if e["type"] == "agent_event" and e["data"]["type"] == "answer_processed")
        finished = next(e["id"] for e in events if e["type"] == "run_finished")
        assert submitted < processed < finished
    finally:
        release_publication.set()
        if responder is not None:
            responder.join(5)
        monkeypatch.undo()
        manager.close()
