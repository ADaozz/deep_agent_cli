"""Exact named credential replacement and shared display-text sanitization."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re
from typing import Any


REDACTED = "[REDACTED]"
LEGACY_HIDDEN = "[已隐藏]"
REDACTION_MARKERS = (REDACTED, LEGACY_HIDDEN)
# Shared PEM header. Callers keep their own tails: evidence spans versus the source-open gate.
PRIVATE_KEY_HEADER = r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"

# Covers OSC/DCS/CSI and other C0 ESC forms used by untrusted tool/MCP text.
_CONTROL_SEQUENCE = re.compile(
    r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c|$)"
    r"|\x1b[P^_].*?(?:\x1b\\|$)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|\x1b[@-_]",
    re.DOTALL,
)


@dataclass(frozen=True)
class SecretRef:
    """A credential value and the display name used as its placeholder.

    ``raw=True`` keeps ``name`` as-is (config ``redaction.replacements``).
    ``raw=False`` prefixes ``$`` for config field paths like ``llm.models.deepseek.api_key``.
    """

    name: str
    value: str
    raw: bool = False

    @property
    def placeholder(self) -> str:
        if self.raw or self.name.startswith("$"):
            return self.name
        return f"${self.name}"


def _profile_api_key_name(profile: Any) -> str:
    source = str(getattr(profile, "source", "") or "").strip()
    if source == "deepseek":
        return "llm.models.deepseek.api_key"
    profile_id = str(getattr(profile, "id", "") or "")
    _, _, model_key = profile_id.partition("/")
    if source and model_key:
        return f"llm.models.{source}.models.{model_key}.api_key"
    if source:
        return f"llm.models.{source}.api_key"
    return "llm.api_key"


def _register(
    refs: list[SecretRef],
    name: str,
    value: Any,
    *,
    min_length: int,
    raw: bool = False,
) -> None:
    if value is None:
        return
    text = str(value).strip()
    if len(text) < max(min_length, 1) or not name:
        return
    cleaned = name if raw else name.lstrip("$")
    refs.append(SecretRef(name=cleaned, value=text, raw=raw))


def known_secret_refs(
    settings: Any | None,
    deepseek_api_key: str | None = None,
    sandbox: Any | None = None,
    *,
    min_length: int = 1,
) -> tuple[SecretRef, ...]:
    """Collect named refs from config API keys and ``redaction.replacements``.

    Sandbox / MCP environment values are not collected. ``sandbox`` is accepted
    for call-site compatibility and ignored. Longer values win at replace time;
    equal-length duplicates keep the first registered name.
    """
    del sandbox  # Environment variables are intentionally not redacted.
    refs: list[SecretRef] = []
    if settings is not None:
        for profile in settings.llm_profiles:
            _register(refs, _profile_api_key_name(profile), profile.api_key, min_length=min_length)
        _register(
            refs, "llm.models.deepseek.api_key", settings.resolved_deepseek_api_key,
            min_length=min_length,
        )
        # User-configured literals always register (min_length=1); they are explicit.
        for literal, display in getattr(settings, "redaction_replacements", ()) or ():
            _register(refs, display, literal, min_length=1, raw=True)
    _register(refs, "llm.models.deepseek.api_key", deepseek_api_key, min_length=min_length)
    return tuple(refs)


def known_secrets(
    settings: Any | None,
    deepseek_api_key: str | None = None,
    sandbox: Any | None = None,
    *,
    min_length: int = 1,
) -> tuple[str, ...]:
    """Credential values only (for containment checks). Prefer ``known_secret_refs`` for replacement."""
    return tuple(dict.fromkeys(ref.value for ref in known_secret_refs(
        settings, deepseek_api_key, sandbox, min_length=min_length,
    )))


def secret_values(secrets: Iterable[SecretRef] | Mapping[str, str] | Iterable[str]) -> tuple[str, ...]:
    """Extract credential values from refs, a value→name map, or a bare value list."""
    if isinstance(secrets, Mapping):
        return tuple(dict.fromkeys(str(value) for value in secrets if value))
    values: list[str] = []
    for item in secrets:
        if isinstance(item, SecretRef):
            if item.value:
                values.append(item.value)
        elif item:
            values.append(str(item))
    return tuple(dict.fromkeys(values))


def _value_to_placeholder(secrets: Iterable[SecretRef] | Mapping[str, str]) -> dict[str, str]:
    """Build value → placeholder. Equal length keeps the first name."""
    if isinstance(secrets, Mapping):
        # Mapping is value → display name; treat as raw custom names.
        sequence = [
            SecretRef(name=str(name), value=str(value), raw=True)
            for value, name in secrets.items()
            if value and name
        ]
    else:
        sequence = [ref for ref in secrets if ref.value and ref.name]
    chosen: dict[str, str] = {}
    for ref in sequence:
        if ref.value not in chosen:
            chosen[ref.value] = ref.placeholder
    return chosen


def replace_known_secrets(
    text: str,
    secrets: Iterable[SecretRef] | Mapping[str, str] = (),
) -> str:
    """Replace known credential values with named placeholders.

    ``secrets`` may be ``SecretRef`` items or a ``value → name`` mapping (raw names).
    """
    if not secrets:
        return text
    if isinstance(secrets, Mapping):
        mapping = _value_to_placeholder(secrets)
    else:
        sequence = [item for item in secrets if isinstance(item, SecretRef)]
        if not sequence:
            return text
        mapping = _value_to_placeholder(sequence)
    for value in sorted(mapping, key=len, reverse=True):
        text = text.replace(value, mapping[value])
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


__all__ = [
    "LEGACY_HIDDEN",
    "REDACTED",
    "REDACTION_MARKERS",
    "SecretRef",
    "contains_redaction_marker",
    "known_secret_refs",
    "known_secrets",
    "replace_known_secrets",
    "sanitize_display_text",
    "secret_values",
    "strip_control_sequences",
    "strip_redaction_markers",
]
