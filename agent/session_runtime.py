"""Session ownership: which persisted thread this runner may write, and its lease.

The runtime owns the thread lease, the switch bookkeeping and the session
catalog metadata. It never touches the graph — loading snapshots and rebuilding
models stay with AgentRunner, which coordinates through these primitives.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
    reasoning_effort: str = "default"
    notices: tuple[str, ...] = ()


class SessionRuntime:
    """Lease lifecycle plus catalog metadata for one runner's session."""

    def __init__(
        self,
        *,
        session_store: SessionStore | None,
        checkpointer: Any | None,
        settings: Settings | None,
        persist_on_first_message: bool = False,
    ) -> None:
        self._store = session_store
        self._settings = settings
        self._in_process_locks = InProcessSessionLockManager(checkpointer)
        self._persist_on_first_message = persist_on_first_message
        self._draft: SessionInfo | None = None
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
        if self._store is not None and thread_id is None and self._persist_on_first_message:
            return self.create_new(model_id=model_id, permission_mode=permission_mode).id
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
        if self._draft is not None and self._draft.id == thread_id:
            return self._in_process_locks.try_acquire(thread_id)
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
        if self._draft is not None and self._draft.id != thread_id:
            self._draft = None
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

    def create_new(self, *, model_id: str, permission_mode: str, title: str = "") -> SessionInfo:
        """Own a blank draft without a catalog row or persistent lock file."""
        if self._store is not None and not self._persist_on_first_message:
            info = self._store.create_session(model_id=model_id, permission_mode=permission_mode, title=title)
            self.adopt_new(info.id)
            return info
        now = datetime.now(timezone.utc)
        identifier = str(uuid4()) if self._store is not None else f"cli-{uuid4()}"
        info = SessionInfo(id=identifier, title=title or "New session", created_at=now,
                           updated_at=now, status="running", model_id=model_id,
                           permission_mode=permission_mode)
        lease = self._in_process_locks.try_acquire(info.id)
        if lease is None:
            raise SessionLockBusyError(f"Session {info.id} is already open in another window")
        self.adopt(info.id, lease)
        self._draft = info
        return info

    def persist_for_message(self, *, model_id: str, permission_mode: str, reasoning_effort: str) -> None:
        """Promote a draft before the graph can write its first checkpoint."""
        if self._store is None or self._draft is None:
            return
        draft = self._draft
        lease = self._store.try_acquire_session(draft.id)
        if lease is None:
            raise SessionLockBusyError(f"Session {draft.id} is already open in another window")
        try:
            self._store.create_session(session_id=draft.id, title=draft.title, model_id=model_id,
                                       permission_mode=permission_mode, reasoning_effort=reasoning_effort)
        except Exception:
            lease.release()
            raise
        self.adopt(draft.id, lease)
        self._draft = None

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
        effort = info.reasoning_effort
        if profile is None or info.model_id != profile.id or (
            effort != "default" and effort not in profile.reasoning_efforts
        ):
            if effort != "default":
                notices.append(f"Saved reasoning effort {effort} is unavailable; using default.")
            effort = "default"
        mode = parse_permission_mode(info.permission_mode or "ask") or PermissionMode.ASK
        if mode is PermissionMode.ALLOW and not allow_mode_available(execution_mode):
            mode = PermissionMode.ASK
            notices.append("Saved allow permission is unavailable here; using ask.")
        return RestorePlan(
            model_id=profile.id if profile is not None else "",
            permission_mode=mode,
            reasoning_effort=effort,
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
