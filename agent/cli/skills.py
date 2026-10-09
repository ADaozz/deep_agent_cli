"""通过原生技能加载器获取目录，并管理输入框中的紧凑技能引用。"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from deepagents.middleware.skills import SkillsMiddleware, SkillsStateUpdate
from langgraph.runtime import Runtime

if TYPE_CHECKING:
    from agent.factory import PreparedAgent


_SKILL_INSTRUCTION = re.compile(
    r"使用技能 (?P<name>[^\n]+?)，先读取 /skills/[^\n]+?/SKILL\.md 并遵循其说明。"
)


def skill_message_display(text: str) -> tuple[str, tuple[str, ...]]:
    """将提交的技能指令还原为显示块，兼容排队消息及已保存的会话。"""
    labels: list[str] = []

    def replace(match: re.Match[str]) -> str:
        label = f"[Skill: {match['name']}]"
        labels.append(label)
        return label

    displayed = _SKILL_INSTRUCTION.sub(replace, text)
    return displayed, tuple(dict.fromkeys(labels))


def load_skill_catalog(prepared: PreparedAgent) -> SkillsStateUpdate:
    loader = SkillsMiddleware(
        backend=prepared.backend, sources=prepared.skill_sources, system_prompt=None,
    )
    return loader.before_agent({"messages": []}, Runtime(), {}) or {"skills_metadata": []}


class SkillDraft:
    def __init__(self) -> None:
        self.name = ""
        self.path = ""

    @property
    def labels(self) -> tuple[str, ...]:
        return (f"[Skill: {self.name}]",) if self.name else ()

    @property
    def has_blocks(self) -> bool:
        return bool(self.name)

    def display(self, name: str, path: str) -> str:
        self.name, self.path = name, path
        return self.labels[0]

    def has_invalid_marker(self, text: str) -> bool:
        if not self.name:
            return False
        return "[Skill" in text.replace(self.labels[0], "")

    def is_present(self, text: str) -> bool:
        return bool(self.labels and self.labels[0] in text)

    def expand(self, text: str) -> str:
        if not self.is_present(text):
            return text
        instruction = f"使用技能 {self.name}，先读取 {self.path} 并遵循其说明。"
        return text.replace(self.labels[0], instruction)

    def clear(self) -> None:
        self.name = self.path = ""
