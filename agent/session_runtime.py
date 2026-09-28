"""Session ownership: which persisted thread this runner may write, and its lease.

The runtime owns the thread lease, the switch bookkeeping and the session
catalog metadata. It never touches the graph — loading snapshots and rebuilding
models stay with AgentRunner, which coordinates through these primitives.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from typing import Any, Callable
from uuid import uuid4

from agent.config import Settings
from agent.middleware.recovery import RecoveryContext
from agent.permission import (
    PermissionMode,
    allow_mode_available,
    parse_permission_mode,
)
from agent.session import SessionInfo, SessionStore, StopReason
from agent.session_lock import (
    InProcessSessionLease,
    InProcessSessionLockManager,
    SessionLease,
    SessionLockBusyError,
)

SessionLeaseLike = SessionLease | InProcessSessionLease


@dataclass(frozen=True)
class RestorePlan:
    """Model and permission a target thread should run with after a switch."""

    model_id: str
    permission_mode: PermissionMode
    notices: tuple[str, ...] = ()


class SessionRuntime:
    """Lease lifecycle plus catalog metadata for one runner's session."""

    def __init__(
        self,
        *,
        session_store: SessionStore | None,
        checkpointer: Any | None,
        settings: Settings | None,
    ) -> None:
        self._store = session_store
        self._settings = settings
        self._in_process_locks = (
            None if session_store is not None else InProcessSessionLockManager(checkpointer)
        )
        self._thread_id: str | None = None
        self._lease: SessionLeaseLike | None = None
        self._pending_switch: str | None = None
        self._abort_wait = Event()

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    @property
    def lease(self) -> SessionLeaseLike | None:
        return self._lease

    @property
    def pending_switch(self) -> str | None:
        return self._pending_switch

    def owns(self, thread_id: str) -> bool:
        return self._lease is not None and self._lease.thread_id == thread_id

    def acquire_initial(
        self, thread_id: str | None, *, model_id: str, permission_mode: str,
    ) -> str:
        """Resolve or create the first thread, then take its lease."""
        if self._store is None:
            target = thread_id or f"cli-{uuid4()}"
        elif thread_id is None:
            info = self._store.create_session(model_id=model_id, permission_mode=permission_mode)
            target = info.id
        else:
            if self._store.get(thread_id) is None:
                self._store.create_session(
                    session_id=thread_id, model_id=model_id, permission_mode=permission_mode,
                )
            target = thread_id
        lease = self.try_acquire(target)
        if lease is None:
            raise SessionLockBusyError(f"Session {target} is already open in another window")
        self._thread_id = target
        self._lease = lease
        return target

    def try_acquire(self, thread_id: str) -> SessionLeaseLike | None:
        if self._store is not None:
            return self._store.try_acquire_session(thread_id)
        assert self._in_process_locks is not None
        return self._in_process_locks.try_acquire(thread_id)

    def wait_acquire(
        self, thread_id: str, *, cancelled: Callable[[], bool] | None = None,
    ) -> SessionLeaseLike | None:
        def combined() -> bool:
            return self._abort_wait.is_set() or (cancelled is not None and cancelled())

        if self._store is not None:
            return self._store.wait_acquire_session(thread_id, cancelled=combined)
        assert self._in_process_locks is not None
        return self._in_process_locks.wait_acquire(thread_id, cancelled=combined)

    def adopt(self, thread_id: str, lease: SessionLeaseLike) -> None:
        """Bind a freshly acquired target and drop the previous lease."""
        previous = self._lease
        self._thread_id = thread_id
        self._lease = lease
        self._pending_switch = None
        if previous is not None:
            previous.release()

    def adopt_new(self, thread_id: str) -> None:
        """Take over a thread this runner just created."""
        lease = self.try_acquire(thread_id)
        if lease is None:
            raise SessionLockBusyError(f"Session {thread_id} is already open in another window")
        self.adopt(thread_id, lease)

    def bind(self, thread_id: str) -> None:
        """Point at a thread whose lease is already held (A→A reload)."""
        self._thread_id = thread_id
        self._pending_switch = None

    def detach_for_wait(self, target_id: str) -> None:
        """Release the current lease and refuse operations while waiting."""
        lease = self._lease
        self._lease = None
        if lease is not None:
            lease.release()
        self._pending_switch = target_id

    def clear_pending(self) -> None:
        self._pending_switch = None

    def release(self) -> None:
        lease = self._lease
        self._lease = None
        if lease is not None:
            lease.release()

    def abort_wait(self) -> None:
        self._abort_wait.set()

    def close(self) -> None:
        self._abort_wait.set()
        self.release()

    def list_sessions(self, *, limit: int = 50) -> list[SessionInfo]:
        if self._store is None:
            return []
        return self._store.list_sessions(limit=limit)

    def resolve(self, session_id: str) -> SessionInfo | None:
        if self._store is None:
            return None
        return self._store.resolve_prefix(session_id) or self._store.get(session_id)

    def get(self, session_id: str) -> SessionInfo | None:
        if self._store is None:
            return None
        return self._store.get(session_id)

    def thread_has_checkpoint(self, thread_id: str) -> bool:
        if self._store is None:
            return False
        return self._store.checkpointer.get_tuple(
            {"configurable": {"thread_id": thread_id}}
        ) is not None

    def restore_plan(self, info: SessionInfo, *, execution_mode: Any) -> RestorePlan:
        """Decide which model and permission the target thread should run with."""
        notices: list[str] = []
        profile = self._settings.active_profile if self._settings is not None else None
        if self._settings is not None and info.model_id:
            try:
                profile = self._settings.get_profile(info.model_id)
            except KeyError:
                notices.append(f"Saved model {info.model_id} is unavailable; using {profile.id}.")
        mode = parse_permission_mode(info.permission_mode or "ask") or PermissionMode.ASK
        if mode is PermissionMode.ALLOW and not allow_mode_available(execution_mode):
            mode = PermissionMode.ASK
            notices.append("Saved allow permission is unavailable here; using ask.")
        return RestorePlan(
            model_id=profile.id if profile is not None else "",
            permission_mode=mode,
            notices=tuple(notices),
        )

    def recovery_context(
        self, info: SessionInfo, *, has_checkpoint: bool, interrupt_active: bool,
    ) -> RecoveryContext | None:
        needs_recovery = info.last_run_status in {StopReason.ABORTED, StopReason.ERROR} or (
            info.last_run_status is StopReason.PENDING and has_checkpoint
        )
        if needs_recovery and not interrupt_active:
            return RecoveryContext.for_stop_reason(info.last_run_status)
        return None
