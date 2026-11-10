"""Model profile value object and its domain invariants."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


DEFAULT_STREAM_CHUNK_TIMEOUT = 120.0
DEFAULT_REQUEST_TIMEOUT = 60.0
InputKind = Literal["text", "image"]


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_retries", _model_max_retries(self.max_retries, field_name="max_retries"))
        object.__setattr__(
            self, "stream_chunk_timeout",
            _model_stream_chunk_timeout(self.stream_chunk_timeout, field_name="stream_chunk_timeout"),
        )

    def supports_input(self, kind: InputKind) -> bool:
        return kind in self.input


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
