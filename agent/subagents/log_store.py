"""Private, bounded JSONL event storage, separate from conversation checkpoints."""

from __future__ import annotations

from agent.redaction import SecretRef, replace_known_secrets


from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Any


_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_EVENT_BYTES = 24_000
MAX_PAGE_BYTES = 64_000


class SubAgentLogStore:
    def __init__(self, root: Path, secrets: tuple[SecretRef, ...] = ()) -> None:
        self.root = root
        self.secrets = tuple(item for item in secrets if isinstance(item, SecretRef) and item.value)
        self._lock = threading.Lock()

    def _directory(self, parent: str) -> Path:
        # Parent IDs are opaque, never paths, including legacy session IDs.
        return self.root / hashlib.sha256(parent.encode()).hexdigest()

    def _redact(self, text: str) -> str:
        return replace_known_secrets(text, self.secrets)

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, dict):
            return {str(key): self._sanitize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._sanitize(item) for item in value]
        if value is None or isinstance(value, (int, float, bool)):
            return value
        if hasattr(value, "model_dump"):
            return self._sanitize(value.model_dump())
        return self._redact(str(value))

    def append(self, parent: str, run_id: str, event: str, **payload: Any) -> None:
        if not _ID.fullmatch(run_id):
            raise ValueError("Invalid subagent run ID")
        record = self._sanitize(dict(timestamp=datetime.now(timezone.utc).isoformat(), run_id=run_id, event=event, **payload))
        encoded = json.dumps(record, ensure_ascii=False)
        if len(encoded.encode()) > MAX_EVENT_BYTES:
            kept = {
                key: record[key]
                for key in (
                    "timestamp", "run_id", "event", "status", "mode", "parent_run_id",
                    "tool_call_id", "depth", "elapsed_seconds", "description", "model",
                )
                if key in record
            }
            kept["truncated"] = True
            kept["summary_truncated"] = True
            kept["result_summary"] = encoded.encode()[:1000].decode(errors="ignore")
            record = kept
            encoded = json.dumps(record, ensure_ascii=False)
        with self._lock:
            directory = self._directory(parent)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(directory / f"{run_id}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as output:
                output.write(encoded + "\n")
                output.flush()

    def read(self, parent: str, run_id: str | None = None, offset: int = 0, limit: int = 100) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("offset must be non-negative and limit must be 1..100")
        directory = self._directory(parent)
        if run_id is None:
            runs = self.list_runs(parent)
            return {"runs": runs[offset:offset + limit]}
        if not _ID.fullmatch(run_id):
            raise ValueError("Invalid subagent run ID")
        try:
            descriptor = os.open(directory / f"{run_id}.jsonl", os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {"error": "Subagent log not found in this parent session"}
        records, size = [], 1024
        with os.fdopen(descriptor, encoding="utf-8") as source:
            for index, line in enumerate(source):
                if index < offset:
                    continue
                if len(records) >= limit or size + len(line.encode()) > MAX_PAGE_BYTES:
                    break
                try:
                    records.append(self._sanitize(json.loads(line)))
                except json.JSONDecodeError:
                    break  # A crash may leave a partial final line.
                size += len(line.encode())
        return {"run_id": run_id, "records": records, "next_offset": offset + len(records)}

    def list_runs(self, parent: str) -> list[str]:
        """Every direct child run, newest first. Not limited to one page."""
        directory = self._directory(parent)
        if not directory.is_dir():
            return []
        paths = sorted(directory.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
        return [path.stem for path in paths if _ID.fullmatch(path.stem)]

    def task_metadata(self, parent: str, run_id: str) -> dict[str, Any] | None:
        """The start record and the last finish record, ignoring page limits.

        A start without a finish is ``unconfirmed``. It is not reported as running:
        the process that wrote the start may already be gone.
        """
        if not _ID.fullmatch(run_id):
            raise ValueError("Invalid subagent run ID")
        try:
            descriptor = os.open(self._directory(parent) / f"{run_id}.jsonl", os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        start: dict[str, Any] | None = None
        finish: dict[str, Any] | None = None
        with os.fdopen(descriptor, encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    break
                if not isinstance(record, dict):
                    continue
                event = record.get("event")
                if event == "start" and start is None:
                    start = record
                elif event == "finish":
                    finish = record
        if start is None and finish is None:
            return None
        start = start or {}
        finish = finish or {}
        recorded = str(finish.get("status") or "")
        status = recorded if recorded in {"completed", "failed", "cancelled"} else "unconfirmed"
        depth = start.get("depth")
        item: dict[str, Any] = {
            "run_id": run_id,
            "parent_run_id": str(start.get("parent_run_id") or parent),
            "tool_call_id": str(start.get("tool_call_id") or ""),
            "depth": depth if type(depth) is int and not isinstance(depth, bool) and depth > 0 else 1,
            "description": str(start.get("description") or ""),
            "mode": str(start.get("mode") or "sync"),
            "status": status,
            "elapsed_seconds": finish.get("elapsed_seconds"),
            "started_at": str(start.get("timestamp") or ""),
        }
        if finish.get("summary_truncated"):
            item["result_summary"] = str(finish.get("result_summary") or finish.get("summary") or "")
            item["summary_truncated"] = True
        elif isinstance(finish.get("result"), str):
            item["result"] = finish.get("result")
        elif isinstance(finish.get("summary"), str) and not finish.get("summary_truncated"):
            item["result"] = finish.get("summary")
        return item
