"""Exact credential replacement and shared display-text sanitization."""

from __future__ import annotations

from collections.abc import Iterable
import re
from typing import Any

from agent.execution.layout import sandbox_environment
from agent.execution.policy import SAFE_INHERITED_ENV, SandboxConfig


REDACTED = "[REDACTED]"
LEGACY_HIDDEN = "[已隐藏]"
REDACTION_MARKERS = (REDACTED, LEGACY_HIDDEN)

# Allowlisted variables that describe the host rather than carry a credential.
_PLAIN_ENV_NAMES = frozenset({*SAFE_INHERITED_ENV, "PATH", "HOME", "PWD", "TMPDIR", "USER", "LOGNAME", "SHELL"})

# Covers OSC/DCS/CSI and other C0 ESC forms used by untrusted tool/MCP text.
_CONTROL_SEQUENCE = re.compile(
    r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c|$)"
    r"|\x1b[P^_].*?(?:\x1b\\|$)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|\x1b[@-_]",
    re.DOTALL,
)


def known_secrets(
    settings: Any | None,
    deepseek_api_key: str | None = None,
    sandbox: SandboxConfig | None = None,
    *,
    min_length: int = 1,
) -> tuple[str, ...]:
    """Collect configured credential values, plus the values injected into ``sandbox`` when given.

    ``min_length`` drops short values that would otherwise blank out unrelated text.
    """
    values: list[Any] = []
    if settings is not None:
        values.extend(profile.api_key for profile in settings.llm_profiles)
        values.append(settings.resolved_deepseek_api_key)
    values.append(deepseek_api_key)
    if sandbox is not None:
        values.extend((sandbox.env_set or {}).values())
        env = sandbox_environment(sandbox)
        values.extend(env.get(name) for name in sandbox.env_allowlist if name not in _PLAIN_ENV_NAMES)
    cleaned = (str(value).strip() for value in values if value)
    return tuple(dict.fromkeys(value for value in cleaned if len(value) >= max(min_length, 1)))


def replace_known_secrets(text: str, secrets: Iterable[str]) -> str:
    unique = dict.fromkeys(secret for secret in secrets if secret)
    for secret in sorted(unique, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


def strip_control_sequences(text: str) -> str:
    """Remove ANSI/OSC/DCS control sequences from untrusted display text."""
    return _CONTROL_SEQUENCE.sub("", text)


def sanitize_display_text(text: str, *, keep_newlines: bool = True) -> str:
    """Strip control sequences and non-printable characters for terminal display."""
    text = strip_control_sequences(text)
    allowed = "\n\r\t" if keep_newlines else ""
    return "".join(c for c in text if c.isprintable() or c in allowed)


def contains_redaction_marker(text: str) -> bool:
    return any(marker in text for marker in REDACTION_MARKERS)


def strip_redaction_markers(text: str) -> str:
    for marker in REDACTION_MARKERS:
        text = text.replace(marker, "")
    return text
