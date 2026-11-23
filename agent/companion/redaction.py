"""Companion 诊断/源码展示：控制序列剥离 + 已知密钥命名精确替换。

分层约定：

1. 精确替换：``replace_known_secrets`` —— 将配置中的模型密钥换成 ``$llm.…`` 路径名，
   以及 ``redaction.replacements`` 中的字面量换成自定义展示名（可不带 ``$``）。
2. 明确不做：沙箱/MCP 环境变量自动脱敏；对自由文本中的 JWT / ``sk-`` / ``gh_`` / AKIA、
   绝对路径、PEM、Bearer 等形态做正则遮盖；疑似形态仅由 Auto Flash 证据扫描标注。
3. ``source_contains_private_key`` 仍用于拒绝打开含私钥的源码文件（只读门禁，不是替换）。

读取侧仍可通过 ``agent.redaction.LEGACY_HIDDEN`` / ``[REDACTED]`` 兼容历史数据。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from agent.redaction import SecretRef, replace_known_secrets, strip_control_sequences

PRIVATE_KEY_MARKER = "PRIVATE KEY-----"
_SOURCE_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[ \t]*(?:\r?\n|(?:\\r)?\\n)"
)

Secrets = Iterable[SecretRef] | Mapping[str, str] | Iterable[str]


def redact_diagnostic_text(text: str, secrets: Secrets = ()) -> str:
    return replace_known_secrets(strip_control_sequences(text), secrets)


def redact_source_text(text: str, secrets: Secrets = ()) -> str:
    return replace_known_secrets(strip_control_sequences(text), secrets)


def source_contains_private_key(text: str) -> bool:
    return _SOURCE_PRIVATE_KEY.search(strip_control_sequences(text)) is not None
