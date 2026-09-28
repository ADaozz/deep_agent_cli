"""Exclusive thread ownership locks for the session store."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from agent.session import SessionStore
from agent.session_lock import (
    InProcessSessionLockManager,
    SessionLockBusyError,
    SessionLockManager,
    SessionLease,
)


def test_two_managers_cannot_hold_the_same_thread(tmp_path: Path) -> None:
    manager = SessionLockManager(tmp_path / "sessions.sqlite3")
    other = SessionLockManager(tmp_path / "sessions.sqlite3")
    lease = manager.try_acquire("thread-a")
    assert lease is not None
    try:
        assert other.try_acquire("thread-a") is None
        assert manager.try_acquire("thread-a") is None
    finally:
        lease.release()
    assert other.try_acquire("thread-a") is not None


def test_different_threads_are_held_in_parallel(tmp_path: Path) -> None:
    manager = SessionLockManager(tmp_path / "sessions.sqlite3")
    other = SessionLockManager(tmp_path / "sessions.sqlite3")
    first = manager.try_acquire("thread-a")
    second = other.try_acquire("thread-b")
    assert first is not None and second is not None
    first.release()
    second.release()


def test_waiter_takes_over_after_release(tmp_path: Path) -> None:
    manager = SessionLockManager(tmp_path / "sessions.sqlite3")
    holder = manager.try_acquire("thread-a")
    assert holder is not None
    acquired = threading.Event()
    lease_box: list[object] = []

    def waiter() -> None:
        lease = manager.wait_acquire("thread-a", poll_seconds=0.01)
        lease_box.append(lease)
        acquired.set()

    thread = threading.Thread(target=waiter)
    thread.start()
    try:
        assert not acquired.wait(0.05)
    finally:
        holder.release()
    assert acquired.wait(5)
    thread.join(5)
    lease = lease_box[0]
    assert isinstance(lease, SessionLease)
    lease.release()


def test_wait_acquire_honours_cancellation(tmp_path: Path) -> None:
    manager = SessionLockManager(tmp_path / "sessions.sqlite3")
    holder = manager.try_acquire("thread-a")
    assert holder is not None
    try:
        assert manager.wait_acquire(
            "thread-a", cancelled=lambda: True, poll_seconds=0.01,
        ) is None
    finally:
        holder.release()


def test_released_lock_file_is_not_deleted(tmp_path: Path) -> None:
    manager = SessionLockManager(tmp_path / "sessions.sqlite3")
    path = manager.lock_path("thread-a")
    lease = manager.try_acquire("thread-a")
    assert lease is not None
    lease.release()
    assert path.is_file()


def test_manager_close_releases_remaining_leases(tmp_path: Path) -> None:
    manager = SessionLockManager(tmp_path / "sessions.sqlite3")
    other = SessionLockManager(tmp_path / "sessions.sqlite3")
    lease = manager.try_acquire("thread-a")
    assert lease is not None
    manager.close()
    assert lease.released
    manager.close()  # idempotent
    assert other.try_acquire("thread-a") is not None


def test_lock_files_and_directory_are_private(tmp_path: Path) -> None:
    manager = SessionLockManager(tmp_path / "sessions.sqlite3")
    lease = manager.try_acquire("thread-a")
    assert lease is not None
    root = tmp_path / "sessions.sqlite3.locks"
    assert (stat := root.stat()) and stat.st_mode & 0o777 == 0o700
    assert (stat := manager.lock_path("thread-a").stat()) and stat.st_mode & 0o777 == 0o600


def test_lock_descriptor_is_close_on_exec(tmp_path: Path) -> None:
    manager = SessionLockManager(tmp_path / "sessions.sqlite3")
    lease = manager.try_acquire("thread-a")
    assert lease is not None
    try:
        assert not os.get_inheritable(lease.fileno())
    finally:
        lease.release()


def test_killed_process_releases_its_lock(tmp_path: Path) -> None:
    db = tmp_path / "killed.sqlite3"
    script = (
        "import os, signal, sys\n"
        "from pathlib import Path\n"
        "from agent.session_lock import SessionLockManager\n"
        "manager = SessionLockManager(Path(sys.argv[1]))\n"
        "lease = manager.try_acquire('held-thread')\n"
        "assert lease is not None\n"
        "print('locked', flush=True)\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(db)],
        stdout=subprocess.PIPE, text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "locked"
    process.wait(timeout=10)

    manager = SessionLockManager(db)
    lease = manager.try_acquire("held-thread")
    assert lease is not None
    lease.release()


def test_live_process_keeps_the_thread_busy(tmp_path: Path) -> None:
    db = tmp_path / "held.sqlite3"
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from agent.session_lock import SessionLockManager\n"
        "manager = SessionLockManager(Path(sys.argv[1]))\n"
        "lease = manager.try_acquire('held-thread')\n"
        "assert lease is not None\n"
        "print('locked', flush=True)\n"
        "time.sleep(float(sys.argv[2]))\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(db), "10"],
        stdout=subprocess.PIPE, text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "locked"
    try:
        manager = SessionLockManager(db)
        deadline = time.monotonic() + 0.5
        busy = True
        while time.monotonic() < deadline:
            lease = manager.try_acquire("held-thread")
            if lease is not None:
                lease.release()
                busy = False
                break
            time.sleep(0.01)
        assert busy, "the live child process must keep the thread busy"
    finally:
        process.kill()
        process.wait(timeout=10)


def test_store_exposes_leases_and_close_releases_them(tmp_path: Path) -> None:
    db = tmp_path / "store.sqlite3"
    store = SessionStore(db)
    lease = store.try_acquire_session("thread-a")
    assert lease is not None
    assert store.try_acquire_session("thread-a") is None
    store.close()
    store.close()  # idempotent
    reopened = SessionStore(db)
    assert reopened.try_acquire_session("thread-a") is not None
    reopened.close()


def test_session_lock_busy_error_is_a_runtime_error() -> None:
    assert issubclass(SessionLockBusyError, RuntimeError)


class _FakeSaver:
    """Minimal weakref-able checkpointer stand-in."""


def test_in_process_same_checkpointer_same_thread_is_exclusive() -> None:
    saver = _FakeSaver()
    first = InProcessSessionLockManager(saver)
    second = InProcessSessionLockManager(saver)
    lease = first.try_acquire("thread-a")
    assert lease is not None
    try:
        assert second.try_acquire("thread-a") is None
    finally:
        lease.release()
    assert second.try_acquire("thread-a") is not None


def test_in_process_different_checkpointers_do_not_block() -> None:
    first = InProcessSessionLockManager(_FakeSaver())
    second = InProcessSessionLockManager(_FakeSaver())
    left = first.try_acquire("thread-a")
    right = second.try_acquire("thread-a")
    assert left is not None and right is not None
    left.release()
    right.release()


def test_in_process_garbage_collected_lease_frees_the_thread() -> None:
    saver = _FakeSaver()
    manager = InProcessSessionLockManager(saver)
    manager.try_acquire("thread-a")
    import gc

    gc.collect()
    assert manager.try_acquire("thread-a") is not None


def test_in_process_wait_honours_cancellation() -> None:
    saver = _FakeSaver()
    manager = InProcessSessionLockManager(saver)
    other = InProcessSessionLockManager(saver)
    holder = other.try_acquire("thread-a")
    assert holder is not None
    try:
        assert manager.wait_acquire(
            "thread-a", cancelled=lambda: True, poll_seconds=0.01,
        ) is None
    finally:
        holder.release()


def test_in_process_close_releases_leases() -> None:
    saver = _FakeSaver()
    manager = InProcessSessionLockManager(saver)
    other = InProcessSessionLockManager(saver)
    lease = manager.try_acquire("thread-a")
    assert lease is not None
    manager.close()
    assert lease.released
    assert other.try_acquire("thread-a") is not None
