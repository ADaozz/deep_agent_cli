"""Operation state for MCP commands.

The UI reads and renders. This object owns whether a connect or mutation is
in progress, which operation generation it belongs to, and the switch lock.
Tool visibility and host execution stay on MCPManager.
"""
from __future__ import annotations

import asyncio


class MCPController:
    def __init__(self) -> None:
        self.busy = False
        self.operation_id = 0
        self.operation: asyncio.Task[object] | None = None
        self.quiet_settled = False
        self.publish_revision: int | None = None
        self.publish_outcome: str | None = None
        self._switch_lock: asyncio.Lock | None = None
        self._publish_lock: asyncio.Lock | None = None

    @property
    def switch_lock(self) -> asyncio.Lock:
        if self._switch_lock is None:
            self._switch_lock = asyncio.Lock()
        return self._switch_lock

    def try_begin(self, *, force: bool = False) -> int | None:
        """Reserve the next operation. None means a worker is already running."""
        if self.busy and not force:
            return None
        self.busy = True
        self.quiet_settled = False
        self.operation_id += 1
        return self.operation_id

    def finish(self, operation_id: int) -> None:
        if self.operation_id == operation_id:
            self.busy = False

    def owns(self, operation_id: int) -> bool:
        return self.operation_id == operation_id

    @property
    def publish_lock(self) -> asyncio.Lock:
        if self._publish_lock is None:
            self._publish_lock = asyncio.Lock()
        return self._publish_lock

    def cached_publish(self, revision: int, *, dirty: bool) -> str | None:
        """Skip a repeat of this revision. A new revision, or a dirty applied one, runs."""
        if self.publish_revision != revision:
            return None
        if self.publish_outcome == "failed":
            return "failed"
        if self.publish_outcome == "applied" and not dirty:
            return "applied"
        return None

    def remember_publish(self, revision: int, outcome: str, *, current_revision: int) -> None:
        """Record the outcome only if a newer revision has not already taken over."""
        if current_revision == revision:
            self.publish_revision = revision
            self.publish_outcome = outcome
