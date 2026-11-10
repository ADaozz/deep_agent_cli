"""Workspace-scoped image attachments stored outside LangGraph checkpoints."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Iterable, Iterator
from uuid import uuid4

from agent.sessions.images import (
    MAX_IMAGE_BYTES,
    _STORAGE_KEY,
    _MIME_TO_EXTENSION,
    ImageAttachment,
    ImageAttachmentRef,
    AttachmentCleanupResult,
    detect_image_mime,
    image_attachment_from_bytes,
)


def image_attachment_from_path(path: Path) -> ImageAttachment:
    if not path.is_file():
        raise ValueError(f"Image path does not exist: {path}")
    size = path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f"Image exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MiB limit: {path.name}")
    return image_attachment_from_bytes(path.read_bytes(), filename=path.name)


class AttachmentStore:
    """Content-addressed attachment files shared by all workspace sessions."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self._lock_path = self.root / ".workspace.lock"
        self._runtime_lock: Any | None = None
        self._mutex = threading.RLock()

    @classmethod
    def beside_database(cls, database: Path) -> "AttachmentStore":
        return cls(database.with_suffix(".attachments"))

    def acquire_runtime_lease(self) -> None:
        if self._runtime_lock is not None:
            return
        handle = self._lock_path.open("a+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        self._runtime_lock = handle

    def close(self) -> None:
        if self._runtime_lock is not None:
            fcntl.flock(self._runtime_lock.fileno(), fcntl.LOCK_UN)
            self._runtime_lock.close()
            self._runtime_lock = None

    def put(self, attachment: ImageAttachment) -> ImageAttachmentRef:
        if attachment.size != len(attachment.data):
            raise ValueError("Image size does not match its byte content")
        if attachment.size <= 0 or attachment.size > MAX_IMAGE_BYTES:
            raise ValueError("Image size is outside the supported range")
        detected = detect_image_mime(attachment.data)
        if attachment.mime_type != detected:
            raise ValueError(f"Image MIME mismatch: declared {attachment.mime_type}, detected {detected}")
        digest = hashlib.sha256(attachment.data).hexdigest()
        storage_key = f"{digest}.{_MIME_TO_EXTENSION[detected]}"
        target = self._path_for_key(storage_key)
        with self._mutex:
            if not target.exists():
                fd, temp_name = tempfile.mkstemp(prefix=".upload-", dir=self.root)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(attachment.data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.chmod(temp_name, 0o600)
                    os.replace(temp_name, target)
                finally:
                    try:
                        os.unlink(temp_name)
                    except FileNotFoundError:
                        pass
        return ImageAttachmentRef(
            id=f"img_{uuid4().hex}",
            filename=Path(attachment.filename).name or f"image.{_MIME_TO_EXTENSION[detected]}",
            mime_type=detected,
            size=attachment.size,
            storage_key=storage_key,
        )

    def read(self, ref: ImageAttachmentRef) -> ImageAttachment:
        path = self._path_for_key(ref.storage_key)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Attachment is missing: {ref.filename} ({ref.storage_key})") from exc
        attachment = image_attachment_from_bytes(data, filename=ref.filename)
        if attachment.mime_type != ref.mime_type or attachment.size != ref.size:
            raise ValueError(f"Attachment is corrupt or changed: {ref.filename}")
        if hashlib.sha256(data).hexdigest() != ref.storage_key.split(".", 1)[0]:
            raise ValueError(f"Attachment checksum mismatch: {ref.filename}")
        return attachment

    def cleanup(
        self,
        live_storage_keys: set[str],
        *,
        protected_storage_keys: Iterable[str] = (),
        release_runtime_lease: bool = False,
    ) -> AttachmentCleanupResult:
        """Delete unreferenced files while holding the exclusive workspace lock."""
        protected = set(protected_storage_keys)
        live = live_storage_keys | protected
        with self._mutex:
            had_runtime = release_runtime_lease and self._runtime_lock is not None
            if had_runtime:
                self.close()
            try:
                with self._maintenance_lock() as locked:
                    if not locked:
                        return AttachmentCleanupResult(0, 0, 0, 0, skipped_busy=True)
                    files = [path for path in self.root.iterdir() if path.is_file() and _STORAGE_KEY.fullmatch(path.name)]
                    deleted = 0
                    freed = 0
                    kept = 0
                    for path in files:
                        if path.name in live:
                            kept += 1
                            continue
                        size = path.stat().st_size
                        path.unlink()
                        deleted += 1
                        freed += size
                    return AttachmentCleanupResult(len(files), kept, deleted, freed)
            finally:
                if had_runtime:
                    self.acquire_runtime_lease()

    @contextmanager
    def _maintenance_lock(self) -> Iterator[bool]:
        handle = self._lock_path.open("a+b")
        locked = False
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                pass
            yield locked
        finally:
            if locked:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _path_for_key(self, storage_key: str) -> Path:
        if not _STORAGE_KEY.fullmatch(storage_key):
            raise ValueError("Invalid attachment storage key")
        path = self.root / storage_key
        if path.parent != self.root:
            raise ValueError("Attachment path escapes its store")
        return path
