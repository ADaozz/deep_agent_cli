"""Service catalog and replayable events, separate from LangGraph checkpoints."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
import json
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
                result TEXT NOT NULL DEFAULT '{}', interrupt_id TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                type TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
        """)
        with self.lock, self.conn:
            for row in self.conn.execute("SELECT id FROM runs").fetchall():
                if self.run(row["id"])["status"] not in TERMINAL:
                    self.update(row["id"], "interrupted", {"status": "interrupted", "error": "Service restarted; resume the session explicitly"})
                    self.emit(row["id"], "run_finished", self.run(row["id"]))

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
            return [dict(row) for row in rows]

    def create_session(self, session_id: str, project_id: str) -> dict[str, Any]:
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO sessions VALUES (?,?,?)", (session_id, project_id, now()))
        return self.session(session_id)

    def create_run(self, run_id: str, session: dict[str, Any]) -> dict[str, Any]:
        stamp = now()
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO runs(id,session_id,project_id,status,created_at,updated_at) VALUES (?,?,?,'running',?,?)",
                (run_id, session["id"], session["project_id"], stamp, stamp),
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

    def update(self, run_id: str, status: str, result: Any = None, interrupt_id: str | None = None) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE runs SET status=?,updated_at=?,result=?,interrupt_id=? WHERE id=?",
                (status, now(), self._json(result or {}), interrupt_id, run_id),
            )

    def emit(self, run_id: str, kind: str, data: Any) -> None:
        with self.lock, self.conn:
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
