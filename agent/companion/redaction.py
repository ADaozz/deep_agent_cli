"""Credential redaction for source and diagnostic evidence."""

import re
from urllib.parse import unquote_plus, urlsplit, urlunsplit


HIDDEN = "[已隐藏]"
PRIVATE_KEY_MARKER = "PRIVATE KEY-----"
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)")
_SOURCE_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[ \t]*(?:\r?\n|(?:\\r)?\\n)")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_AUTH_HEADER = re.compile(r"\b(?:proxy-)?authorization[ \t]*[:=][ \t]*(?:Bearer|Basic)[ \t]+[^\s,;]+", re.I)
_TOKEN = re.compile(
    r"\bBearer[ \t]+[A-Za-z0-9._~+/-]+=*|\b(?:gh[pousr]_[A-Za-z0-9]+|sk-[A-Za-z0-9_-]+|"
    r"AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b", re.I,
)
_SECRET_NAME = r"api[-_]?key|key|token|secret|password|passwd|authorization|credential|signature"
_SECRET_FIELD = rf"(?:[\w-]*[-_]|access|refresh|client|session|private|oauth|id)?(?:{_SECRET_NAME})"
_SECRET_QUERY = re.compile(rf"{_SECRET_FIELD}|sig|auth|code|jwt|sid|session|x-amz-.*|x-goog-.*", re.I)
_SECRET_OPTION = re.compile(
    rf"(--{_SECRET_FIELD})(?:[ \t]+|=)"
    r"""(?:"[^"\r\n]*(?:"|$)|'[^'\r\n]*(?:'|$)|[^\s;]+)""", re.I,
)
_ASSIGNMENT = re.compile(
    rf"""(?<![\w?&;])((?:{_SECRET_FIELD}|密钥|令牌|密码)["']?\s*[:=：]\s*)"""
    r"""(?:"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|[^\s,;]+)""", re.I,
)
_SOURCE_ASSIGNMENT = re.compile(
    rf"""(?<!\w)({_SECRET_FIELD}["']?\s*[:=]\s*)"""
    r"""(?:"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|[\w!@%+./~=:?&-]+(?=[ \t]*(?:$|#|[,;}])))(?![\w(])""",
    re.I | re.M,
)
_URL = r"""https?://[^\s<>"']+"""
_ABSOLUTE_PATH = r"""(?<![\w])(?:[A-Za-z]:[\\/]|\\\\|~/|/)[^\s<>"'，。；]+"""
_DIAGNOSTIC_LOCATION = re.compile(rf"(?P<url>{_URL})|(?P<path>{_ABSOLUTE_PATH})", re.I)
_SOURCE_URL = re.compile(_URL, re.I)
_TECH_PATHS = re.compile(r"\b(?:ci/cd|tcp/ip|http/[23])\b", re.I)


def _redact_url(value, secrets):
    suffix = value[len(value.rstrip(".,)}，。；")):]
    value = value[:len(value) - len(suffix)]
    try:
        parts = urlsplit(value)
        # Validating the hostname and port also rejects malformed authority.
        if not parts.hostname or parts.port == 0:
            return HIDDEN + suffix
    except ValueError:
        return HIDDEN + suffix

    def sensitive(component):
        decoded = unquote_plus(component)
        return _TOKEN.search(decoded) or any(secret in decoded for secret in secrets if secret)

    authority = parts.netloc.rsplit("@", 1)[-1]
    if sensitive(authority):
        return HIDDEN + suffix
    segments = [HIDDEN if sensitive(part) else part for part in parts.path.split("/")]
    query = []
    for entry in re.split(r"[&;]", parts.query):
        if not entry:
            continue
        key, sep, param = entry.partition("=")
        if sensitive(key) or not sep:
            query.append(HIDDEN if sensitive(key) else entry)
        elif _SECRET_QUERY.fullmatch(unquote_plus(key)) or sensitive(param):
            query.append(f"{key}={HIDDEN}")
        else:
            query.append(entry)
    fragment = HIDDEN if parts.fragment else ""
    return urlunsplit((parts.scheme, authority, "/".join(segments), "&".join(query), fragment)) + suffix


def _redact_outside_locations(text, *, source):
    text = _SECRET_OPTION.sub(r"\1 " + HIDDEN, text)
    pattern = _SOURCE_ASSIGNMENT if source else _ASSIGNMENT
    text = pattern.sub(r"\1" + HIDDEN, text)
    return _TOKEN.sub(HIDDEN, text)


_DIAGNOSTIC_TOKENS = re.compile(
    rf"(?P<assignment>{_ASSIGNMENT.pattern})|(?P<option>{_SECRET_OPTION.pattern})|{_DIAGNOSTIC_LOCATION.pattern}", re.I,
)
_SOURCE_TOKENS = re.compile(
    rf"(?P<assignment>{_SOURCE_ASSIGNMENT.pattern})|(?P<url>{_SOURCE_URL.pattern})", re.I | re.M,
)


def _redact(text, secrets, *, source):
    text = _ANSI_ESCAPE.sub("", text)
    if not source:
        text = _PRIVATE_KEY.sub(HIDDEN, text)
    text = _AUTH_HEADER.sub(HIDDEN, text)
    locations = _SOURCE_TOKENS if source else _DIAGNOSTIC_TOKENS
    result = []
    end = 0
    for match in locations.finditer(text):
        result.append(_redact_outside_locations(text[end:match.start()], source=source))
        if match["url"] is not None:
            result.append(_redact_url(match[0], secrets))
        elif match.lastgroup == "path":
            result.append(match[0] if _TECH_PATHS.fullmatch(match[0]) else HIDDEN)
        else:
            result.append(_redact_outside_locations(match[0], source=source))
        end = match.end()
    result.append(_redact_outside_locations(text[end:], source=source))
    safe = "".join(result)
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            safe = safe.replace(secret, HIDDEN)
    return safe


def redact_diagnostic_text(text, secrets=()):
    return _redact(text, secrets, source=False)


def redact_source_text(text, secrets=()):
    return _redact(text, secrets, source=True)


def source_contains_private_key(text):
    return _SOURCE_PRIVATE_KEY.search(_ANSI_ESCAPE.sub("", text)) is not None
