"""`/mcp` uses the same InteractionController as the other slash commands."""
from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from agent.cli.interactions import InteractionController
from agent.cli.lifecycle import TaskStop
from agent.cli.mcp_controller import MCPController
from agent.mcp.manager import ServerStatus
from agent.permission import PermissionMode
from agent.runner import InterruptKind

_USAGE = "Usage: /mcp, /mcp enable, /mcp disable, /mcp add, /mcp remove <name>, /mcp test <name>, /mcp tools <name>"
_DISABLED_HINT = "MCP is disabled. Run /mcp enable first."
_MCP_OPERATION_RUNNING = "MCP operation running"


async def handle_mcp_command(app: Any, arg: str) -> None:
    manager = getattr(app.runner, "mcp", None)
    if manager is None:
        app.state.add_system("MCP is not available in this session.", error=True)
        return
    text = arg.strip()
    if not text:
        _show_mcp_status(app, manager)
        return
    command, _, rest = text.partition(" ")
    rest = rest.strip()
    if command == "enable":
        if rest:
            app.state.add_system("Usage: /mcp enable", error=True)
            return
        await _set_mcp_enabled(app, True)
        return
    if command == "disable":
        if rest:
            app.state.add_system("Usage: /mcp disable", error=True)
            return
        await _set_mcp_enabled(app, False)
        return
    if manager.config_error:
        app.state.add_system(manager.describe(), error=True)
        return
    if not manager.enabled:
        app.state.add_system(_DISABLED_HINT, error=True)
        return
    if command == "add":
        if rest:
            app.state.add_system("Usage: /mcp add", error=True)
            return
        if app.mcp_controller.busy:
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
    app.state.add_system(_USAGE, error=True)


def finish_mcp_interaction(app: Any, interaction: InteractionController, *, cancelled: bool) -> None:
    """The add-wizard draft stays on the app. It is form state, not a live connection."""
    if cancelled or str(interaction.values.get("confirm") or "") == "cancel":
        app.interaction = None
        app._mcp_draft = {}
        app.set_status("MCP add cancelled")
        return
    draft = dict(app._mcp_draft)
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
            question = "Host command, outside the sandbox. Example: python server.py"
            label = "Host command"
        else:
            question = "URL, for example https://mcp.example.com/mcp"
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
            question="Bearer uses an environment variable. OAuth opens a browser.",
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
                question="Environment variable name. Do not paste the token.",
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
        question="Unique server name.",
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
                {"value": "cancel", "label": "Cancel", "description": "No change"},
                {"value": "add", "label": "Add server", "description": "Save and connect"},
            ],
        }],
    )
    app.set_status("Confirm the host server · Enter confirm · Esc cancel", transient=False)


def _summary(draft: dict[str, str]) -> str:
    lines = [f"Name: {draft.get('name', '')}", f"Transport: {draft.get('transport', '')}"]
    if draft.get("transport") == "stdio":
        lines.append("Host command, outside the sandbox:")
        lines.append(draft.get("target") or "<missing command>")
    else:
        lines.append("URL: " + draft.get("target", ""))
        auth = draft.get("auth") or "none"
        lines.append(f"Auth: {auth}")
        if auth == "bearer":
            lines.append(f"Token variable: {draft.get('token_env', '')}")
        elif auth == "oauth":
            lines.append("OAuth opens a browser.")
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


def _start_operation(
    app: Any, operation: Any, *, quiet_success: bool = False, force: bool = False,
) -> asyncio.Task | None:
    # Reserve before scheduling: two Enter events must not launch two workers.
    # /mcp disable does not use this gate, so it can cancel an OAuth wait.
    # A new enable round may force its own connect without waiting for that work.
    controller: MCPController = app.mcp_controller
    op_id = controller.try_begin(force=force)
    if op_id is None:
        app.set_status("MCP operation still running")
        return None
    app.set_status(_MCP_OPERATION_RUNNING, transient=False)
    epoch = app.runner.mcp.switch_epoch

    async def run() -> None:
        try:
            if quiet_success:
                result = await asyncio.to_thread(operation)
            else:
                result = await app._run_blocking(operation)
            if app.runner.mcp.switch_epoch != epoch:
                # A later enable/disable owns the status text. Still publish tools
                # when this task is the one that finished the catalog change.
                if app.runner.mcp.toolset_dirty():
                    await _coalesced_publish(app)
                return
            if quiet_success:
                if not result.ok:
                    app.state.add_system(result.message, error=True)
                if app.runner.mcp.toolset_dirty():
                    await _coalesced_publish(app)
                    if app.runner.mcp.switch_epoch == epoch:
                        _announce_published_switch(app)
                _finish_quiet_success(app, op_id, epoch)
                return
            app.state.add_system(result.message, error=not result.ok)
            if not _switch_status_visible(app):
                app.set_status(
                    "MCP operation finished" if result.ok else "MCP operation failed; you can retry",
                    transient=False,
                )
            if app.runner.mcp.toolset_dirty():
                await _apply_when_idle(app)
        except Exception as exc:
            if app.runner.mcp.switch_epoch != epoch:
                return
            app.state.add_system(f"MCP operation failed: {str(exc)[:400]}", error=True)
            app.set_status("MCP operation failed; you can retry", transient=False)
        finally:
            controller.finish(op_id)
            app.application.invalidate()

    lifecycle = getattr(app, "lifecycle", None)
    if lifecycle is None:
        # Test doubles construct only the MCP surface. The real CLI always has a lifecycle.
        controller.operation = asyncio.create_task(run())
    else:
        controller.operation = lifecycle.spawn(run(), stop=TaskStop.OBSERVE, name="mcp-operation")
    return controller.operation


async def _wait_operation(app: Any, task: asyncio.Task | None) -> None:
    if task is None:
        return
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        app.state.add_system("Wait cancelled. The MCP operation is still running.")


def _apply_when_idle_worker(runner: Any) -> list[Any] | None:
    # Only the graph build runs on the worker. The caller already skips this
    # while an agent run is active. Wait out another idle publish instead of
    # treating that brief lock as a failed toolset update.
    events: list[Any] = []
    if not runner.apply_pending_config_if_idle(events.append, lock_timeout=5):
        return None
    return events


def disabled_status_text(manager: Any) -> str:
    count = len(manager.list_servers()) + len(manager.invalid_servers())
    if count == 0:
        summary = "No MCP servers are configured."
    elif count == 1:
        summary = "1 server configured."
    else:
        summary = f"{count} servers configured."
    return "\n".join([
        "MCP: Disabled",
        "",
        summary,
        "Connections and tools are inactive.",
    ])


def _queued_switch_text(enabled: bool, *, approval: bool) -> str:
    action = "enable" if enabled else "disable"
    lines = [f"MCP {action} queued until the next safe point.", "Finish the current operation first."]
    if approval:
        lines.append("Resolve the pending approval first.")
    return "\n".join(lines)


def _switch_block_reason(app: Any) -> str | None:
    if getattr(app.state, "running", False):
        return "running"
    current = app.runner.current_interrupt()
    if current is None:
        return None
    if current.kind is InterruptKind.WAITING_CONFIRMATION:
        return "approval"
    return "busy"


def _mcp_switch_lock(app: Any) -> asyncio.Lock:
    """Serialize global switch commands without covering OAuth or connect_pending."""
    return app.mcp_controller.switch_lock


async def _set_mcp_enabled(app: Any, enabled: bool) -> None:
    async with _mcp_switch_lock(app):
        manager = app.runner.mcp
        if manager.config_error:
            app.state.add_system(manager.describe(), error=True)
            return
        # Read the switch only after the lock is held, so an earlier command
        # cannot be overtaken by a later one that sampled a stale value.
        if manager.enabled is enabled:
            epoch = manager.switch_epoch
            changed = False
        else:
            result = await app._run_blocking(partial(manager.set_enabled, enabled))
            if not result.ok:
                app.state.add_system(result.message, error=True)
                app.set_status("MCP update failed", transient=False)
                return
            epoch = manager.switch_epoch
            changed = True
    if manager.switch_epoch != epoch:
        return
    if not changed:
        _show_mcp_status(app, manager)
        return
    if enabled:
        await _connect_after_enable(app, epoch)
        if manager.switch_epoch != epoch:
            return
    await _report_switch(app, enabled, epoch)


async def _connect_after_enable(app: Any, epoch: int) -> None:
    """Start this enable's connections without waiting for the previous round.

    Disable has already cancelled that round. Its task may still be unwinding,
    but the new connect_pending belongs to this epoch and must not block on it.
    """
    if app.runner.mcp.switch_epoch != epoch:
        return
    _start_operation(app, app.runner.mcp.connect_pending, quiet_success=True, force=True)


def _shown_status(app: Any) -> str:
    state = getattr(app, "state", None)
    if state is not None and getattr(state, "status", None):
        return state.status
    return getattr(app, "status", "") or ""


def _finish_quiet_success(app: Any, op_id: int, epoch: int) -> None:
    """Return the status line to idle after an automatic connect, unless a switch owns it."""
    if not app.mcp_controller.owns(op_id):
        return
    manager = getattr(app.runner, "mcp", None)
    if manager is None or manager.switch_epoch != epoch:
        return
    if getattr(app.state, "running", False):
        return
    app.mcp_controller.quiet_settled = True
    if _shown_status(app) != _MCP_OPERATION_RUNNING:
        return
    app.set_status("Ready", transient=False)


def _switch_status_visible(app: Any) -> bool:
    """True when the status line is already showing a global MCP switch."""
    texts = [getattr(app, "status", "") or ""]
    state = getattr(app, "state", None)
    if state is not None:
        texts.append(getattr(state, "status", "") or "")
    return any(
        text.startswith("MCP:") or text.startswith("MCP enable") or text.startswith("MCP disable")
        or text.startswith("MCP update failed") or text.startswith("MCP tool update")
        for text in texts
    )


def _announce_published_switch(app: Any) -> None:
    """Show the switch once the latest toolset revision is the published one."""
    manager = app.runner.mcp
    if manager.consume_switch_notice() is None:
        return
    _show_mcp_status(app, manager)


async def _coalesced_publish(app: Any) -> str:
    """Publish one toolset revision once. A second caller joins or skips it.

    A later revision, such as tools discovered after connect, still publishes.
    A failed attempt is not repeated for that same revision.
    """
    async with app.mcp_controller.publish_lock:
        manager = app.runner.mcp
        revision = manager.toolset_revision
        cached = app.mcp_controller.cached_publish(revision, dirty=manager.toolset_dirty())
        if cached is not None:
            return cached
        outcome = await _publish_mcp_toolset(app)
        app.mcp_controller.remember_publish(
            revision, outcome, current_revision=manager.toolset_revision,
        )
        return outcome


async def _publish_mcp_toolset(app: Any) -> str:
    """Apply a pending MCP toolset once. Returns applied, queued, or failed."""
    if _switch_block_reason(app) is not None:
        return "queued"
    events = await app._run_blocking(_apply_when_idle_worker, app.runner)
    if events is None:
        return "queued"
    for event in events:
        app._apply_event(event, announce=False)
    if any(getattr(event, "type", "") == "runtime_config_failed" for event in events):
        return "failed"
    if app.runner.mcp.toolset_dirty():
        return "queued"
    return "applied"


async def _report_switch(app: Any, enabled: bool, epoch: int) -> None:
    manager = app.runner.mcp
    if manager.switch_epoch != epoch:
        return
    outcome = await _coalesced_publish(app)
    if manager.switch_epoch != epoch:
        return
    if outcome == "failed":
        app.set_status("MCP tool update failed; retry at the next safe point", transient=False)
        return
    if outcome != "applied":
        reason = _switch_block_reason(app)
        app.state.add_system(_queued_switch_text(enabled, approval=reason == "approval"))
        action = "enable" if enabled else "disable"
        app.set_status(f"MCP {action} queued", transient=False)
        return
    if manager.switch_epoch != epoch:
        return
    # runtime_config_applied announces this when the app handles the event.
    # A caller that ignores the event still notifies from the published revision.
    _announce_published_switch(app)


def _show_mcp_status(app: Any, manager: Any) -> None:
    if manager.config_error:
        app.state.add_system(manager.describe(), error=True)
        app.set_status("MCP config error", transient=False)
        return
    if not manager.enabled and manager.toolset_dirty():
        reason = _switch_block_reason(app)
        app.state.add_system(_queued_switch_text(False, approval=reason == "approval"))
        app.set_status("MCP disable queued", transient=False)
        return
    if not manager.enabled:
        app.state.add_system(disabled_status_text(manager))
        app.set_status("MCP: Disabled", transient=False)
        return
    text, spans = _enabled_listing(manager)
    app.state.add_system(text, styled_spans=spans)
    app.set_status("MCP: Enabled", transient=False)


def _enabled_listing(manager: Any) -> tuple[str, tuple[tuple[int, int, str], ...]]:
    rows = [
        (
            state.config.name,
            "failed" if state.status is ServerStatus.ERROR else state.status.value,
            state.tool_count if state.status is ServerStatus.CONNECTED else 0,
        )
        for state in manager.list_servers()
    ]
    rows.extend((name, "failed", 0) for name in manager.invalid_servers())
    lines = ["MCP: Enabled", "", "Servers:"]
    spans: list[tuple[int, int, str]] = []
    if not rows:
        lines.append("  (none)")
    else:
        name_width = max(len(name) for name, _status, _count in rows)
        status_width = max(len(status) for _name, status, _count in rows)
        offset = len("\n".join(lines)) + 1
        for name, status, count in rows:
            prefix = f"  {name:<{name_width}}  "
            gap = " " * (status_width - len(status) + 2)
            line = f"{prefix}{status}{gap}{count} tools"
            color = "green" if status == "connected" else "red"
            status_start = offset + len(prefix)
            spans.append((status_start, status_start + len(status), color))
            lines.append(line)
            offset += len(line) + 1
    return "\n".join(lines), tuple(spans)


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


async def _apply_when_idle(app: Any, *, announce_status: bool = True) -> bool:
    if app.state.running:
        events = None
    else:
        events = await app._run_blocking(_apply_when_idle_worker, app.runner)
    if events is None:
        if announce_status and not _switch_status_visible(app):
            app.set_status("MCP change queued until the current run reaches a safe point", transient=False)
        return False
    for event in events:
        app._apply_event(event, announce=False)
    if any(getattr(event, "type", "") == "runtime_config_failed" for event in events):
        if announce_status and not _switch_status_visible(app):
            app.set_status("MCP tool update failed; retry at the next safe point", transient=False)
        return False
    if announce_status and not _switch_status_visible(app):
        app.set_status("MCP tools updated", transient=False)
    return not app.runner.mcp.toolset_dirty()
