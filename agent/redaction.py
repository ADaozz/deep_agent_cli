"""Exact replacement of credentials explicitly supplied by the caller."""

from collections.abc import Iterable
from typing import Any

from agent.execution.layout import sandbox_environment
from agent.execution.policy import SAFE_INHERITED_ENV, SandboxConfig


REDACTED = "[REDACTED]"

# Allowlisted variables that describe the host rather than carry a credential.
_PLAIN_ENV_NAMES = frozenset({*SAFE_INHERITED_ENV, "PATH", "HOME", "PWD", "TMPDIR", "USER", "LOGNAME", "SHELL"})


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
