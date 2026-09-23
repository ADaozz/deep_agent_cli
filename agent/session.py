"""SQLite session catalog + LangGraph SqliteSaver for local persistence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import os
from pathlib import Path
import sqlite3
from typing import Any, Literal
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.attachments import (
    AttachmentCleanupResult,
    AttachmentStore,
    ImageAttachmentRef,
    find_attachment_storage_keys,
    refs_from_message,
)

SessionStatus = Literal[
    "running", "completed", "waiting", "cancelled", "failed", "interrupted",
]


class StopReason(StrEnum):
    PENDING = "pending"
    STOP = "stop"
    LENGTH = "length"
    TOOL_USE = "tool_use"
    ERROR = "error"
    ABORTED = "aborted"
    DEFERRED = "deferred"

_CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS session_catalog (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    model_id TEXT,
    permission_mode TEXT,
    last_run_status TEXT
);
CREATE INDEX IF NOT EXISTS session_catalog_updated_idx
    ON session_catalog (updated_at DESC);
"""


@dataclass(frozen=True)
class SessionInfo:
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    status: SessionStatus
    model_id: str | None = None
    permission_mode: str | None = None
    last_run_status: StopReason = StopReason.PENDING


@dataclass
class TranscriptBlock:
    kind: str
    content: str = ""
    thinking: str = ""
    tool_call_id: str = ""
    name: str = ""
    arguments: dict[str, Any] | None = None
    is_error: bool = False
    status: str = ""
    attachments: tuple[ImageAttachmentRef, ...] = ()


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
        self._conn.executescript(_CATALOG_DDL)
        existing_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(session_catalog)")}
        for name in ("model_id", "permission_mode", "last_run_status"):
            if name not in existing_columns:
                self._conn.execute(f"ALTER TABLE session_catalog ADD COLUMN {name} TEXT")
        self._conn.commit()
        if self.path.exists():
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self.checkpointer = SqliteSaver(self._conn, serde=_strict_serde())
        self.attachment_store = AttachmentStore.beside_database(self.path)
        self.startup_cleanup: AttachmentCleanupResult | None = None
        try:
            self.startup_cleanup = self.cleanup_attachments()
        except Exception:  # noqa: BLE001 - persistence must remain usable if maintenance fails
            self.startup_cleanup = None
        self.attachment_store.acquire_runtime_lease()

    @classmethod
    def for_workspace(
        cls,
        workspace: Path,
        *,
        override: str | Path | None = None,
    ) -> "SessionStore":
        return cls(workspace_state_path(workspace, override=override))

    def close(self) -> None:
        self.attachment_store.close()
        self._conn.close()

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
        )
        with self.checkpointer.lock:
            self._conn.execute(
                "INSERT INTO session_catalog (id, title, created_at, updated_at, status, model_id, permission_mode, last_run_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (info.id, info.title, info.created_at.isoformat(), info.updated_at.isoformat(), info.status,
                 info.model_id, info.permission_mode, info.last_run_status.value),
            )
            self._conn.commit()
        return info

    def touch(
        self,
        session_id: str,
        *,
        status: SessionStatus | None = None,
        title: str | None = None,
        model_id: str | None = None,
        permission_mode: str | None = None,
        last_run_status: StopReason | None = None,
    ) -> None:
        now = _utc_now().isoformat()
        with self.checkpointer.lock:
            row = self._conn.execute(
                "SELECT title, status, model_id, permission_mode, last_run_status FROM session_catalog WHERE id = ?", (session_id,),
            ).fetchone()
            if row is None:
                return
            new_title = title if title is not None else row[0]
            new_status = status if status is not None else row[1]
            self._conn.execute(
                "UPDATE session_catalog SET title = ?, updated_at = ?, status = ?, model_id = ?, permission_mode = ?, last_run_status = ? WHERE id = ?",
                (new_title, now, new_status, model_id if model_id is not None else row[2],
                 permission_mode if permission_mode is not None else row[3],
                 last_run_status.value if last_run_status is not None else row[4], session_id),
            )
            self._conn.commit()

    def list_sessions(self, *, limit: int = 50) -> list[SessionInfo]:
        with self.checkpointer.lock:
            rows = self._conn.execute(
                "SELECT id, title, created_at, updated_at, status, model_id, permission_mode, last_run_status "
                "FROM session_catalog ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_session_info(row) for row in rows]

    def get(self, session_id: str) -> SessionInfo | None:
        with self.checkpointer.lock:
            row = self._conn.execute(
                "SELECT id, title, created_at, updated_at, status, model_id, permission_mode, last_run_status FROM session_catalog WHERE id = ?",
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
                "SELECT id, title, created_at, updated_at, status, model_id, permission_mode, last_run_status FROM session_catalog WHERE id LIKE ?",
                (f"{prefix}%",),
            ).fetchall()
        if len(rows) != 1:
            return None
        return _session_info(rows[0])


def _session_info(row: Any) -> SessionInfo:
    raw_reason = row[7] or StopReason.PENDING.value
    try:
        reason = StopReason(raw_reason)
    except ValueError:
        reason = StopReason.PENDING
    return SessionInfo(
        id=row[0], title=row[1], created_at=_parse_dt(row[2]), updated_at=_parse_dt(row[3]),
        status=row[4], model_id=row[5], permission_mode=row[6], last_run_status=reason,
    )


def messages_to_transcript(messages: list[BaseMessage]) -> list[TranscriptBlock]:
    """Rebuild a presentation-neutral transcript from LangGraph checkpoint messages."""
    blocks: list[TranscriptBlock] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            blocks.append(TranscriptBlock(
                kind="user",
                content=_message_text(message),
                attachments=refs_from_message(message),
            ))
        elif isinstance(message, AIMessage):
            thinking = _reasoning_text(message)
            text = _visible_text(message)
            if thinking or text:
                blocks.append(TranscriptBlock(kind="assistant", content=text, thinking=thinking))
            for call in message.tool_calls or []:
                blocks.append(TranscriptBlock(
                    kind="tool",
                    tool_call_id=str(call.get("id") or ""),
                    name=str(call.get("name") or "tool"),
                    arguments=call.get("args") if isinstance(call.get("args"), dict) else {},
                    status="running",
                ))
        elif isinstance(message, ToolMessage):
            content = _message_text(message)
            tool_call_id = str(getattr(message, "tool_call_id", "") or "")
            status = str(getattr(message, "status", "") or "")
            is_error = status == "error" or content.lower().startswith("error")
            updated = False
            for block in reversed(blocks):
                if block.kind == "tool" and block.tool_call_id == tool_call_id:
                    block.content = content
                    block.is_error = is_error
                    block.status = "error" if is_error else "completed"
                    updated = True
                    break
            if not updated:
                blocks.append(TranscriptBlock(
                    kind="tool",
                    tool_call_id=tool_call_id,
                    name=str(getattr(message, "name", "") or "tool"),
                    content=content,
                    is_error=is_error,
                    status="error" if is_error else "completed",
                ))
    return blocks


def _message_text(message: BaseMessage) -> str:
    text = getattr(message, "text", None)
    if isinstance(text, str) and text:
        return text
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts)


def _reasoning_text(message: AIMessage) -> str:
    additional = getattr(message, "additional_kwargs", None) or {}
    reasoning = additional.get("reasoning_content") or additional.get("reasoning")
    if isinstance(reasoning, str):
        return reasoning
    content = getattr(message, "content", "")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in {"reasoning", "thinking"}:
                parts.append(str(block.get("text") or block.get("reasoning") or ""))
        return "".join(parts)
    return ""


def _visible_text(message: AIMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts)
