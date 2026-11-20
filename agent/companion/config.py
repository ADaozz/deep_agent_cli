"""File paths and request budgets for Lain and its worker."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
from typing import Mapping


def companion_key(directory):
    if key := os.environ.get("COMPANION_DEEPSEEK_API_KEY"):
        return key
    try:
        fd = os.open(Path(directory) / "companion.key", os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if stat.S_ISREG(info.st_mode) and info.st_size <= 4096:
                value = stream.read(4097)
                if len(value) <= 4096:
                    return value.decode("utf-8").strip()
    except (OSError, UnicodeError):
        pass
    return ""


@dataclass(frozen=True)
class CompanionConfig:
    persona_prompt: Path = Path(__file__).with_name("persona.md")
    observation_prompt: Path = Path(__file__).with_name("observation.md")
    chat_prompt: Path = Path(__file__).with_name("chat.md")
    gifs: Mapping[str, Path] = field(default_factory=dict)
    rpc_timeout_seconds: float = 3.0
    question_timeout_seconds: float = 60.0
    source_read_lines: int = 50

    def __post_init__(self):
        for name, minimum, maximum in (
            ("rpc_timeout_seconds", 0.05, 10),
            ("question_timeout_seconds", 0.05, 180),
            ("source_read_lines", 40, 60),
        ):
            value = getattr(self, name)
            expected = (int,) if name == "source_read_lines" else (int, float)
            if type(value) not in expected or not minimum <= value <= maximum:
                raise ValueError(f"companion.{name} must be between {minimum} and {maximum}")
