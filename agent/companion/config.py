"""File paths for the Lain sidebar and its worker."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class CompanionConfig:
    persona_prompt: Path = Path(__file__).with_name("persona.md")
    observation_prompt: Path = Path(__file__).with_name("observation.md")
    chat_prompt: Path = Path(__file__).with_name("chat.md")
    gifs: Mapping[str, Path] = field(default_factory=dict)
