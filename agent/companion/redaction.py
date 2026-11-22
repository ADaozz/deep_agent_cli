"""Companion 诊断/源码展示限制 + 已知密钥精确替换。

分层约定（已确认保留）：

1. 精确替换：``replace_known_secrets`` —— 唯一的“密钥识别”入口。
2. 展示限制（不是通用密钥猜测）：下列规则只服务 Companion 只读展示，
   避免把路径、URL 凭据段、字段赋值和 PEM 块原样送进侧栏/诊断：
   - PEM / private key 块遮盖与源码拒绝
   - ``Authorization: Bearer|Basic …`` 整段遮盖（请求头形态）
   - 字段名形如 api_key/token/password 的赋值与 ``--token`` 选项
   - URL 的 userinfo、敏感 query 名、含已知密钥的 path 段
   - 诊断文本中的绝对路径（技术词如 ci/cd 除外）
3. 明确不做：对自由文本中的 JWT / ``sk-`` / ``gh_`` / AKIA 等形态做猜测遮盖。

新输出占位符统一为 ``[REDACTED]``；读取侧仍可通过 ``agent.redaction.LEGACY_HIDDEN`` 兼容旧数据。
"""

from __future__ import annotations

import re
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from agent.redaction import REDACTED, replace_known_secrets, strip_control_sequences


PRIVATE_KEY_MARKER = "PRIVATE KEY-----"
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)"
)
_SOURCE_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[ \t]*(?:\r?\n|(?:\\r)?\\n)"
)
_AUTH_HEADER = re.compile(
    r"\b(?:proxy-)?authorization[ \t]*[:=][ \t]*(?:Bearer|Basic)[ \t]+[^\s,;]+", re.I,
)
_SECRET_NAME = r"api[-_]?key|key|token|secret|password|passwd|authorization|credential|signature"
_SECRET_FIELD = rf"(?:[\w-]*[-_]|access|refresh|client|session|private|oauth|id)?(?:{_SECRET_NAME})"
_SECRET_QUERY = re.compile(rf"{_SECRET_FIELD}|sig|auth|code|jwt|sid|session|x-amz-.*|x-goog-.*", re.I)
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


def _redact_url(value: str, secrets: tuple[str, ...]) -> str:
    suffix = value[len(value.rstrip(".,)}，。；")):]
    value = value[: len(value) - len(suffix)]
    try:
        parts = urlsplit(value)
        if not parts.hostname or parts.port == 0:
            return REDACTED + suffix
    except ValueError:
        return REDACTED + suffix

    def sensitive(component: str) -> bool:
        decoded = unquote_plus(component)
        return any(secret in decoded for secret in secrets if secret)

    # user:password@host — strip credentials from authority as a display limit.
    if "@" in parts.netloc:
        authority = parts.netloc.rsplit("@", 1)[-1]
    else:
        authority = parts.netloc
    if sensitive(authority):
        return REDACTED + suffix
    segments = [REDACTED if sensitive(part) else part for part in parts.path.split("/")]
    query = []
    for entry in re.split(r"[&;]", parts.query):
        if not entry:
            continue
        key, sep, param = entry.partition("=")
        if sensitive(key) or not sep:
            query.append(REDACTED if sensitive(key) else entry)
        elif _SECRET_QUERY.fullmatch(unquote_plus(key)) or sensitive(param):
            query.append(f"{key}={REDACTED}")
        else:
            query.append(entry)
    fragment = REDACTED if parts.fragment else ""
    return urlunsplit((parts.scheme, authority, "/".join(segments), "&".join(query), fragment)) + suffix


def _redact_outside_locations(text: str, *, source: bool) -> str:
    text = _SECRET_OPTION.sub(r"\1 " + REDACTED, text)
    pattern = _SOURCE_ASSIGNMENT if source else _ASSIGNMENT
    return pattern.sub(r"\1" + REDACTED, text)


_DIAGNOSTIC_TOKENS = re.compile(
    rf"(?P<assignment>{_ASSIGNMENT.pattern})|(?P<option>{_SECRET_OPTION.pattern})|{_DIAGNOSTIC_LOCATION.pattern}",
    re.I,
)
_SOURCE_TOKENS = re.compile(
    rf"(?P<assignment>{_SOURCE_ASSIGNMENT.pattern})|(?P<url>{_SOURCE_URL.pattern})",
    re.I | re.M,
)


def _redact(text: str, secrets: tuple[str, ...] = (), *, source: bool) -> str:
    text = strip_control_sequences(text)
    if not source:
        text = _PRIVATE_KEY.sub(REDACTED, text)
    text = _AUTH_HEADER.sub(REDACTED, text)
    locations = _SOURCE_TOKENS if source else _DIAGNOSTIC_TOKENS
    result: list[str] = []
    end = 0
    for match in locations.finditer(text):
        result.append(_redact_outside_locations(text[end:match.start()], source=source))
        if match["url"] is not None:
            result.append(_redact_url(match[0], secrets))
        elif match.lastgroup == "path":
            result.append(match[0] if _TECH_PATHS.fullmatch(match[0]) else REDACTED)
        else:
            result.append(_redact_outside_locations(match[0], source=source))
        end = match.end()
    result.append(_redact_outside_locations(text[end:], source=source))
    return replace_known_secrets("".join(result), secrets)


def redact_diagnostic_text(text: str, secrets: tuple[str, ...] = ()) -> str:
    return _redact(text, tuple(secrets), source=False)


def redact_source_text(text: str, secrets: tuple[str, ...] = ()) -> str:
    return _redact(text, tuple(secrets), source=True)


def source_contains_private_key(text: str) -> bool:
    return _SOURCE_PRIVATE_KEY.search(strip_control_sequences(text)) is not None
