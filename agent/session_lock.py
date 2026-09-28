"""Exclusive thread ownership for one session database.

A lease is an advisory ``flock`` on ``<database>.locks/<sha256(thread_id)>.lock``
so two processes can never write the same persisted thread at once. Lock files
are created once and never deleted; releasing only drops the kernel hold. The
locks directory and files stay private to the database owner and every file
descriptor is close-on-exec so agent child processes cannot inherit a hold.

Runners without a persisted store share nothing across processes; for those,
``InProcessSessionLockManager`` guards threads that flow through one
checkpointer object within this process.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable
from weakref import WeakKeyDictionary, ref

WAIT_POLL_SECONDS = 0.05


class SessionLockBusyError(RuntimeError):
    """The thread is already owned by another runner or process."""


class SessionLease:
    """Ownership of one persisted thread; ``release`` is idempotent."""

    def __init__(
        self,
        thread_id: str,
        handle: Any,
        on_release: Callable[["SessionLease"], None],
    ) -> None:
        self._thread_id = thread_id
        self._handle = handle
        self._on_release = on_release
        self._released = False

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
        self._on_release(self)

    def fileno(self) -> int:
        return self._handle.fileno()


class SessionLockManager:
    """flock-backed thread ownership beside one session database."""

    def __init__(self, database: Path) -> None:
        self._root = database.parent / f"{database.name}.locks"
        self._root.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            os.chmod(self._root, 0o700)
        except OSError:
            pass
        self._mutex = threading.Lock()
        self._leases: dict[str, "ref[SessionLease]"] = {}

    def lock_path(self, thread_id: str) -> Path:
        digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
        return self._root / f"{digest}.lock"

    def try_acquire(self, thread_id: str) -> SessionLease | None:
        path = self.lock_path(thread_id)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        handle = os.fdopen(fd, "r+b")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        lease = SessionLease(thread_id, handle, self._forget)
        with self._mutex:
            self._leases[thread_id] = ref(lease)
        return lease

    def wait_acquire(
        self,
        thread_id: str,
        *,
        cancelled: Callable[[], bool] | None = None,
        poll_seconds: float = WAIT_POLL_SECONDS,
    ) -> SessionLease | None:
        while True:
            if cancelled is not None and cancelled():
                return None
            lease = self.try_acquire(thread_id)
            if lease is not None:
                return lease
            time.sleep(poll_seconds)

    def close(self) -> None:
        """Release every lease this manager still tracks; idempotent."""
        with self._mutex:
            leases = [item() for item in self._leases.values()]
            self._leases.clear()
        for lease in leases:
            if lease is not None:
                lease.release()

    def _forget(self, lease: SessionLease) -> None:
        with self._mutex:
            held = self._leases.get(lease.thread_id)
            if held is not None and held() is lease:
                del self._leases[lease.thread_id]


class InProcessSessionLease:
    """Ownership of one in-memory thread; ``release`` is idempotent."""

    def __init__(self, thread_id: str, owners: dict[str, "ref[InProcessSessionLease]"]) -> None:
        self._thread_id = thread_id
        self._owners = owners
        self._released = False

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        with _IN_PROCESS_GUARD:
            held = self._owners.get(self._thread_id)
            if held is not None and held() is self:
                del self._owners[self._thread_id]


_IN_PROCESS_GUARD = threading.Lock()
_IN_PROCESS_OWNERS: "WeakKeyDictionary[Any, dict[str, ref[InProcessSessionLease]]]" = WeakKeyDictionary()


class InProcessSessionLockManager:
    """Guards threads shared through one checkpointer object within this process."""

    def __init__(self, checkpointer: Any) -> None:
        self._checkpointer = checkpointer

    def try_acquire(self, thread_id: str) -> InProcessSessionLease | None:
        if self._checkpointer is None:
            # Nothing is shared when the graph has no checkpointer at all.
            return InProcessSessionLease(thread_id, {})
        with _IN_PROCESS_GUARD:
            owners = _IN_PROCESS_OWNERS.get(self._checkpointer)
            if owners is None:
                owners = {}
                _IN_PROCESS_OWNERS[self._checkpointer] = owners
            held = owners.get(thread_id)
            if held is not None:
                lease = held()
                if lease is not None and not lease.released:
                    return None
            lease = InProcessSessionLease(thread_id, owners)
            owners[thread_id] = ref(lease)
            return lease

    def wait_acquire(
        self,
        thread_id: str,
        *,
        cancelled: Callable[[], bool] | None = None,
        poll_seconds: float = WAIT_POLL_SECONDS,
    ) -> InProcessSessionLease | None:
        while True:
            if cancelled is not None and cancelled():
                return None
            lease = self.try_acquire(thread_id)
            if lease is not None:
                return lease
            time.sleep(poll_seconds)

    def close(self) -> None:
        if self._checkpointer is None:
            return
        with _IN_PROCESS_GUARD:
            owners = _IN_PROCESS_OWNERS.get(self._checkpointer)
            if owners is None:
                return
            leases = [item() for item in owners.values()]
            owners.clear()
        for lease in leases:
            if lease is not None:
                lease.release()
