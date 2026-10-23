"""`/mcp` uses the same InteractionController as the other slash commands."""
from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from agent.cli.interactions import InteractionController
from agent.mcp.manager import ServerStatus
from agent.permission import PermissionMode


async def handle_mcp_command(app: Any, arg: str) -> None:
    manager = getattr(app.runner, "mcp", None)
    if manager is None:
        app.state.add_system("MCP is not available in this session.", error=True)
        return
    text = arg.strip()
    if not text:
        _show_servers(app, manager)
        app.set_status("MCP servers")
        return
    command, _, rest = text.partition(" ")
    rest = rest.strip()
    if command == "add":
        if rest:
            app.state.add_system("Usage: /mcp add", error=True)
            return
        if getattr(app, "_mcp_busy", False):
            app.set_status("MCP operation still running")
            return
        _begin_add(app)
        return
    if command == "remove":
        if not rest or " " in rest:
            app.state.add_system("Usage: /mcp remove <name>", error=True)
            return
        await _wait_operation(app, _start_operation(app, partial(manager.remove, rest)))
        return
    if command in {"test", "tools"}:
        if not rest or " " in rest:
            app.state.add_system(f"Usage: /mcp {command} <name>", error=True)
            return
        if command == "tools":
            _show_tools(app, manager, rest)
            return
        await _wait_operation(app, _start_operation(app, partial(manager.test, rest)))
        return
    app.state.add_system("Usage: /mcp, /mcp add, /mcp remove <name>, /mcp test <name>, /mcp tools <name>", error=True)


def finish_mcp_interaction(app: Any, interaction: InteractionController, *, cancelled: bool) -> None:
    if cancelled or str(interaction.values.get("confirm") or "") == "cancel":
        app.interaction = None
        app._mcp_draft = {}
        app.set_status("MCP add cancelled")
        return
    draft = dict(getattr(app, "_mcp_draft", {}))
    kind = interaction.kind
    if kind == "mcp_name":
        draft["name"] = str(interaction.values.get("name") or "").strip()
        app._mcp_draft = draft
        app.interaction = InteractionController(
            kind="mcp_transport",
            title="Add MCP server",
            question=f"How should {draft['name']} connect?",
            fields=[{
                "id": "transport", "type": "single_select", "label": "Transport", "required": True,
                "options": [
                    {"value": "streamable_http", "label": "Streamable HTTP", "description": "Remote URL"},
                    {"value": "stdio", "label": "stdio", "description": "Command on this host"},
                ],
            }],
        )
        app.set_status("Select a transport · Enter confirm · Esc cancel", transient=False)
        return
    if kind == "mcp_transport":
        draft["transport"] = str(interaction.values.get("transport") or "")
        app._mcp_draft = draft
        if draft["transport"] == "stdio":
            question = (
                "Command and arguments. This process runs on the host, outside the sandbox. "
                "Example: python server.py --port 8000"
            )
            label = "Host command"
        else:
            question = "Streamable HTTP endpoint, for example https://mcp.example.com/mcp"
            label = "URL"
        app.interaction = InteractionController(
            kind="mcp_target",
            title="Add MCP server",
            question=question,
            fields=[{"id": "target", "type": "text", "label": label, "required": True, "options": []}],
        )
        app.set_status("Enter the target · Enter confirm · Esc cancel", transient=False)
        return
    if kind == "mcp_target":
        draft["target"] = str(interaction.values.get("target") or "").strip()
        app._mcp_draft = draft
        if draft.get("transport") == "stdio":
            _ask_confirm(app, draft)
            return
        app.interaction = InteractionController(
            kind="mcp_auth",
            title="Add MCP server",
            question="Authentication. Tokens stay in the environment; OAuth is confirmed in the browser.",
            fields=[{
                "id": "auth", "type": "single_select", "label": "Auth", "required": True,
                "options": [
                    {"value": "none", "label": "None", "description": ""},
                    {"value": "bearer", "label": "Bearer token", "description": "Read from an environment variable"},
                    {"value": "oauth", "label": "OAuth", "description": "FastMCP opens a browser"},
                ],
            }],
        )
        app.set_status("Select auth · Enter confirm · Esc cancel", transient=False)
        return
    if kind == "mcp_auth":
        draft["auth"] = str(interaction.values.get("auth") or "none")
        app._mcp_draft = draft
        if draft["auth"] == "bearer":
            app.interaction = InteractionController(
                kind="mcp_token",
                title="Add MCP server",
                question="Environment variable name that holds the bearer token. Do not paste the token.",
                fields=[{"id": "token_env", "type": "text", "label": "Environment variable", "required": True, "options": []}],
            )
            app.set_status("Enter the variable name · Enter confirm · Esc cancel", transient=False)
            return
        _ask_confirm(app, draft)
        return
    if kind == "mcp_token":
        draft["token_env"] = str(interaction.values.get("token_env") or "").strip()
        app._mcp_draft = draft
        _ask_confirm(app, draft)
        return
    if kind == "mcp_confirm":
        app.interaction = None
        app._mcp_draft = {}
        _commit_add(app, draft)
        return
    app.interaction = None
    app._mcp_draft = {}


def _begin_add(app: Any) -> None:
    if app.interaction is not None:
        app.set_status("Finish the current interaction before adding an MCP server")
        return
    app._mcp_draft = {}
    app.interaction = InteractionController(
        kind="mcp_name",
        title="Add MCP server",
        question="Server name. It must be unique. Cancel leaves the configuration unchanged.",
        fields=[{"id": "name", "type": "text", "label": "Name", "required": True, "options": []}],
    )
    app.set_status("Name the server · Enter confirm · Esc cancel", transient=False)


def _ask_confirm(app: Any, draft: dict[str, str]) -> None:
    if app.runner.permission_mode() is PermissionMode.ALLOW:
        app.interaction = None
        app._mcp_draft = {}
        _commit_add(app, draft)
        return
    app._mcp_draft = draft
    app.interaction = InteractionController(
        kind="mcp_confirm",
        title="Add this MCP server?",
        question=_summary(draft),
        fields=[{
            "id": "confirm", "type": "single_select", "label": "Save", "required": True,
            "options": [
                {"value": "cancel", "label": "Cancel", "description": "Do not change the configuration"},
                {"value": "add", "label": "Add server", "description": "Save and test the connection"},
            ],
        }],
    )
    app.set_status("Confirm the host server · Enter confirm · Esc cancel", transient=False)


def _summary(draft: dict[str, str]) -> str:
    lines = [f"Name: {draft.get('name', '')}", f"Transport: {draft.get('transport', '')}"]
    if draft.get("transport") == "stdio":
        lines.append("This command will run on the host, outside the sandbox:")
        lines.append(draft.get("target") or "<missing command>")
    else:
        lines.append("URL: " + draft.get("target", ""))
        auth = draft.get("auth") or "none"
        lines.append(f"Auth: {auth}")
        if auth == "bearer":
            lines.append(f"Token environment variable: {draft.get('token_env', '')}")
        if auth == "oauth":
            lines.append("OAuth opens a browser. The agent cannot approve it for you.")
    lines.append("Cancel does not change the configuration.")
    return "\n".join(lines)


def _commit_add(app: Any, draft: dict[str, str]) -> None:
    manager = app.runner.mcp
    transport = draft.get("transport") or ""
    fields: dict[str, Any] = {
        "name": draft.get("name") or "",
        "transport": transport,
        "auth_type": draft.get("auth") or "none",
        "token_env": draft.get("token_env") or "",
    }
    if transport == "stdio":
        fields["command"] = draft.get("target") or ""
    else:
        fields["url"] = draft.get("target") or ""
    _start_operation(app, partial(manager.add_from_fields, **fields))


def _start_operation(app: Any, operation: Any) -> asyncio.Task | None:
    # Reserve before scheduling: two Enter events must not launch two workers.
    if getattr(app, "_mcp_busy", False):
        app.set_status("MCP operation still running")
        return None
    app._mcp_busy = True
    app.set_status("MCP operation running; connections and OAuth have a timeout", transient=False)

    async def run() -> None:
        try:
            result = await app._run_blocking(operation)
            app.state.add_system(result.message, error=not result.ok)
            app.set_status("MCP operation finished" if result.ok else "MCP operation failed; you can retry", transient=False)
            if app.runner.mcp.toolset_dirty():
                await _apply_when_idle(app)
        except Exception as exc:
            app.state.add_system(f"MCP operation failed: {str(exc)[:400]}", error=True)
            app.set_status("MCP operation failed; you can retry", transient=False)
        finally:
            app._mcp_busy = False
            app.application.invalidate()

    app._mcp_operation = asyncio.create_task(run())
    return app._mcp_operation


async def _wait_operation(app: Any, task: asyncio.Task | None) -> None:
    if task is None:
        return
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        app.state.add_system(
            "MCP wait cancelled. The background operation is still running until completion or timeout."
        )


def _apply_when_idle_worker(runner: Any) -> list[Any] | None:
    # Only the graph build runs on the worker. UI updates return through the loop.
    events: list[Any] = []
    if not runner._operation_lock.acquire(blocking=False):
        return None
    try:
        if runner.current_interrupt() is not None:
            return None
        runner._apply_pending_runtime_config(events.append)
    finally:
        runner._operation_lock.release()
    return events


def _show_servers(app: Any, manager: Any) -> None:
    if manager.config_error:
        app.state.add_system(manager.describe(), error=True)
        return
    rows = [
        (
            state.config.name,
            "failed" if state.status is ServerStatus.ERROR else state.status.value,
            state.tool_count if state.status is ServerStatus.CONNECTED else 0,
        )
        for state in manager.list_servers()
    ]
    rows.extend((name, "failed", 0) for name in manager.invalid_servers())
    if not rows:
        app.state.add_system("No MCP servers are configured.")
        return
    lines: list[str] = []
    spans: list[tuple[int, int, str]] = []
    offset = 0
    for name, status, count in rows:
        prefix = f"• {name}: "
        line = f"{prefix}{status} ({count} tools)"
        color = "green" if status == "connected" else "red"
        status_start = offset + len(prefix)
        spans.extend((
            (offset, offset + 1, color),
            (status_start, status_start + len(status), color),
        ))
        lines.append(line)
        offset += len(line) + 1
    app.state.add_system("\n".join(lines), styled_spans=tuple(spans))


def _show_tools(app: Any, manager: Any, name: str) -> None:
    state = manager.get(name)
    if state is None:
        invalid = manager.invalid_message(name)
        if invalid is not None:
            app.state.add_system(invalid, error=True)
            return
        app.state.add_system(f"No MCP server named {name}.", error=True)
        return
    if not state.tools:
        app.state.add_system("\n".join([
            f"{name} is {state.status.value}. {state.error}".strip(),
            *[f"Warning: {note}" for note in state.warnings],
        ]))
        return
    lines = [f"{name} · {state.status.value} · {len(state.tools)} tools"]
    for tool in state.tools:
        summary = " ".join((tool.description or "").split())
        lines.append(f"- {tool.name}: {summary[:180]}")
    lines.extend(f"Warning: {note}" for note in state.warnings)
    app.state.add_system("\n".join(lines))


async def _apply_when_idle(app: Any) -> None:
    if app.state.running:
        events = None
    else:
        events = await app._run_blocking(_apply_when_idle_worker, app.runner)
    if events is None:
        app.set_status("MCP change queued until the current run reaches a safe point", transient=False)
        return
    for event in events:
        app._apply_event(event, announce=False)
    if any(getattr(event, "type", "") == "runtime_config_failed" for event in events):
        app.set_status("MCP tool update failed; retry at the next safe point", transient=False)
    else:
        app.set_status("MCP tools updated", transient=False)
