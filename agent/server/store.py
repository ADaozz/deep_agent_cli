"""Service catalog and replayable events, separate from LangGraph checkpoints."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
import json
import base64
import os
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any

TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})


def serializable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return serializable(asdict(value))
    if isinstance(value, dict):
        return {str(key): serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"type": type(value).__name__}  # Do not stringify arbitrary objects/credentials.


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ServiceStore:
    def __init__(self, path: Path, secrets: tuple[str, ...] = ()) -> None:
        self.lock = RLock()
        self.secrets = tuple(sorted({value for value in secrets if value}, key=len, reverse=True))
        self.conn = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, project_id TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT '{}', interrupt_id TEXT,
                prompt TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                type TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
            CREATE INDEX IF NOT EXISTS runs_session_created ON runs(session_id, created_at DESC, id DESC);
        """)
        with self.lock:
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(runs)")}
            if "prompt" not in columns:
                self.conn.execute("ALTER TABLE runs ADD COLUMN prompt TEXT NOT NULL DEFAULT ''")
            for row in self.conn.execute("SELECT id FROM runs").fetchall():
                if self.run(row["id"])["status"] not in TERMINAL:
                    self.finish(row["id"], "interrupted", {"status": "interrupted", "error": "Service restarted; resume the session explicitly"})

    def _json(self, value: Any) -> str:
        def redact(item: Any) -> Any:
            if isinstance(item, str):
                for secret in self.secrets:
                    item = item.replace(secret, "[REDACTED]")
                return item
            if isinstance(item, dict):
                return {key: redact(value) for key, value in item.items()}
            if isinstance(item, list):
                return [redact(value) for value in item]
            return item
        return json.dumps(redact(serializable(value)), ensure_ascii=False)

    def session(self, session_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            return dict(row) if row else None

    def sessions(self, project_id: str | None) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM sessions WHERE (? IS NULL OR project_id=?) ORDER BY created_at DESC LIMIT 100",
                (project_id, project_id),
            ).fetchall()
            result = [dict(row) for row in rows]
            if result:
                ids = [row["id"] for row in result]
                marks = ",".join("?" * len(ids))
                prompts = self.conn.execute(
                    f"SELECT session_id, prompt FROM runs WHERE session_id IN ({marks}) "
                    "AND prompt != '' ORDER BY created_at ASC, id ASC",
                    ids,
                ).fetchall()
                titles: dict[str, str] = {}
                for item in prompts:
                    titles.setdefault(item["session_id"], " ".join(item["prompt"].split())[:80])
                for row in result:
                    row["title"] = titles.get(row["id"], "")
            return result

    def create_session(self, session_id: str, project_id: str) -> dict[str, Any]:
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO sessions VALUES (?,?,?)", (session_id, project_id, now()))
        return self.session(session_id)

    def delete_empty_session(self, session_id: str) -> bool:
        """Remove a session that never accepted a run. A session with runs stays."""
        with self.lock, self.conn:
            if self.conn.execute("SELECT 1 FROM runs WHERE session_id=? LIMIT 1", (session_id,)).fetchone():
                return False
            deleted = self.conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            return deleted.rowcount > 0

    def create_run(self, run_id: str, session: dict[str, Any], prompt: str = "") -> dict[str, Any]:
        stamp = now()
        stored = prompt if isinstance(prompt, str) else ""
        for secret in self.secrets:
            stored = stored.replace(secret, "[REDACTED]")
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO runs(id,session_id,project_id,status,created_at,updated_at,prompt) VALUES (?,?,?,'running',?,?,?)",
                (run_id, session["id"], session["project_id"], stamp, stamp, stored),
            )
        return self.run(run_id)

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["result"] = json.loads(result["result"])
            return result

    def first_run_at(self, session_id: str) -> str | None:
        with self.lock:
            row = self.conn.execute('SELECT MIN(created_at) FROM runs WHERE session_id=?', (session_id,)).fetchone()
            return row[0] if row else None

    def session_runs(self, session_id: str, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        after = None
        if cursor is not None:
            try:
                if len(cursor) > 512:
                    raise ValueError
                raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
                decoded = json.loads(raw)
                if set(decoded) != {"session", "time", "id"} or decoded["session"] != session_id:
                    raise ValueError
                if not isinstance(decoded["id"], str) or not 0 < len(decoded["id"]) <= 128:
                    raise ValueError
                stamp = datetime.fromisoformat(decoded["time"])
                if stamp.tzinfo is None:
                    raise ValueError
                after = (decoded["time"], decoded["id"])
            except (ValueError, TypeError, KeyError):
                raise ValueError("Invalid Run history cursor") from None
        if not 1 <= limit <= 100:
            raise ValueError("Run history limit must be between 1 and 100")
        columns = "id,session_id,project_id,status,created_at,updated_at"
        with self.lock:
            if after is None:
                rows = self.conn.execute(
                    f"SELECT {columns} FROM runs WHERE session_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
                    (session_id, limit + 1),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    f"SELECT {columns} FROM runs WHERE session_id=? AND (created_at,id)<(?,?) "
                    "ORDER BY created_at DESC,id DESC LIMIT ?", (session_id, *after, limit + 1),
                ).fetchall()
        page = [dict(row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = page[-1]
            payload = json.dumps({"session": session_id, "time": last["created_at"], "id": last["id"]}).encode()
            next_cursor = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        return {"runs": page, "next_cursor": next_cursor}

    def update(self, run_id: str, status: str, result: Any = None, interrupt_id: str | None = None) -> None:
        with self.lock, self.conn:
            self._update(run_id, status, result, interrupt_id)

    def _update(self, run_id: str, status: str, result: Any, interrupt_id: str | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET status=?,updated_at=?,result=?,interrupt_id=? WHERE id=?",
            (status, now(), self._json(result or {}), interrupt_id, run_id),
        )

    def finish(self, run_id: str, status: str, result: Any) -> None:
        """Publish a terminal snapshot and its event in one SQLite transaction.

        Keep the store lock until commit so SSE snapshots on this connection
        cannot observe the updated status before run_finished exists. Helpers
        deliberately do not commit; an event-write failure rolls back both.
        """
        if status not in TERMINAL:
            raise ValueError("finish requires a terminal status")
        with self.lock, self.conn:
            self._update(run_id, status, result)
            self._emit(run_id, "run_finished", self.run(run_id))

    def emit(self, run_id: str, kind: str, data: Any) -> None:
        with self.lock, self.conn:
            self._emit(run_id, kind, data)

    def _emit(self, run_id: str, kind: str, data: Any) -> None:
        self.conn.execute("INSERT INTO events(run_id,type,data) VALUES (?,?,?)",
                          (run_id, kind, self._json(data)))

    def events(self, run_id: str, after: int, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            return [{"id": row["id"], "type": row["type"], "data": json.loads(row["data"])}
                    for row in self.conn.execute(
                        "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
                        (run_id, after, limit),
                    ).fetchall()]

    def close(self) -> None:
        with self.lock:
            self.conn.close()
