"""Image attachment values and checkpoint reference format."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable


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
