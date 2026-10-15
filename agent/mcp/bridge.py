"""Delegate complete LangChain invocations to one owned asyncio loop."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, CancelledError, TimeoutError
import threading
from typing import Any, Awaitable

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, ToolException
from pydantic import PrivateAttr

from agent.cancel import get_cancel_context, emit_tool_output


class AsyncBridge:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._pending: dict[Future, Any] = {}
        self._closed = False
        self._thread = threading.Thread(target=self._serve, name="deep-agent-mcp", daemon=True)
        self._thread.start()
        self._ready.wait()

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.run_until_complete(self._loop.shutdown_default_executor())
        finally:
            self._loop.close()

    def run(self, awaitable: Awaitable, *, timeout: float | None = None) -> Any:
        if threading.current_thread() is self._thread:
            awaitable.close()
            raise RuntimeError("Cannot synchronously wait on the MCP event loop")
        result: Future = Future()
        cancel_requested = threading.Event()
        task: asyncio.Task | None = None

        def cancel_on_loop() -> None:
            if cancel_requested.is_set():
                return
            cancel_requested.set()
            if task is not None and not task.done():
                task.cancel()

        def cancel() -> None:
            self._loop.call_soon_threadsafe(cancel_on_loop)

        def completed(done: asyncio.Task) -> None:
            # Unlike run_coroutine_threadsafe.cancel(), this Future completes
            # only AFTER the Task has finished its cancellation/finally cleanup.
            try:
                result.set_result(done.result())
            except asyncio.CancelledError:
                result.set_exception(CancelledError())
            except BaseException as exc:
                result.set_exception(exc)
            finally:
                with self._lock:
                    self._pending.pop(result, None)

        def start() -> None:
            nonlocal task
            task = self._loop.create_task(awaitable)
            task.add_done_callback(completed)
            if cancel_requested.is_set():
                task.cancel()

        with self._lock:
            if self._closed:
                awaitable.close()
                raise RuntimeError("MCP bridge is closed")
            self._pending[result] = cancel
            self._loop.call_soon_threadsafe(start)
        ctx = get_cancel_context()
        if ctx is not None:
            ctx.register_callback(cancel)
        try:
            return result.result(timeout=timeout)
        except TimeoutError:
            cancel()
            # Do not return before cleanup, even when a server ignores cancel.
            # Runner.close() will time out and retain its session lease.
            if ctx is not None:
                emit_tool_output(ctx.tool_call_id, "MCP request timed out; waiting for local cleanup.")
            try:
                result.result()
            except CancelledError:
                pass
            raise TimeoutError("MCP request timed out after local cleanup") from None

    def close(self) -> None:
        if threading.current_thread() is self._thread:
            raise RuntimeError("Cannot close MCP bridge from its own loop")
        with self._lock:
            self._closed = True
            pending = list(self._pending.items())
        for _, cancel in pending:
            cancel()
        for future, _ in pending:
            try:
                future.result(timeout=5)
            except TimeoutError:
                raise RuntimeError("MCP cleanup timed out; bridge remains alive until tasks drain") from None
            except BaseException:
                pass
        if self._thread.is_alive():
            async def drain_tasks() -> None:
                tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            cleanup = asyncio.run_coroutine_threadsafe(drain_tasks(), self._loop)
            try:
                cleanup.result(timeout=5)
            except TimeoutError:
                raise RuntimeError("MCP background cleanup timed out; event loop remains alive") from None
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError("MCP event loop did not stop")


class SyncMCPTool(BaseTool):
    """Delegate the entire invocation, including validation/result formatting once."""

    _original: BaseTool = PrivateAttr()
    _bridge: AsyncBridge = PrivateAttr()

    def __init__(self, original: BaseTool, bridge: AsyncBridge, *, profile: str, url: str) -> None:
        super().__init__(
            name=original.name, description=original.description,
            args_schema=original.args_schema, response_format=original.response_format,
            metadata={**(original.metadata or {}), "deep_agent_mcp": {"profile": profile, "url": url}},
            tags=original.tags, return_direct=original.return_direct,
            handle_tool_error=original.handle_tool_error,
            handle_validation_error=original.handle_validation_error,
        )
        self._original = original
        self._bridge = bridge

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        try:
            return self._bridge.run(self._original.ainvoke(input, config, **kwargs))
        except CancelledError:
            if isinstance(input, dict) and input.get("type") == "tool_call":
                return ToolMessage(content="Cancelled by user.", tool_call_id=input["id"],
                                   name=self.name, status="error")
            raise ToolException("Cancelled by user.") from None

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        # Keep Client calls on its owner loop even for library async consumers.
        return await asyncio.to_thread(self.invoke, input, config, **kwargs)

    def run(self, tool_input: Any, **kwargs: Any) -> Any:
        return self._bridge.run(self._original.arun(tool_input, **kwargs))

    async def arun(self, tool_input: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.run, tool_input, **kwargs)

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("MCP calls delegate through invoke() or run()")
