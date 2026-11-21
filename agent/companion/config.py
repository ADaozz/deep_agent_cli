"""File paths and request budgets for Lain and its worker."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


SOURCE_READ_MAX_LINES = 200


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
            ("source_read_lines", 1, SOURCE_READ_MAX_LINES),
        ):
            value = getattr(self, name)
            expected = (int,) if name == "source_read_lines" else (int, float)
            if type(value) not in expected or not minimum <= value <= maximum:
                raise ValueError(f"companion.{name} must be between {minimum} and {maximum}")
