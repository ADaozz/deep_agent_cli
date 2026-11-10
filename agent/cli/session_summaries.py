"""Background title jobs and cached session labels for the terminal view."""
from __future__ import annotations

import asyncio
from contextvars import Context
from typing import TYPE_CHECKING, Awaitable, Callable

from agent.cli.lifecycle import TaskStop
from agent.models.session_summary import generate_session_summary
from agent.sessions.contracts import SessionInfo
from agent.sessions.summary import SessionSummaryRequest, validate_summary

if TYPE_CHECKING:
    from agent.cli.lifecycle import AppLifecycle
    from agent.runtime.runner import AgentRunner


SUMMARY_TIMEOUT_SECONDS = 10.0


class SessionSummaries:
    def __init__(self, *, runner: AgentRunner, lifecycle: Callable[[], AppLifecycle],
                 invalidate: Callable[[], None],
                 generate: Callable[[SessionSummaryRequest], Awaitable[str | None]] | None = None) -> None:
        self.runner = runner
        self.lifecycle = lifecycle
        self.invalidate = invalidate
        self.generate = generate or self._generate
        self._labels: dict[str, str | None] = {}
        self._attempted: set[str] = set()
        if runner.session_store is not None:
            info = runner.session_store.get(runner.thread_id)
            if info is not None:
                self.remember(info)

    def remember(self, info: SessionInfo) -> None:
        if info.summary or info.id not in self._labels:
            self._labels[info.id] = info.summary

    def label(self, thread_id: str) -> str:
        return self._labels.get(thread_id) or thread_id

    def request(self, request: SessionSummaryRequest) -> None:
        if (self.lifecycle().exiting or self.runner.session_store is None
                or request.thread_id in self._attempted or self._labels.get(request.thread_id)):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._attempted.add(request.thread_id)
        # Do not inherit a graph's callbacks, stream owner or checkpoint context.
        Context().run(
            self.lifecycle().spawn, self._complete(request), stop=TaskStop.CANCEL,
            name=f"session-summary-{request.thread_id[:8]}",
        )

    async def _generate(self, request: SessionSummaryRequest) -> str | None:
        settings = self.runner.settings
        if settings is None:
            return None
        return await generate_session_summary(
            request, settings.get_profile(request.model_id),
            secrets=self.runner.summary_secrets(), trust_env=settings.trust_env,
        )

    async def _complete(self, request: SessionSummaryRequest) -> None:
        try:
            async with asyncio.timeout(SUMMARY_TIMEOUT_SECONDS):
                title = await self.generate(request)
            title = validate_summary(title or "", self.runner.summary_secrets())
            if title is None or self.lifecycle().exiting:
                return
            store = self.runner.session_store
            if store is None:
                return
            if store.set_summary_if_empty(request.thread_id, title):
                self._labels[request.thread_id] = title
            else:
                info = store.get(request.thread_id)
                if info is not None:
                    self.remember(info)
            self.invalidate()
        except asyncio.CancelledError:
            raise
        except Exception:
            # An optional label must never fail or overwrite the coding task.
            return
