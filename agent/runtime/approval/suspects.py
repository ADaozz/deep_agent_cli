"""Read-only heuristics for suspected credentials in Flash evidence.

These patterns never rewrite text; they only annotate spans for the reviewer.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from agent.redaction import PRIVATE_KEY_HEADER

_EXCERPT = 120

_PRIVATE_KEY = re.compile(
    PRIVATE_KEY_HEADER + r"[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)"
)
_AUTH_HEADER = re.compile(
    r"\b(?:proxy-)?authorization[ \t]*[:=][ \t]*(?:Bearer|Basic)[ \t]+[^\s,;]+", re.I,
)
_SECRET_NAME = r"api[-_]?key|key|token|secret|password|passwd|authorization|credential|signature"
_SECRET_FIELD = rf"(?:[\w-]*[-_]|access|refresh|client|session|private|oauth|id)?(?:{_SECRET_NAME})"
_SECRET_OPTION = re.compile(
    rf"(--{_SECRET_FIELD})(?:[ \t]+|=)"
    r"""(?:"[^"\r\n]*(?:"|$)|'[^'\r\n]*(?:'|$)|[^\s;]+)""",
    re.I,
)
_ASSIGNMENT = re.compile(
    rf"""(?<![\w?&;])((?:{_SECRET_FIELD}|密钥|令牌|密码)["']?\s*[:=：]\s*)"""
    r"""(?:"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|[^\s,;]+)""",
    re.I,
)
_URL = re.compile(r"""https?://[^\s<>"']+""", re.I)
_SECRET_QUERY = re.compile(
    rf"{_SECRET_FIELD}|sig|auth|code|jwt|sid|session|x-amz-.*|x-goog-.*", re.I,
)
_MAX_SUSPECTS = 32


def _excerpt(text: str, start: int, end: int) -> str:
    chunk = text[start:end]
    if len(chunk) <= _EXCERPT:
        return chunk
    return chunk[:_EXCERPT] + "…"


def _add(
    findings: list[dict[str, Any]], *, kind: str, start: int, end: int, text: str,
) -> None:
    if len(findings) >= _MAX_SUSPECTS:
        return
    findings.append({
        "kind": kind,
        "start": start,
        "end": end,
        "excerpt": _excerpt(text, start, end),
    })


def scan_suspects(text: str) -> list[dict[str, Any]]:
    """Return suspected credential-like spans without modifying ``text``."""
    if not text:
        return []
    findings: list[dict[str, Any]] = []
    for match in _PRIVATE_KEY.finditer(text):
        _add(findings, kind="private_key", start=match.start(), end=match.end(), text=text)
    for match in _AUTH_HEADER.finditer(text):
        _add(findings, kind="auth_header", start=match.start(), end=match.end(), text=text)
    for match in _SECRET_OPTION.finditer(text):
        _add(findings, kind="secret_option", start=match.start(), end=match.end(), text=text)
    for match in _ASSIGNMENT.finditer(text):
        _add(findings, kind="secret_assignment", start=match.start(), end=match.end(), text=text)
    for match in _URL.finditer(text):
        raw = match.group(0)
        try:
            parts = urlsplit(raw.rstrip(".,)}，。；"))
        except ValueError:
            continue
        if "@" in (parts.netloc or ""):
            _add(findings, kind="url_userinfo", start=match.start(), end=match.end(), text=text)
            continue
        for entry in re.split(r"[&;]", parts.query or ""):
            if not entry:
                continue
            key, sep, _ = entry.partition("=")
            if sep and _SECRET_QUERY.fullmatch(key):
                _add(findings, kind="url_sensitive_query", start=match.start(), end=match.end(), text=text)
                break
        if len(findings) >= _MAX_SUSPECTS:
            break
    return findings


__all__ = ["scan_suspects"]
