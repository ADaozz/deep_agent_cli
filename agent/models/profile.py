"""Model profile value object and its domain invariants."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any, Literal


DEFAULT_STREAM_CHUNK_TIMEOUT = 120.0
DEFAULT_REQUEST_TIMEOUT = 60.0
InputKind = Literal["text", "image"]

# Runtime session identity in a configured header value.
SESSION_ID_PLACEHOLDER = "{session_id}"
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


@dataclass(frozen=True)
class ModelProfile:
    """Named OpenAI-compatible chat model profile."""

    id: str
    model: str
    api_key: str = "sk-local"
    base_url: str = "http://localhost:8000/v1"
    input: tuple[InputKind, ...] = ("text",)
    api: Literal["responses", "chat_completions"] = "responses"
    # 0 = unknown, so the UI can hide the context meter instead of guessing.
    context_window: int = 0
    source: str = ""
    reasoning_efforts: tuple[str, ...] = ()
    max_retries: int = 5
    stream_chunk_timeout: float = DEFAULT_STREAM_CHUNK_TIMEOUT
    # Ordered literal request headers; ``{session_id}`` resolves per session.
    headers: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_retries", _model_max_retries(self.max_retries, field_name="max_retries"))
        object.__setattr__(
            self, "stream_chunk_timeout",
            _model_stream_chunk_timeout(self.stream_chunk_timeout, field_name="stream_chunk_timeout"),
        )
        object.__setattr__(self, "headers", _model_headers(self.headers, field_name="headers"))

    def supports_input(self, kind: InputKind) -> bool:
        return kind in self.input

    @property
    def has_session_headers(self) -> bool:
        """Whether any configured value depends on the runtime session identity."""
        return any(SESSION_ID_PLACEHOLDER in value for _, value in self.headers)

    def resolved_headers(self, session_id: str | None) -> dict[str, str]:
        """Concrete headers for one request session.

        A header that needs ``{session_id}`` is omitted while the session has no
        identity yet (for example a blank CLI draft before its first message).
        """
        if not self.headers:
            return {}
        identity = "" if session_id is None else str(session_id).strip()
        if any(char in identity for char in ("\r", "\n", "\0")):
            raise ValueError("session_id must not contain newlines or NUL characters")
        resolved: dict[str, str] = {}
        for name, value in self.headers:
            if SESSION_ID_PLACEHOLDER in value:
                if not identity:
                    continue
                value = value.replace(SESSION_ID_PLACEHOLDER, identity)
            resolved[name] = value
        return resolved


def _model_headers(value: Any, *, field_name: str) -> tuple[tuple[str, str], ...]:
    """Normalize an ordered header mapping or pair sequence; reject wire-unsafe values."""
    if value is None:
        return ()
    if isinstance(value, Mapping):
        items: Any = list(value.items())
    elif isinstance(value, (tuple, list)):
        items = list(value)
    else:
        raise ValueError(f"{field_name} must be a mapping of header names to values")
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError(f"{field_name} must be a mapping of header names to values")
        name, raw = item
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ValueError(f"{field_name} header names must be non-empty strings without surrounding spaces")
        if not _HEADER_NAME.fullmatch(name):
            raise ValueError(f"{field_name}.{name} is not a valid HTTP header name")
        if name.lower() in seen:
            raise ValueError(f"{field_name} contains duplicate header {name}")
        seen.add(name.lower())
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            raise ValueError(f"{field_name}.{name} must be a string")
        text = str(raw)
        if any(char in text for char in ("\r", "\n", "\0")):
            raise ValueError(f"{field_name}.{name} must not contain newlines or NUL characters")
        pairs.append((name, text))
    return tuple(pairs)


def _model_max_retries(value: Any, *, field_name: str) -> int:
    if value is None:
        return 5
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _model_stream_chunk_timeout(value: Any, *, field_name: str) -> float:
    """Seconds without a parsed stream chunk before the current model request retries.

    ``0`` disables the idle timeout. ``None`` keeps the 120 second default.
    """
    if value is None:
        return DEFAULT_STREAM_CHUNK_TIMEOUT
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a non-negative number of seconds")
    number = float(value)
    if not (number >= 0 and number < float("inf")):
        raise ValueError(f"{field_name} must be a non-negative number of seconds")
    return number
