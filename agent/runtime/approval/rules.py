"""Static allow / review / ask / deny rules before Flash review."""
from __future__ import annotations

import re
from typing import Any

from agent.execution.filesystem import WorkspaceCompositeBackend
from agent.runtime.approval.policy import MANUAL, REASONS
from agent.runtime.approval.redact import payload_looks_sensitive
from agent.runtime.approval.types import Decision

_READ_ONLY = frozenset({
    "ls", "read", "mcp_list", "read_task_log", "check_background_task",
    "request_human_input", "compact_conversation",
})

_REVIEW_TOOLS = frozenset({"execute", "delete", "write", "edit", "web_search", "web_fetch"})

# Writable workspace paths that still need semantic review.
_SENSITIVE_WRITE = re.compile(
    r"(?i)("
    r"(?:^|/)\.env(?:\.|$)|"
    r"(?:^|/)(?:credentials|secrets|service.?account)[^/]*$|"
    r"(?:^|/)\.git/hooks/|"
    r"(?:^|/)(?:authorized_keys|id_rsa|id_ed25519)(?:\.|$)|"
    r"(?:^|/)(?:\.npmrc|\.pypirc|\.netrc)(?:$|/)"
    r")"
)


def is_sensitive_write_path(path: object) -> bool:
    if not isinstance(path, str) or not path:
        return False
    return _SENSITIVE_WRITE.search(path.replace("\\", "/")) is not None


def web_static_rule(call: dict[str, Any], secrets: tuple[str, ...] = ()) -> Decision | None:
    """Allow benign public web lookups; ask/deny when outbound payload looks sensitive."""
    name = call["name"]
    args = call.get("args") or {}
    if name == "web_search":
        payload = str(args.get("query") or args.get("q") or "")
    elif name == "web_fetch":
        payload = str(args.get("url") or "")
    else:
        return MANUAL
    if not payload.strip():
        return MANUAL
    if payload_looks_sensitive(payload, secrets):
        # Known or obvious secrets must not be sent as-is.
        if any(secret and secret in payload for secret in secrets):
            return Decision("deny", REASONS["data"])
        return MANUAL
    return Decision("allow", REASONS["authorized"])


def static_rule(
    call: dict[str, Any],
    *,
    backend: Any,
    trusted: frozenset[str],
    custom: set[str],
    secrets: tuple[str, ...] = (),
) -> Decision | None:
    """Return a final Decision, or None when Flash review is required."""
    name = call["name"]
    if name in custom:
        return MANUAL
    if name in trusted:
        return Decision("allow", "受信任的 MCP 服务。")
    if name in _READ_ONLY:
        return Decision("allow", REASONS["authorized"])
    if name in {"web_search", "web_fetch"}:
        return web_static_rule(call, secrets)
    if name in {"write", "edit"} and isinstance(backend, WorkspaceCompositeBackend):
        path = call["args"].get("file_path")
        if backend.is_workspace_mutation(path):
            if is_sensitive_write_path(path):
                return None
            return Decision("allow", "授权工作区内的写入。")
    if name not in _REVIEW_TOOLS:
        return MANUAL
    return None
