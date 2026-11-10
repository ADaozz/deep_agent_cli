"""Shared editor and interaction state used by terminal use cases."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from agent.cli.interactions import InteractionController
from agent.cli.pasted_content import PastedContentDraft
from agent.cli.skills import SkillDraft


@dataclass
class DialogState:
    current: InteractionController | None = None
    deferred: InteractionController | None = None
    reviewing: bool = False
    approval_draft: Any = None
    skill_picker_draft: tuple[str, int] | None = None

@dataclass
class InputDrafts:
    pasted: PastedContentDraft = field(default_factory=PastedContentDraft)
    skill: SkillDraft = field(default_factory=SkillDraft)

@dataclass
class RunPresentation:
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task[None] | None = None
    epoch: int = 0
    cancel_sent: bool = False
    send_queue_after_cancel: bool = False
    last_queue_enter: float | None = None
    compacting: bool = False
