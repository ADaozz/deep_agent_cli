"""Private, bounded JSONL event storage, separate from conversation checkpoints."""
from __future__ import annotations

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
    def __init__(self, root: Path, secrets: tuple[str, ...] = ()) -> None:
        self.root = root
        self.secrets = tuple(value for value in secrets if value)
        self._lock = threading.Lock()

    def _directory(self, parent: str) -> Path:
        # Parent IDs are opaque, never paths, including legacy session IDs.
        return self.root / hashlib.sha256(parent.encode()).hexdigest()

    def _redact(self, text: str) -> str:
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

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
            record = dict(timestamp=record["timestamp"], run_id=record["run_id"], event=record["event"],
                          truncated=True, status=record.get("status"), model=record.get("model"),
                          preview=encoded.encode()[:MAX_EVENT_BYTES // 4].decode(errors="ignore"))
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
            paths = sorted(directory.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
            return {"runs": [path.stem for path in paths[offset:offset + limit] if _ID.fullmatch(path.stem)]}
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
