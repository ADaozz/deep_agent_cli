"""Run MCP coroutines on one long-lived loop.

LangChain's MCP tools only implement the async path. The runner still drives
``graph.stream()`` synchronously, so each call is submitted to this loop instead
of starting a fresh ``asyncio.run()``.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError as FutureCancelled
import threading
import time
from typing import Any


class MCPCallCancelled(Exception):
    """The caller asked the in-flight MCP call to stop."""


class MCPCallTimeout(Exception):
    """The MCP call did not finish before its deadline."""


class SyncAsyncBridge:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="deep-agent-mcp",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("MCP event loop did not start")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()
        self._loop.close()

    def call(
        self,
        coro: Any,
        *,
        timeout: float | None,
        cancel_event: threading.Event | None = None,
    ) -> Any:
        future = self._submit(coro)
        return self._wait(future, timeout=timeout, cancel_event=cancel_event)

    def _submit(self, coro: Any) -> Any:
        try:
            if self._closed:
                raise RuntimeError("MCP bridge is closed")
            return asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError:
            coro.close()
            raise

    async def acall(
        self,
        coro: Any,
        *,
        timeout: float | None,
        cancel_event: threading.Event | None = None,
    ) -> Any:
        """Await the existing bridge loop without blocking the caller's loop."""
        future = self._submit(coro)
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise MCPCallCancelled("Cancelled by user.")
                if future.done():
                    try:
                        return future.result()
                    except FutureCancelled as exc:
                        raise MCPCallCancelled("Cancelled by user.") from exc
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise MCPCallTimeout(f"MCP call timed out after {timeout:.0f}s")
                await asyncio.sleep(.05 if remaining is None else min(.05, remaining))
        except (asyncio.CancelledError, MCPCallCancelled, MCPCallTimeout):
            future.cancel()
            # Concurrent Future cancellation is acknowledged before the asyncio
            # task has unwound. Join only this request, on its owning loop.
            async def join_request() -> None:
                tasks = [
                    task for task in asyncio.all_tasks()
                    if task.get_coro() is coro and not task.done()
                ]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

            try:
                await asyncio.wait_for(
                    asyncio.wrap_future(self._submit(join_request())), timeout=2,
                )
            except (TimeoutError, RuntimeError):
                pass  # A closing manager drains the bridge; cleanup is bounded.
            raise

    def _wait(
        self,
        future: Any,
        *,
        timeout: float | None,
        cancel_event: threading.Event | None,
    ) -> Any:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                self._cancel_future(future)
                raise MCPCallCancelled("Cancelled by user.")
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                self._cancel_future(future)
                raise MCPCallTimeout(
                    "MCP call timed out" if timeout is None else f"MCP call timed out after {timeout:.0f}s"
                )
            slice_timeout = 0.05 if remaining is None else min(0.05, remaining)
            try:
                return future.result(slice_timeout)
            except TimeoutError:
                if future.done():
                    raise
                continue
            except (asyncio.CancelledError, FutureCancelled) as exc:
                raise MCPCallCancelled("Cancelled by user.") from exc

    @staticmethod
    def _cancel_future(future: Any) -> None:
        future.cancel()
        try:
            future.result(timeout=2)
        except Exception:  # noqa: BLE001 — cancellation, timeout, or the tool's own error
            pass

    def pending_task_count(self) -> int:
        async def _count() -> int:
            current = asyncio.current_task()
            return sum(1 for task in asyncio.all_tasks() if task is not current and not task.done())

        return int(self.call(_count(), timeout=5))

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True

        async def _drain() -> None:
            current = asyncio.current_task()
            tasks = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        if self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(_drain(), self._loop).result(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
