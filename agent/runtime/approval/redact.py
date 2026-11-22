"""Redact secrets from classifier evidence before remote review."""
from __future__ import annotations

import re
from collections.abc import Iterable

from agent.redaction import REDACTED, replace_known_secrets

_PLACEHOLDER = re.compile(r"\[[a-z_]+(?::\d+)?\]|\[REDACTED\]")

# Obvious secret-bearing patterns. Matched values are replaced with typed placeholders.
_SECRET_PATTERNS = (
    ("bearer_token", re.compile(r"(?i)\b(authorization\s*[:=]\s*)(bearer\s+\S+)")),
    ("api_key", re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|secret[_-]?key|client[_-]?secret)\s*[:=]\s*)([^\s\"',}]+)"
    )),
    ("cookie", re.compile(r"(?i)((?:cookie|set-cookie)\s*[:=]\s*)([^\s\"';]+)")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("aws_key", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
    ("github_pat", re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{20,})\b")),
    ("slack_token", re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})\b")),
)

_UNSAFE_HINTS = re.compile(
    r"(?i)("
    r"authorization\s*[:=]\s*bearer\s+\S{8,}"
    r"|api[_-]?key\s*[:=]\s*\S{8,}"
    r"|access[_-]?token\s*[:=]\s*\S{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"
    r"|gh[pousr]_[A-Za-z0-9_]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r")"
)


def _replace_patterns(text: str) -> str:
    for kind, pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m, k=kind: f"{m.group(1)}[{k}:{len(m.group(2))}]", text)
        else:
            text = pattern.sub(lambda m, k=kind: f"[{k}:{len(m.group(0))}]", text)
    return text


def _without_placeholders(text: str) -> str:
    return _PLACEHOLDER.sub(" ", text)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace known secrets and obvious credential patterns with typed placeholders."""
    redacted = replace_known_secrets(text, secrets)
    return _replace_patterns(redacted)


def redact_for_classifier(text: str, secrets: Iterable[str] = ()) -> str | None:
    """Return redacted evidence, or None when residual secrets make remote review unsafe."""
    redacted = redact_text(text, secrets)
    for secret in secrets:
        if secret and secret in redacted:
            return None
    if _UNSAFE_HINTS.search(_without_placeholders(redacted)):
        return None
    return redacted


def payload_looks_sensitive(text: str, secrets: Iterable[str] = ()) -> bool:
    """True when outbound tool arguments appear to carry secrets."""
    if any(secret and secret in text for secret in secrets):
        return True
    return _UNSAFE_HINTS.search(text) is not None or any(p.search(text) for _, p in _SECRET_PATTERNS)
