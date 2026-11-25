"""Static Auto rules, derived from the Ask approval rules. Calls the sandbox already bounds skip review."""
from __future__ import annotations

from collections.abc import Collection
from typing import Any

from agent.execution.filesystem import WorkspaceCompositeBackend
from agent.execution.network import network_requested
from agent.runtime.approval.policy import ALLOW, MANUAL
from agent.runtime.approval.types import Decision
from agent.tools.catalog import EXECUTE_TOOL, FILE_MUTATION_TOOLS, WEB_TOOLS, WRITE_STDIN_TOOL

ALLOW_TRUSTED_MCP = Decision("allow", "受信任的 MCP 服务。")
ALLOW_WORKSPACE = Decision("allow", "工作区内的文件变更。")
ALLOW_SANDBOXED = Decision("allow", "受限沙箱内的无网命令。")

# These grants depend on state outside the call (MCP trust, path resolution, sandbox mode); recheck before running.
REVOCABLE = frozenset({ALLOW_TRUSTED_MCP, ALLOW_WORKSPACE, ALLOW_SANDBOXED})


def static_rule(
    call: dict[str, Any],
    *,
    gated: Collection[str],
    backend: Any,
    sandbox_mode: str | None,
    trusted: Collection[str] = (),
    custom: Collection[str] = (),
) -> Decision | None:
    """Return a final Decision, or None when Flash review is required.

    ``gated`` holds the tool names that need confirmation in Ask mode. Tools outside it are not gated in either mode.
    """
    name = call["name"]
    args = call.get("args") or {}
    if name in custom:
        return MANUAL
    if name in trusted:
        return ALLOW_TRUSTED_MCP
    if name not in gated or name in WEB_TOOLS:
        return ALLOW
    if name in FILE_MUTATION_TOOLS:
        in_workspace = isinstance(backend, WorkspaceCompositeBackend) and backend.is_workspace_mutation(
            args.get("file_path"))
        return ALLOW_WORKSPACE if in_workspace else None
    if name == EXECUTE_TOOL:
        return ALLOW_SANDBOXED if sandbox_mode == "restricted" and not network_requested(args) else None
    if name == WRITE_STDIN_TOOL:
        action = str(args.get("action") or "write")
        chars = str(args.get("chars") or "")
        if action == "write" and not chars:
            return ALLOW  # output poll only
        return ALLOW_SANDBOXED if sandbox_mode == "restricted" else None
    # Untrusted host MCP tools and MCP server changes take effect outside the sandbox.
    return MANUAL
