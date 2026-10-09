"""Load editable offline working messages and drive their run-local rotation."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import zip_longest
import math
from pathlib import Path
import random

import yaml


DEFAULT_MESSAGES_PATH = Path(__file__).with_suffix('.yaml')


@dataclass(frozen=True)
class WorkingMessage:
    text: str
    speaker: str = ""
    source_url: str = ""
    published_on: str | None = None

    @property
    def display(self) -> str:
        return f"{self.speaker}：{self.text}" if self.speaker else self.text


@dataclass(frozen=True)
class WorkingMessageConfig:
    interval_seconds: float
    jokes: tuple[WorkingMessage, ...]
    fgo: tuple[WorkingMessage, ...]
    warning: str = ""

    @property
    def messages(self) -> tuple[WorkingMessage, ...]:
        if not self.fgo:
            return self.jokes
        if not self.jokes:
            return self.fgo
        mixed: list[WorkingMessage] = []
        for index, pair in enumerate(zip_longest(self.jokes[::2], self.jokes[1::2])):
            mixed.extend(message for message in pair if message is not None)
            mixed.append(self.fgo[index % len(self.fgo)])
        # Keep all FGO lines reachable even when the joke list is very short.
        pairs = (len(self.jokes) + 1) // 2
        mixed.extend(self.fgo[pairs:])
        return tuple(mixed)


def _parse_entries(raw: object, name: str) -> tuple[WorkingMessage, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"{name} 必须是列表")
    entries = []
    for index, value in enumerate(raw, 1):
        item = {"text": value} if isinstance(value, str) else value
        if not isinstance(item, dict):
            raise ValueError(f"{name} 第 {index} 条必须是字符串或对象")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{name} 第 {index} 条的 text 必须是非空字符串")
        for key in ("text", "speaker", "source_url", "published_on"):
            field = item.get(key, "")
            if not isinstance(field, str) or any(ord(char) < 32 or ord(char) == 127 for char in field):
                raise ValueError(f"{name} 第 {index} 条的 {key} 必须是单行字符串")
        entries.append(WorkingMessage(
            text=text.strip(), speaker=item.get("speaker", "").strip() if name != "fgo" else "",
            source_url=item.get("source_url", ""), published_on=item.get("published_on"),
        ))
    return tuple(entries)


def _read_config(path: Path) -> WorkingMessageConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("配置必须是 YAML 对象")
    interval = raw.get("interval_seconds", 8)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval_seconds 必须是大于零的有限数字")
    return WorkingMessageConfig(
        interval_seconds=float(interval),
        jokes=_parse_entries(raw.get("jokes", []), "jokes"),
        fgo=_parse_entries(raw.get("fgo", []), "fgo"),
    )


def load_working_messages(path: Path | None = None) -> WorkingMessageConfig:
    """Seed a user file once; invalid user files fall back without being overwritten."""
    default = _read_config(DEFAULT_MESSAGES_PATH)
    if path is None:
        return default
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("x", encoding="utf-8") as target:
                    target.write(DEFAULT_MESSAGES_PATH.read_text(encoding="utf-8"))
            except FileExistsError:
                pass
        return _read_config(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return WorkingMessageConfig(
            default.interval_seconds, default.jokes, default.fgo,
            warning=f"无法加载轮播文案配置 {path}，已使用默认文案：{exc}",
        )


class WorkingMessageRotation:
    """A run-local clock, driven by the application's existing render timer."""

    def __init__(self, config: WorkingMessageConfig | None = None) -> None:
        self.config = config if config is not None else load_working_messages()
        self._messages = self.config.messages
        self._started_at: float | None = None
        self._cycle = -1
        self._previous: WorkingMessage | None = None

    def reset(self) -> None:
        self._started_at = None
        self._cycle = -1

    def _shuffle_cycle(self) -> None:
        jokes, fgo = list(self.config.jokes), list(self.config.fgo)
        random.shuffle(jokes)
        random.shuffle(fgo)
        messages = list(WorkingMessageConfig(
            self.config.interval_seconds, tuple(jokes), tuple(fgo),
        ).messages)
        previous = self._previous
        for index, message in enumerate(messages):
            if previous is not None and message.display == previous.display:
                replacement = next((
                    other for other in range(index + 1, len(messages))
                    if messages[other].display != previous.display
                ), None)
                if replacement is not None:
                    messages[index], messages[replacement] = messages[replacement], messages[index]
            previous = messages[index]
        self._messages = tuple(messages)

    def current(self, active: bool, now: float) -> WorkingMessage | None:
        if not active or not self._messages:
            self.reset()
            return None
        if self._started_at is None:
            self._started_at = now
        index = int(max(0.0, now - self._started_at) / self.config.interval_seconds)
        cycle = index // len(self._messages)
        if cycle != self._cycle:
            self._shuffle_cycle()
            self._cycle = cycle
        self._previous = self._messages[index % len(self._messages)]
        return self._previous
