"""Exit request and UI shutdown stay apart from Runner.close()."""
from __future__ import annotations

import asyncio

import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent.cli.app import CliApplication
from agent.cli.lifecycle import ExitPhase, TaskStop
from agent.runner import AgentRunner
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
