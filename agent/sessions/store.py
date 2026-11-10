"""SQLite session catalog + LangGraph SqliteSaver for local persistence."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable
from uuid import uuid4

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.sessions.contracts import SessionStatus, StopReason, SessionInfo
from agent.sessions.attachments import AttachmentStore
from agent.sessions.images import (
    AttachmentCleanupResult,
    ImageAttachmentRef,
    find_attachment_storage_keys,
)
from agent.sessions.locks import SessionLease, SessionLockManager


_CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS session_catalog (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    model_id TEXT,
    permission_mode TEXT,
    pending_model_id TEXT,
    pending_permission_mode TEXT,
    reasoning_effort TEXT NOT NULL DEFAULT 'default',
    pending_reasoning_effort TEXT,
    last_run_status TEXT NOT NULL,
    summary TEXT
);
CREATE INDEX IF NOT EXISTS session_catalog_updated_idx
    ON session_catalog (updated_at DESC);
CREATE TABLE IF NOT EXISTS session_model_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    metrics_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS session_model_metrics_session_idx
    ON session_model_metrics (session_id, id);
CREATE TABLE IF NOT EXISTS session_turn_timings (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    elapsed_seconds REAL NOT NULL,
    finished_at TEXT NOT NULL,
    PRIMARY KEY (session_id, message_id)
);
"""


def workspace_state_path(workspace: Path, *, override: str | Path | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser().resolve()
    env = os.environ.get("DEEP_AGENT_STATE_PATH")
    if env:
        return Path(env).expanduser().resolve()
    digest = hashlib.sha256(str(workspace.expanduser().resolve()).encode("utf-8")).hexdigest()[:16]
    return Path.home() / ".deep-agent" / "sessions" / f"{digest}.sqlite3"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _strict_serde() -> JsonPlusSerializer:
    # Strict allowlist: only built-in safe msgpack types are reconstructed.
    return JsonPlusSerializer(allowed_msgpack_modules=None, pickle_fallback=False)


class SessionStore:
    """Presentation-neutral session API backed by one SQLite file per workspace."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        try:
            self._initialize_catalog()
        except Exception:
            self._conn.close()
            raise
        if self.path.exists():
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self.checkpointer = SqliteSaver(self._conn, serde=_strict_serde())
        self.session_locks = SessionLockManager(self.path)
        self.attachment_store = AttachmentStore.beside_database(self.path)
        self.startup_cleanup: AttachmentCleanupResult | None = None
        try:
            self.startup_cleanup = self.cleanup_attachments()
        except Exception:  # noqa: BLE001 - persistence must remain usable if maintenance fails
            self.startup_cleanup = None
        self.attachment_store.acquire_runtime_lease()
        self._closed = False

    @classmethod
    def for_workspace(
        cls,
        workspace: Path,
        *,
        override: str | Path | None = None,
    ) -> "SessionStore":
        return cls(workspace_state_path(workspace, override=override))

    def has_checkpoint(self, thread_id: str) -> bool:
        return self.checkpointer.get_tuple({"configurable": {"thread_id": thread_id}}) is not None

    def try_acquire_session(self, thread_id: str) -> SessionLease | None:
        """Take exclusive ownership of a persisted thread; None when busy."""
        return self.session_locks.try_acquire(thread_id)

    def wait_acquire_session(
        self,
        thread_id: str,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> SessionLease | None:
        """Block until the thread is free, the wait is cancelled, or ownership lands."""
        return self.session_locks.wait_acquire(thread_id, cancelled=cancelled)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.session_locks.close()
        self.attachment_store.close()
        self._conn.close()


    def _initialize_catalog(self) -> None:
        expected = {
            "id", "title", "created_at", "updated_at", "model_id", "permission_mode",
            "pending_model_id", "pending_permission_mode", "reasoning_effort",
            "pending_reasoning_effort", "last_run_status", "summary",
        }
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(session_catalog)")}
            if columns == expected - {"summary"}:
                self._conn.execute("ALTER TABLE session_catalog ADD COLUMN summary TEXT")
            elif columns and columns != expected:
                raise ValueError(
                    "Unsupported session catalog schema; automatic migration is not supported. "
                    "Use a separate state database for this version."
                )
        self._conn.executescript(_CATALOG_DDL)

    def attachment_storage_keys(self) -> set[str]:
        """Return references from every retained checkpoint and pending write."""
        live: set[str] = set()
        for item in self.checkpointer.list(None):
            live.update(find_attachment_storage_keys(item.checkpoint))
            live.update(find_attachment_storage_keys(item.pending_writes))
        return live

    def cleanup_attachments(
        self,
        *,
        protected: tuple[ImageAttachmentRef, ...] = (),
        release_runtime_lease: bool = False,
    ) -> AttachmentCleanupResult:
        live = self.attachment_storage_keys()
        return self.attachment_store.cleanup(
            live,
            protected_storage_keys=(ref.storage_key for ref in protected),
            release_runtime_lease=release_runtime_lease,
        )

    def create_session(
        self, *, title: str = "", session_id: str | None = None,
        model_id: str | None = None, permission_mode: str = "ask",
        reasoning_effort: str = "default",
    ) -> SessionInfo:
        now = _utc_now()
        info = SessionInfo(
            id=session_id or str(uuid4()),
            title=title or "New session",
            created_at=now,
            updated_at=now,
            status="running",
            model_id=model_id,
            permission_mode=permission_mode,
            reasoning_effort=reasoning_effort,
        )
        with self.checkpointer.lock:
            self._conn.execute(
                "INSERT INTO session_catalog (id, title, created_at, updated_at, model_id, permission_mode, last_run_status, reasoning_effort) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (info.id, info.title, info.created_at.isoformat(), info.updated_at.isoformat(),
                 info.model_id, info.permission_mode, info.last_run_status.value, info.reasoning_effort),
            )
            self._conn.commit()
        return info

    def set_summary_if_empty(self, session_id: str, summary: str) -> bool:
        """Publish metadata once without changing session ordering or its identity."""
        with self.checkpointer.lock:
            cursor = self._conn.execute(
                "UPDATE session_catalog SET summary = ? WHERE id = ? AND (summary IS NULL OR summary = '')",
                (summary, session_id),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def touch(
        self,
        session_id: str,
        *,
        title: str | None = None,
        model_id: str | None = None,
        permission_mode: str | None = None,
        last_run_status: StopReason | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        now = _utc_now().isoformat()
        with self.checkpointer.lock:
            row = self._conn.execute(
                "SELECT title, model_id, permission_mode, last_run_status, reasoning_effort FROM session_catalog WHERE id = ?", (session_id,),
            ).fetchone()
            if row is None:
                return
            new_title = title if title is not None else row[0]
            self._conn.execute(
                "UPDATE session_catalog SET title = ?, updated_at = ?, model_id = ?, permission_mode = ?, last_run_status = ?, reasoning_effort = ? WHERE id = ?",
                (new_title, now, model_id if model_id is not None else row[1],
                 permission_mode if permission_mode is not None else row[2],
                 last_run_status.value if last_run_status is not None else row[3],
                 reasoning_effort if reasoning_effort is not None else row[4], session_id),
            )
            self._conn.commit()

    def set_pending_config(
        self, session_id: str, *, model_id: str | None, permission_mode: str | None,
        reasoning_effort: str | None = None,
    ) -> None:
        with self.checkpointer.lock:
            self._conn.execute(
                "UPDATE session_catalog SET pending_model_id = ?, pending_permission_mode = ?, pending_reasoning_effort = ?, updated_at = ? WHERE id = ?",
                (model_id, permission_mode, reasoning_effort, _utc_now().isoformat(), session_id),
            )
            self._conn.commit()

    def append_model_metrics(self, session_id: str, metrics: dict[str, Any]) -> None:
        payload = json.dumps(metrics, ensure_ascii=False, allow_nan=False)
        with self.checkpointer.lock:
            self._conn.execute(
                "INSERT INTO session_model_metrics (session_id, metrics_json) "
                "SELECT id, ? FROM session_catalog WHERE id = ?",
                (payload, session_id),
            )
            self._conn.commit()

    def model_metrics_history(self, session_id: str) -> list[dict[str, Any]]:
        with self.checkpointer.lock:
            rows = self._conn.execute(
                "SELECT metrics_json FROM session_model_metrics WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_turn_timing(
        self, session_id: str, message_id: str, elapsed_seconds: float, finished_at: datetime,
    ) -> None:
        with self.checkpointer.lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO session_turn_timings "
                "(session_id, message_id, elapsed_seconds, finished_at) "
                "SELECT id, ?, ?, ? FROM session_catalog WHERE id = ?",
                (message_id, elapsed_seconds, finished_at.isoformat(), session_id),
            )
            self._conn.commit()

    def turn_timings(self, session_id: str) -> dict[str, tuple[float, datetime]]:
        with self.checkpointer.lock:
            rows = self._conn.execute(
                "SELECT message_id, elapsed_seconds, finished_at FROM session_turn_timings WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        return {row[0]: (row[1], _parse_dt(row[2])) for row in rows}

    def list_sessions(self, *, limit: int = 50) -> list[SessionInfo]:
        with self.checkpointer.lock:
            rows = self._conn.execute(
                "SELECT id, title, created_at, updated_at, model_id, permission_mode, last_run_status, pending_model_id, pending_permission_mode, reasoning_effort, pending_reasoning_effort, summary "
                "FROM session_catalog ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_session_info(row) for row in rows]

    def get(self, session_id: str) -> SessionInfo | None:
        with self.checkpointer.lock:
            row = self._conn.execute(
                "SELECT id, title, created_at, updated_at, model_id, permission_mode, last_run_status, pending_model_id, pending_permission_mode, reasoning_effort, pending_reasoning_effort, summary FROM session_catalog WHERE id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return _session_info(row)

    def resolve_prefix(self, prefix: str) -> SessionInfo | None:
        prefix = prefix.strip()
        if not prefix:
            return None
        exact = self.get(prefix)
        if exact is not None:
            return exact
        with self.checkpointer.lock:
            rows = self._conn.execute(
                "SELECT id, title, created_at, updated_at, model_id, permission_mode, last_run_status, pending_model_id, pending_permission_mode, reasoning_effort, pending_reasoning_effort, summary FROM session_catalog WHERE id LIKE ?",
                (f"{prefix}%",),
            ).fetchall()
        if len(rows) != 1:
            return None
        return _session_info(rows[0])


def _session_info(row: Any) -> SessionInfo:
    reason = StopReason(row[6])
    return SessionInfo(
        id=row[0], title=row[1], created_at=_parse_dt(row[2]), updated_at=_parse_dt(row[3]),
        status=_status_for_reason(reason), model_id=row[4], permission_mode=row[5], last_run_status=reason,
        pending_model_id=row[7], pending_permission_mode=row[8],
        reasoning_effort=row[9], pending_reasoning_effort=row[10], summary=row[11],
    )


def _status_for_reason(reason: StopReason) -> SessionStatus:
    if reason is StopReason.PENDING:
        return "running"
    if reason is StopReason.STOP:
        return "completed"
    if reason is StopReason.ERROR:
        return "failed"
    if reason is StopReason.ABORTED:
        return "cancelled"
    if reason is StopReason.DEFERRED:
        return "waiting"
    raise KeyError(reason)
