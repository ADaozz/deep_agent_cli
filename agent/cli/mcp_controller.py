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
        self._switch_lock: asyncio.Lock | None = None

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
