"""Exit request and UI shutdown stay apart from Runner.close()."""
from __future__ import annotations

import asyncio
import threading
import warnings
from types import SimpleNamespace

import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent.cli.app import CliApplication
from agent.cli.lifecycle import ExitPhase, TaskStop
from agent.runner import AgentRunner, RunResult
from agent.session import SessionStore
from tests.conftest import scripted_model


def _runner(store: SessionStore | None = None) -> AgentRunner:
    return AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        session_store=store,
        thread_id=None if store is not None else "lifecycle",
    )


def test_repeated_exit_cancels_a_run_once_and_keeps_the_lease(tmp_path) -> None:
    store = SessionStore(tmp_path / "lease.sqlite3")
    runner = _runner(store)
    cancels: list[int] = []
    runner.request_cancel = lambda: cancels.append(1)  # type: ignore[method-assign]
    pipe = create_pipe_input()
    pipe.__enter__()
    try:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.application.exit = lambda *args, **kwargs: None  # type: ignore[method-assign]
        app.state.running = True
        app.exit()
        app.exit()
        assert app.lifecycle.phase is ExitPhase.EXIT_REQUESTED
        assert cancels == [1]
        assert runner._runtime.lease is not None
        assert runner._closed is False
    finally:
        pipe.__exit__(None, None, None)
        runner.close()
        store.close()


def test_shutdown_cancels_ui_tasks_and_leaves_the_run(tmp_path) -> None:
    store = SessionStore(tmp_path / "tasks.sqlite3")
    runner = _runner(store)
    pipe = create_pipe_input()
    pipe.__enter__()

    async def scenario() -> None:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.application.exit = lambda *args, **kwargs: None  # type: ignore[method-assign]
        cancelled = asyncio.Event()

        async def helper() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def sticky() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(30)

        async def agent_run() -> None:
            await asyncio.sleep(30)

        helper_task = app.lifecycle.spawn(helper(), stop=TaskStop.CANCEL, name="clipboard-copy")
        sticky_task = app.lifecycle.spawn(sticky(), stop=TaskStop.CANCEL, name="clipboard-paste")
        run_task = app.lifecycle.spawn(agent_run(), stop=TaskStop.OBSERVE, name="agent-run")
        await asyncio.sleep(0)
        app.state.running = True
        app.exit()
        await app.lifecycle.shutdown(task_timeout=0.5)
        assert cancelled.is_set()
        assert helper_task.cancelled()
        assert not sticky_task.done()
        assert "clipboard-paste" in app.lifecycle.unfinished
        assert not run_task.cancelled()
        assert "agent-run" in app.lifecycle.unfinished
        assert app.lifecycle.phase is ExitPhase.STOPPED
        assert runner._runtime.lease is not None
        await app.lifecycle.shutdown()
        assert app.lifecycle.phase is ExitPhase.STOPPED
        run_task.cancel()
        sticky_task.cancel()
        await asyncio.gather(run_task, sticky_task, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        pipe.__exit__(None, None, None)
        runner.close()
        store.close()


def test_shutdown_stops_pending_mcp_connections(tmp_path) -> None:
    runner = _runner()
    calls: list[str] = []

    class Manager:
        def cancel_pending_connections(self) -> None:
            calls.append("cancel")

        def close(self) -> None:
            return None

    pipe = create_pipe_input()
    pipe.__enter__()

    async def scenario() -> None:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        runner.mcp = Manager()  # type: ignore[attr-defined]
        app.application.exit = lambda *args, **kwargs: None  # type: ignore[method-assign]
        app.exit()
        await app.lifecycle.shutdown()
        assert calls == ["cancel"]
        with pytest.raises(RuntimeError):
            app._io_executor.submit(lambda: None)

    try:
        asyncio.run(scenario())
    finally:
        pipe.__exit__(None, None, None)
        runner.close()


def test_run_async_exit_keeps_the_lease_until_runner_close(tmp_path) -> None:
    store = SessionStore(tmp_path / "tty.sqlite3")
    runner = _runner(store)
    pipe_cm = create_pipe_input()
    pipe = pipe_cm.__enter__()

    async def scenario() -> None:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        task = asyncio.create_task(app.run_async())
        for _ in range(100):
            if app.application.is_running:
                break
            await asyncio.sleep(0.01)
        else:
            error = task.exception() if task.done() else None
            raise AssertionError(f"TUI did not start: {error}")
        pipe.send_text("\x03\x03")
        await asyncio.wait_for(task, 3)
        assert app.lifecycle.phase is ExitPhase.STOPPED
        assert runner._runtime.lease is not None
        assert runner._closed is False

    try:
        asyncio.run(scenario())
        assert runner._runtime.lease is not None
        runner.close()
        assert runner._runtime.lease is None
    finally:
        pipe_cm.__exit__(None, None, None)
        if not runner._closed:
            runner.close()
        store.close()


def test_exit_during_a_run_cancels_without_releasing_the_lease(tmp_path) -> None:
    store = SessionStore(tmp_path / "run.sqlite3")
    runner = _runner(store)
    started, release = threading.Event(), threading.Event()
    cancels: list[int] = []

    def blocked(text: str, **kwargs: object) -> RunResult:
        started.set()
        release.wait(3)
        return RunResult(status="cancelled")

    runner.invoke_with_attachment_refs = blocked  # type: ignore[method-assign]
    runner.request_cancel = lambda: cancels.append(1) or release.set()  # type: ignore[method-assign]
    pipe_cm = create_pipe_input()
    pipe = pipe_cm.__enter__()

    async def scenario() -> None:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        task = asyncio.create_task(app.run_async())
        for _ in range(100):
            if app.application.is_running:
                break
            await asyncio.sleep(0.01)
        pipe.send_text("work\r")
        assert await asyncio.to_thread(started.wait, 2)
        pipe.send_text("\x03\x03")
        await asyncio.wait_for(task, 3)
        assert cancels == [1]
        assert runner._runtime.lease is not None
        assert runner._closed is False

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        pipe_cm.__exit__(None, None, None)
        runner.close()
        store.close()


def test_shutdown_does_not_claim_a_running_executor_call_stopped(tmp_path) -> None:
    store = SessionStore(tmp_path / "exec.sqlite3")
    runner = _runner(store)
    started, release = threading.Event(), threading.Event()

    def work() -> str:
        started.set()
        release.wait(3)
        return "finished-after-shutdown"

    pipe = create_pipe_input()
    pipe.__enter__()
    future_box: list[object] = []

    async def scenario() -> None:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.application.exit = lambda *args, **kwargs: None  # type: ignore[method-assign]
        future = app._io_executor.submit(work)
        future_box.append(future)
        assert await asyncio.to_thread(started.wait, 2)
        app.exit()
        await app.lifecycle.shutdown(task_timeout=0.05)
        assert not future.done()
        assert runner._runtime.lease is not None

    try:
        asyncio.run(scenario())
        future = future_box[0]
        release.set()
        assert future.result(2) == "finished-after-shutdown"  # type: ignore[attr-defined]
        assert runner._runtime.lease is not None
    finally:
        release.set()
        pipe.__exit__(None, None, None)
        runner.close()
        store.close()


def test_mcp_connect_exit_clears_busy_once(tmp_path) -> None:
    runner = _runner()
    seen: list[str] = []

    class Manager:
        def __init__(self) -> None:
            self.event = threading.Event()
            self.calls: list[str] = []

        def cancel_pending_connections(self) -> None:
            self.calls.append("cancel")
            self.event.set()

        def close(self) -> None:
            return None

    manager = Manager()
    pipe = create_pipe_input()
    pipe.__enter__()

    async def scenario() -> None:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        runner.mcp = manager  # type: ignore[attr-defined]
        app.application.exit = lambda *args, **kwargs: None  # type: ignore[method-assign]
        operation_id = app.mcp_controller.try_begin()
        assert operation_id is not None

        async def connect() -> None:
            try:
                await asyncio.to_thread(manager.event.wait, 2)
                seen.append("finished")
            finally:
                app.mcp_controller.finish(operation_id)

        app.mcp_controller.operation = app.lifecycle.spawn(
            connect(), stop=TaskStop.OBSERVE, name="mcp-operation",
        )
        await asyncio.sleep(0)
        app.exit()
        app.exit()
        await app.lifecycle.shutdown(task_timeout=1)
        await app.lifecycle.shutdown()
        assert seen == ["finished"]
        assert manager.calls == ["cancel"]
        assert app.mcp_controller.busy is False
        assert runner._closed is False

    try:
        asyncio.run(scenario())
    finally:
        manager.event.set()
        pipe.__exit__(None, None, None)
        runner.close()


def test_session_wait_exit_ignores_a_late_switch_result(tmp_path) -> None:
    store = SessionStore(tmp_path / "late-wait.sqlite3")
    holder = _runner(store)
    holder.invoke("held")
    target = holder.thread_id
    waiter = _runner(store)
    started, release = threading.Event(), threading.Event()

    def late(session_id: str, *, cancelled: object = None) -> object:
        started.set()
        release.wait(3)
        return SimpleNamespace(
            transcript=[],
            model_metrics_history=[],
            notices=[],
            info=SimpleNamespace(id=session_id, last_run_status=SimpleNamespace(value="stop")),
        )

    waiter.complete_session_switch = late  # type: ignore[method-assign]
    pipe = create_pipe_input()
    pipe.__enter__()

    async def scenario() -> None:
        app = CliApplication(waiter, input=pipe, output=DummyOutput())
        app.application.exit = lambda *args, **kwargs: None  # type: ignore[method-assign]
        await app.resume_session(target[:8])
        assert app.sessions.waiting
        assert await asyncio.to_thread(started.wait, 2)
        app.exit()
        release.set()
        task = app.sessions.wait_task
        assert task is not None
        await asyncio.wait_for(task, 2)
        assert not app.sessions.waiting
        assert app.interaction is None
        assert all("Resumed session" not in getattr(block, "content", "") for block in app.state.blocks)
        assert store.try_acquire_session(target) is None

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        pipe.__exit__(None, None, None)
        holder.close()
        waiter.close()
        store.close()


def test_session_wait_escape_reopens_the_picker(tmp_path) -> None:
    store = SessionStore(tmp_path / "esc-wait.sqlite3")
    holder = _runner(store)
    holder.invoke("held")
    target = holder.thread_id
    waiter = _runner(store)
    pipe = create_pipe_input()
    pipe.__enter__()

    async def scenario() -> None:
        app = CliApplication(waiter, input=pipe, output=DummyOutput())
        app.application.exit = lambda *args, **kwargs: None  # type: ignore[method-assign]
        await app.resume_session(target[:8])
        assert app.sessions.waiting
        app.sessions.cancel_wait()
        await asyncio.wait_for(app.sessions.wait_task, 2)
        assert not app.sessions.waiting
        assert app.lifecycle.phase is ExitPhase.ACTIVE
        assert app.interaction is not None and app.interaction.kind == "resume"
        assert store.try_acquire_session(target) is None

    try:
        asyncio.run(scenario())
    finally:
        pipe.__exit__(None, None, None)
        holder.close()
        waiter.close()
        store.close()


def test_session_wait_exit_cancels_the_wait_and_keeps_the_holder(tmp_path) -> None:
    store = SessionStore(tmp_path / "wait.sqlite3")
    holder = _runner(store)
    holder.invoke("held")
    target = holder.thread_id
    waiter = _runner(store)
    pipe = create_pipe_input()
    pipe.__enter__()

    async def scenario() -> None:
        app = CliApplication(waiter, input=pipe, output=DummyOutput())
        app.application.exit = lambda *args, **kwargs: None  # type: ignore[method-assign]
        await app.resume_session(target[:8])
        assert app.sessions.waiting
        app.exit()
        await app.lifecycle.shutdown(task_timeout=2)
        assert not app.sessions.waiting
        assert app.interaction is None
        assert waiter._runtime.lease is None
        assert store.try_acquire_session(target) is None

    try:
        asyncio.run(scenario())
    finally:
        pipe.__exit__(None, None, None)
        holder.close()
        waiter.close()
        store.close()


def test_one_cleanup_failure_does_not_skip_the_rest(tmp_path) -> None:
    store = SessionStore(tmp_path / "fail.sqlite3")
    runner = _runner(store)
    helper_cancelled = asyncio.Event()
    pipe = create_pipe_input()
    pipe.__enter__()

    async def scenario() -> None:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.application.exit = lambda *args, **kwargs: None  # type: ignore[method-assign]

        async def boom() -> None:
            raise RuntimeError("clipboard failed")

        async def helper() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                helper_cancelled.set()
                raise

        app.lifecycle.spawn(boom(), stop=TaskStop.CANCEL, name="clipboard-copy")
        app.lifecycle.spawn(helper(), stop=TaskStop.CANCEL, name="clipboard-paste")
        await asyncio.sleep(0)
        app.exit()
        await app.lifecycle.shutdown(task_timeout=0.5)
        assert any(
            item.name == "clipboard-copy" and item.stage == "done" and isinstance(item.error, RuntimeError)
            for item in app.lifecycle.failures
        )
        assert helper_cancelled.is_set()
        assert runner._runtime.lease is not None
        with pytest.raises(RuntimeError):
            app._io_executor.submit(lambda: None)

    try:
        asyncio.run(scenario())
    finally:
        pipe.__exit__(None, None, None)
        runner.close()
        store.close()


def test_cleanup_timeout_is_bounded_and_keeps_the_lease(tmp_path) -> None:
    store = SessionStore(tmp_path / "timeout.sqlite3")
    runner = _runner(store)
    pipe = create_pipe_input()
    pipe.__enter__()
    pending: list[asyncio.Task[object]] = []

    async def scenario() -> None:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.application.exit = lambda *args, **kwargs: None  # type: ignore[method-assign]

        async def sticky() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(30)

        pending.append(app.lifecycle.spawn(sticky(), stop=TaskStop.CANCEL, name="clipboard-paste"))
        await asyncio.sleep(0)
        started = asyncio.get_running_loop().time()
        app.exit()
        await app.lifecycle.shutdown(task_timeout=0.2)
        assert asyncio.get_running_loop().time() - started < 1.5
        assert any(item.name == "clipboard-paste" and item.stage == "timeout" for item in app.lifecycle.failures)
        assert "clipboard-paste" in app.lifecycle.unfinished
        assert runner._runtime.lease is not None
        assert not pending[0].cancelled()

    try:
        asyncio.run(scenario())
        assert pending[0].cancelled()
        assert runner._runtime.lease is not None
    finally:
        pipe.__exit__(None, None, None)
        runner.close()
        store.close()


def test_failure_before_shutdown_is_recorded_once() -> None:
    runner = _runner()
    pipe = create_pipe_input()
    pipe.__enter__()

    async def scenario() -> None:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.application.exit = lambda *args, **kwargs: None  # type: ignore[method-assign]

        async def boom() -> None:
            raise RuntimeError("early clipboard")

        app.lifecycle.spawn(boom(), stop=TaskStop.CANCEL, name="clipboard-copy")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert app.lifecycle._tracked == []
        app.exit()
        await app.lifecycle.shutdown(task_timeout=0.2)
        matches = [
            item for item in app.lifecycle.failures
            if item.name == "clipboard-copy" and isinstance(item.error, RuntimeError)
        ]
        assert len(matches) == 1
        assert matches[0].stage == "done"
        before = app.lifecycle.failures
        await app.lifecycle.shutdown()
        assert app.lifecycle.failures == before
        assert app.lifecycle.phase is ExitPhase.STOPPED

    try:
        asyncio.run(scenario())
    finally:
        pipe.__exit__(None, None, None)
        runner.close()


def test_shutdown_refuses_a_new_observe_task_without_await_warning() -> None:
    runner = _runner()
    pipe = create_pipe_input()
    pipe.__enter__()

    async def scenario() -> None:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app.application.exit = lambda *args, **kwargs: None  # type: ignore[method-assign]
        app.exit()

        async def during_exit() -> None:
            await asyncio.sleep(30)

        with pytest.raises(RuntimeError, match="refusing task during-exit"):
            app.lifecycle.spawn(during_exit(), stop=TaskStop.OBSERVE, name="during-exit")
        await app.lifecycle.shutdown(task_timeout=0.05)

        async def late() -> None:
            await asyncio.sleep(30)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            with pytest.raises(RuntimeError, match="refusing task late-run"):
                app.lifecycle.spawn(late(), stop=TaskStop.OBSERVE, name="late-run")
            await asyncio.sleep(0)
        assert app.lifecycle._tracked == []
        assert not any("never awaited" in str(item.message) for item in caught)

        async def helper() -> None:
            await asyncio.sleep(30)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            refused = app.lifecycle.spawn(helper(), stop=TaskStop.CANCEL, name="clipboard-copy")
            await asyncio.sleep(0)
        assert refused.done() and refused.exception() is None
        assert not any("never awaited" in str(item.message) for item in caught)
        assert all(item.name != "clipboard-copy" for item in app.lifecycle.failures)

    try:
        asyncio.run(scenario())
    finally:
        pipe.__exit__(None, None, None)
        runner.close()
