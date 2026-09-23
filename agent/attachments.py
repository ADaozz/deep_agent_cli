"""Workspace-scoped image attachments stored outside LangGraph checkpoints."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Iterable, Iterator
from uuid import uuid4


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES_PER_MESSAGE = 4
ATTACHMENT_META_KEY = "image_attachments"
_REF_TYPE = "deep-agent/image-attachment-ref"
_REF_VERSION = 1
_STORAGE_KEY = re.compile(r"^[0-9a-f]{64}\.(?:png|jpe?g|webp|gif)$")


@dataclass(frozen=True)
class ImageAttachment:
    filename: str
    mime_type: str
    data: bytes
    size: int


@dataclass(frozen=True)
class ImageAttachmentRef:
    id: str
    filename: str
    mime_type: str
    size: int
    storage_key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": _REF_TYPE,
            "version": _REF_VERSION,
            "id": self.id,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size": self.size,
            "storage_key": self.storage_key,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ImageAttachmentRef":
        if not isinstance(value, dict):
            raise ValueError("attachment reference must be a mapping")
        if value.get("type") != _REF_TYPE or value.get("version") != _REF_VERSION:
            raise ValueError("unsupported attachment reference")
        ref = cls(
            id=str(value.get("id") or ""),
            filename=str(value.get("filename") or ""),
            mime_type=str(value.get("mime_type") or ""),
            size=int(value.get("size") or 0),
            storage_key=str(value.get("storage_key") or ""),
        )
        if not ref.id or not ref.filename or not _STORAGE_KEY.fullmatch(ref.storage_key):
            raise ValueError("invalid attachment reference")
        if ref.mime_type not in _MIME_TO_EXTENSION or ref.size <= 0 or ref.size > MAX_IMAGE_BYTES:
            raise ValueError("invalid attachment reference metadata")
        return ref


@dataclass(frozen=True)
class AttachmentCleanupResult:
    scanned: int
    kept: int
    deleted: int
    freed_bytes: int
    skipped_busy: bool = False


_MIME_TO_EXTENSION = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def detect_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    raise ValueError("Unsupported image format; use PNG, JPEG, WebP, or GIF")


def image_attachment_from_bytes(data: bytes, *, filename: str) -> ImageAttachment:
    size = len(data)
    if size <= 0:
        raise ValueError("Image is empty")
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f"Image exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MiB limit")
    mime_type = detect_image_mime(data)
    return ImageAttachment(filename=Path(filename).name or "image", mime_type=mime_type, data=data, size=size)


def image_attachment_from_path(path: Path) -> ImageAttachment:
    if not path.is_file():
        raise ValueError(f"Image path does not exist: {path}")
    size = path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f"Image exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MiB limit: {path.name}")
    return image_attachment_from_bytes(path.read_bytes(), filename=path.name)


def refs_to_dicts(refs: Iterable[ImageAttachmentRef]) -> list[dict[str, Any]]:
    return [ref.to_dict() for ref in refs]


def refs_from_message(message: Any) -> tuple[ImageAttachmentRef, ...]:
    additional = getattr(message, "additional_kwargs", None)
    raw = additional.get(ATTACHMENT_META_KEY, []) if isinstance(additional, dict) else []
    if not isinstance(raw, list):
        return ()
    refs: list[ImageAttachmentRef] = []
    for item in raw:
        try:
            refs.append(ImageAttachmentRef.from_dict(item))
        except (TypeError, ValueError):
            continue
    return tuple(refs)


def find_attachment_storage_keys(value: Any) -> set[str]:
    """Recursively collect only versioned, valid attachment references."""
    found: set[str] = set()
    stack = [value]
    seen: set[int] = set()
    while stack:
        item = stack.pop()
        if isinstance(item, (dict, list, tuple, set)) or hasattr(item, "__dict__"):
            identity = id(item)
            if identity in seen:
                continue
            seen.add(identity)
        if isinstance(item, dict):
            try:
                found.add(ImageAttachmentRef.from_dict(item).storage_key)
                continue
            except (TypeError, ValueError):
                stack.extend(item.values())
        elif isinstance(item, (list, tuple, set)):
            stack.extend(item)
        elif hasattr(item, "model_dump"):
            try:
                stack.append(item.model_dump())
            except Exception:  # noqa: BLE001
                continue
        elif hasattr(item, "__dict__"):
            stack.append(vars(item))
    return found


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
