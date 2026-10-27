"""Expose an async model stream as a sync iterator that another thread can cancel.

The synchronous LangGraph runner consumes model output on a worker thread. A
blocking ``httpx`` read cannot be interrupted from outside that thread, so the
request itself runs as an ``asyncio`` task on one managed loop thread, and the
worker iterates the chunks through a bounded hand-off queue. Cancelling the
task interrupts the pending read, lets the SDK close the HTTP response, and
wakes the worker.

Every bridged request uses the same loop, so a model's ``httpx.AsyncClient``
never crosses loops. The loop starts on first use and stops at interpreter
exit (or ``shutdown_stream_loop``), cancelling any task still running.
"""
from __future__ import annotations

import asyncio
import atexit
from contextvars import ContextVar, Token, copy_context
import os
import queue
import threading
from typing import Any, AsyncIterator, Callable, Generic, Iterator, Protocol, TypeVar

from tenacity import RetryCallState

T = TypeVar("T")

# Upper bound for waiting until a cancelled request has released its HTTP
# response. Cancellation interrupts a pending read immediately; this only
# bounds a misbehaving transport, after which the stream is reported unclosed.
CANCEL_CLOSE_SECONDS = 5.0
MAX_BUFFERED_CHUNKS = 64


class ModelStreamCancelled(Exception):
    """The model request was cancelled by the run that owns it."""

    def __init__(self, message: str = "Model stream cancelled", *, closed: bool = True) -> None:
        super().__init__(message)
        self.closed = closed


class StreamOwner(Protocol):
    def register_model_stream(self, stream: "CancellableStreamBridge[Any]") -> bool: ...

    def unregister_model_stream(self, stream: "CancellableStreamBridge[Any]") -> None: ...


_OWNER: ContextVar[StreamOwner | None] = ContextVar("deep_agent_model_stream_owner", default=None)


def set_stream_owner(owner: StreamOwner | None) -> Token[StreamOwner | None]:
    return _OWNER.set(owner)


def reset_stream_owner(token: Token[StreamOwner | None]) -> None:
    _OWNER.reset(token)


def current_stream_owner() -> StreamOwner | None:
    return _OWNER.get()


class _StreamLoop:
    """The single daemon thread that owns the event loop for bridged streams."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pid: int | None = None

    def get(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if (
                self._loop is not None and self._thread is not None
                and self._thread.is_alive() and self._pid == os.getpid()
            ):
                return self._loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()
            thread = threading.Thread(
                target=self._run, args=(loop, ready), name="deep-agent-model-stream", daemon=True,
            )
            thread.start()
            ready.wait()
            self._loop, self._thread, self._pid = loop, thread, os.getpid()
            return loop

    @staticmethod
    def _run(loop: asyncio.AbstractEventLoop, ready: threading.Event) -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        try:
            loop.run_forever()
        finally:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def in_loop_thread(self) -> bool:
        thread = self._thread
        return thread is not None and thread is threading.current_thread()

    def shutdown(self, timeout: float = CANCEL_CLOSE_SECONDS) -> bool:
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is None or thread is None or not thread.is_alive():
            return True
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout)
        return not thread.is_alive()


_LOOP = _StreamLoop()
atexit.register(_LOOP.shutdown)

_LIVE_LOCK = threading.Lock()
_LIVE: set["CancellableStreamBridge[Any]"] = set()


def live_stream_count() -> int:
    """Bridged requests whose async task has not finished yet."""
    with _LIVE_LOCK:
        return len(_LIVE)


def in_stream_loop_thread() -> bool:
    return _LOOP.in_loop_thread()


def shutdown_stream_loop(timeout: float = CANCEL_CLOSE_SECONDS) -> bool:
    """Stop the shared loop; tasks still running are cancelled first."""
    return _LOOP.shutdown(timeout)


_CHUNK = "chunk"
_DONE = "done"
_ERROR = "error"
_CANCELLED = "cancelled"
_WAKE = "wake"


class CancellableStreamBridge(Generic[T]):
    """Run one async iterator on the stream loop and hand its items to one consumer."""

    def __init__(
        self,
        factory: Callable[[], AsyncIterator[T]],
        *,
        max_buffered: int = MAX_BUFFERED_CHUNKS,
        close_timeout: float = CANCEL_CLOSE_SECONDS,
    ) -> None:
        if max_buffered < 1:
            raise ValueError("max_buffered must be positive")
        self._factory = factory
        self._max_buffered = max_buffered
        self._close_timeout = close_timeout
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._lock = threading.Lock()
        self._state = "new"  # new -> running -> finished
        self._cancel_requested = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._slots: asyncio.Semaphore | None = None
        self._closed = threading.Event()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    @property
    def closed(self) -> bool:
        """True once the async request has fully finished, or never started."""
        return self._closed.is_set()

    def wait_closed(self, timeout: float) -> bool:
        return self._closed.wait(timeout)

    def cancel(self) -> None:
        """Request cancellation from any thread; never waits for network I/O."""
        with self._lock:
            if self._cancel_requested:
                return
            self._cancel_requested = True
            loop = self._loop if self._state == "running" else None
        self._queue.put((_WAKE, None))
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._cancel_task)
            except RuntimeError:
                # The loop is gone, and its shutdown already cancelled the task.
                pass

    def close(self) -> bool:
        """Cancel an unfinished request and wait (bounded) until it released its response."""
        with self._lock:
            unfinished = self._state == "running" and not self._closed.is_set()
        if unfinished:
            self.cancel()
        return self._closed.wait(self._close_timeout) if self._state != "new" else True

    def iter_chunks(self) -> Iterator[T]:
        self._start()
        try:
            while True:
                kind, value = self._queue.get()
                if kind == _DONE:
                    self._closed.wait(self._close_timeout)
                    return
                if kind == _ERROR and not self._cancel_requested:
                    self._closed.wait(self._close_timeout)
                    raise value
                if self._cancel_requested or kind in (_CANCELLED, _ERROR, _WAKE):
                    closed = self._closed.wait(self._close_timeout)
                    raise ModelStreamCancelled(
                        "Model stream cancelled" if closed
                        else "Model stream cancelled, but the HTTP request did not confirm it closed",
                        closed=closed,
                    )
                self._release_slot()
                yield value
        finally:
            self.close()

    # -- internals --------------------------------------------------------------------
    def _start(self) -> None:
        with self._lock:
            if self._state != "new":
                raise RuntimeError("A model stream bridge can only be iterated once")
            if self._cancel_requested:
                self._state = "finished"
                self._closed.set()
                self._queue.put((_CANCELLED, None))
                return
            loop = _LOOP.get()
            self._loop = loop
            self._state = "running"
            with _LIVE_LOCK:
                _LIVE.add(self)
            context = copy_context()
            try:
                loop.call_soon_threadsafe(self._create_task, context)
            except RuntimeError as exc:
                self._finish()
                self._queue.put((_ERROR, exc))

    def _create_task(self, context: Any) -> None:
        assert self._loop is not None
        if self._cancel_requested:
            self._queue.put((_CANCELLED, None))
            self._finish()
            return
        self._slots = asyncio.Semaphore(self._max_buffered)
        self._task = self._loop.create_task(self._pump(), context=context)
        self._task.add_done_callback(lambda _task: self._finish())

    def _cancel_task(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    def _release_slot(self) -> None:
        loop, slots = self._loop, self._slots
        if loop is not None and slots is not None:
            try:
                loop.call_soon_threadsafe(slots.release)
            except RuntimeError:
                pass

    def _finish(self) -> None:
        with self._lock:
            self._state = "finished"
        with _LIVE_LOCK:
            _LIVE.discard(self)
        self._closed.set()

    async def _pump(self) -> None:
        assert self._slots is not None
        iterator: AsyncIterator[T] | None = None
        outcome: tuple[str, Any] = (_DONE, None)
        try:
            iterator = self._factory()
            async for item in iterator:
                await self._slots.acquire()
                self._queue.put((_CHUNK, item))
        except asyncio.CancelledError:
            outcome = (_CANCELLED, None)
        except BaseException as exc:  # noqa: BLE001 - every failure reaches the consumer
            outcome = (_ERROR, exc)
        finally:
            aclose = getattr(iterator, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except BaseException:  # noqa: BLE001
                    pass
            self._queue.put(outcome)


class ModelStreamInterrupted(Exception):
    """Retryable request or body failure within a streaming LLM call."""


def stream_retry_state(error: Exception, attempt: int) -> RetryCallState:
    state = RetryCallState(None, None, (), {})
    state.attempt_number = attempt
    state.set_exception((type(error), error, error.__traceback__))
    return state


def wait_for_stream_retry(attempt: int) -> None:
    """Back off on the managed loop, keeping Esc effective between requests."""
    async def wait() -> AsyncIterator[None]:
        await asyncio.sleep(stream_retry_delay(attempt))
        yield None

    bridge = CancellableStreamBridge(wait)
    owner = current_stream_owner()
    if owner is not None and not owner.register_model_stream(bridge):
        raise ModelStreamCancelled()
    try:
        list(bridge.iter_chunks())
    finally:
        if owner is not None:
            owner.unregister_model_stream(bridge)


def stream_retry_delay(attempt: int) -> float:
    return min(0.5 * 2 ** min(attempt, 4), 8.0)


def close_model_http_clients(model: Any, timeout: float = CANCEL_CLOSE_SECONDS) -> None:
    """Close clients owned by a short-lived model on the stream's own loop."""
    client = getattr(model, "http_client", None)
    if client is not None:
        client.close()
    async_client = getattr(model, "http_async_client", None)
    if async_client is not None:
        asyncio.run_coroutine_threadsafe(async_client.aclose(), _LOOP.get()).result(timeout)
