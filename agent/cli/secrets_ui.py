"""`/secrets` reuses InteractionController and the shared secret field — never displays plaintext."""

from __future__ import annotations

from typing import Any

from agent.cli.interactions import InteractionController
from agent.secrets.store import (
    ManagedStorage,
    SecretExistsError,
    SecretMetadata,
    SecretScope,
    validate_secret_name,
    validate_secret_scope,
)


_USAGE = "Usage: /secrets, /secrets list, /secrets set [name], /secrets update [name], /secrets delete [name]"


def format_secrets_table(items: list[SecretMetadata]) -> str:
    """Render metadata-only credential table (never includes secret values)."""
    if not items:
        return "No managed credentials stored for this session or persistently."
    lines = ["Name  Scope  Storage  Authorized consumers"]
    for item in items:
        consumers = ", ".join(item.consumers) if item.consumers else "none"
        lines.append(f"{item.name}  {item.scope}  {item.storage}  {consumers}")
    return "\n".join(lines)


async def handle_secrets_command(app: Any, arg: str) -> None:
    text = arg.strip()
    if not text or text == "list":
        items = app.runner.list_secrets()
        app.state.add_command_panel("/secrets", format_secrets_table(items))
        return
    command, _, rest = text.partition(" ")
    rest = rest.strip()
    if command in {"set", "update"}:
        if " " in rest:
            app.state.add_system(f"Usage: /secrets {command} [name]", error=True)
            return
        if app.dialogs.current is not None:
            app.set_status("Finish the current interaction before managing credentials")
            return
        if command == "update" and not rest:
            _begin_pick_for_update(app)
            return
        if rest:
            try:
                name = validate_secret_name(rest)
            except ValueError as exc:
                app.state.add_system(str(exc), error=True)
                return
            existing = app.runner.secret_store.get_metadata(name, session_id=app.runner.thread_id)
            if existing is not None:
                _open_secret_value_prompt(app, name=name, scope=existing.scope, default_storage=existing.storage)
                return
            _begin_scope_prompt(app, name=name)
            return
        _begin_name_prompt(app)
        return
    if command == "delete":
        if " " in rest:
            app.state.add_system("Usage: /secrets delete [name]", error=True)
            return
        if app.dialogs.current is not None:
            app.set_status("Finish the current interaction before managing credentials")
            return
        if not rest:
            _begin_pick_for_delete(app)
            return
        try:
            name = validate_secret_name(rest)
        except ValueError as exc:
            app.state.add_system(str(exc), error=True)
            return
        existing = [item for item in app.runner.list_secrets() if item.name == name]
        if not existing:
            app.state.add_system(f"No managed credential named {name}.", error=True)
            return
        _begin_delete_confirm(app, name, existing)
        return
    app.state.add_system(_USAGE, error=True)


def finish_secrets_interaction(
    app: Any,
    interaction: InteractionController,
    *,
    cancelled: bool,
) -> None:
    if cancelled:
        interaction.clear_secrets()
        app._clear_secret_buffer()
        app.dialogs.current = None
        app._secret_draft = {}
        app.set_status("Credential action cancelled")
        return

    kind = interaction.kind
    draft = dict(getattr(app, "_secret_draft", {}) or {})

    if kind == "secret_name":
        raw_name = str(interaction.values.get("name") or "").strip()
        try:
            name = validate_secret_name(raw_name)
        except ValueError as exc:
            interaction.error = str(exc)
            app.dialogs.current = interaction
            return
        existing = app.runner.secret_store.get_metadata(name, session_id=app.runner.thread_id)
        if existing is not None:
            _open_secret_value_prompt(app, name=name, scope=existing.scope, default_storage=existing.storage)
            return
        _begin_scope_prompt(app, name=name)
        return

    if kind == "secret_scope":
        name = str(draft.get("name") or "")
        try:
            scope: SecretScope = validate_secret_scope(interaction.values.get("scope"))
        except ValueError as exc:
            interaction.error = str(exc)
            return
        _open_secret_value_prompt(app, name=name, scope=scope, default_storage="session")
        return

    if kind == "secret_pick_update":
        name = str(interaction.values.get("name") or "")
        if not name:
            app.dialogs.current = None
            return
        existing = app.runner.secret_store.get_metadata(name, session_id=app.runner.thread_id)
        if existing is None:
            app.dialogs.current = None
            app.state.add_system(f"No managed credential named {name}.", error=True)
            return
        _open_secret_value_prompt(app, name=name, scope=existing.scope, default_storage=existing.storage)
        return

    if kind == "secret_value":
        if "__confirm_overwrite__" in interaction.values:
            decision = str(interaction.values.pop("__confirm_overwrite__") or "")
            if decision != "overwrite":
                interaction.clear_secrets()
                app._clear_secret_buffer()
                app.dialogs.current = None
                app._secret_draft = {}
                app.set_status("Credential update cancelled")
                return
            interaction.overwrite_confirmed = True
            interaction.fields = [f for f in interaction.fields if f.get("id") != "__confirm_overwrite__"]
        subs = interaction.secret_submissions()
        submission = subs.get("value")
        name = str(draft.get("name") or "")
        scope = validate_secret_scope(draft.get("scope") or "host")
        if submission is None or not name:
            interaction.clear_secrets()
            app._clear_secret_buffer()
            app.dialogs.current = None
            app._secret_draft = {}
            return
        if not interaction.overwrite_confirmed:
            conflicts = app.runner.check_secret_overwrites([(name, submission.storage)])
            if conflicts:
                interaction.prompt_overwrite_confirmation(conflicts)
                app.set_status("Credential exists · confirm overwrite", transient=False)
                return
        try:
            meta = app.runner.set_secret(
                name,
                submission.value,
                scope=scope,
                storage=submission.storage,
                overwrite=interaction.overwrite_confirmed,
            )
        except (SecretExistsError, ValueError, OSError) as exc:
            app.state.add_system(f"Could not save credential {name}: {exc}", error=True)
        else:
            app.state.add_system(f"Saved credential {meta.name} ({meta.scope}, {meta.storage}).")
        finally:
            interaction.clear_secrets()
            app._clear_secret_buffer()
            app.dialogs.current = None
            app._secret_draft = {}
        return

    if kind == "secret_pick_delete":
        name = str(interaction.values.get("name") or "")
        existing = [item for item in app.runner.list_secrets() if item.name == name]
        if not existing:
            app.dialogs.current = None
            app.state.add_system(f"No managed credential named {name}.", error=True)
            return
        _begin_delete_confirm(app, name, existing)
        return

    if kind == "secret_delete_confirm":
        decision = str(interaction.values.get("confirm") or "cancel")
        name = str(draft.get("name") or "")
        app.dialogs.current = None
        app._secret_draft = {}
        if decision != "delete" or not name:
            app.set_status("Credential delete cancelled")
            return
        removed = app.runner.delete_secret(name)
        if removed:
            app.state.add_system(f"Deleted credential {name}.")
        else:
            app.state.add_system(f"No managed credential named {name}.", error=True)
        return

    interaction.clear_secrets()
    app._clear_secret_buffer()
    app.dialogs.current = None
    app._secret_draft = {}


def _begin_name_prompt(app: Any) -> None:
    app._secret_draft = {}
    app.dialogs.current = InteractionController(
        kind="secret_name",
        source="secret_manage",
        title="Add or update credential",
        question="Enter the credential reference name (e.g. GITHUB_TOKEN). Do not paste the secret value here.",
        fields=[{"id": "name", "type": "text", "label": "Reference name", "required": True, "options": []}],
    )
    app.set_status("Enter credential name · Enter confirm · Esc cancel", transient=False)


def _begin_scope_prompt(app: Any, *, name: str) -> None:
    app._secret_draft = {"name": name}
    app.dialogs.current = InteractionController(
        kind="secret_scope",
        source="secret_manage",
        title=f"Scope for {name}",
        question="Choose where this credential may be used.",
        fields=[{
            "id": "scope",
            "type": "single_select",
            "label": "Scope",
            "required": True,
            "options": [
                {"value": "host", "label": "Host", "description": "Trusted host consumers (e.g. authorized MCP)"},
                {"value": "sandbox", "label": "Sandbox", "description": "Authorized sandbox commands"},
            ],
        }],
    )
    app.set_status("Select scope · Enter confirm · Esc cancel", transient=False)


def _open_secret_value_prompt(
    app: Any,
    *,
    name: str,
    scope: SecretScope,
    default_storage: ManagedStorage = "session",
) -> None:
    app._secret_draft = {"name": name, "scope": scope}
    controller = InteractionController(
        kind="secret_value",
        source="secret_manage",
        title=f"Set credential {name}",
        question="",
        fields=[{
            "id": "value",
            "type": "secret",
            "label": name,
            "required": True,
            "secret_name": name,
            "secret_scope": scope,
            "options": [],
        }],
    )
    controller.secret_storages["value"] = default_storage
    app.dialogs.current = controller
    app.set_status("Enter secret value · ↑↓ storage · Enter confirm · Esc cancel", transient=False)


def _begin_pick_for_update(app: Any) -> None:
    items = app.runner.list_secrets()
    if not items:
        app.state.add_system("No managed credentials to update. Use /secrets set to add one.")
        return
    seen: dict[str, SecretMetadata] = {}
    for item in items:
        seen.setdefault(item.name, item)
    app.dialogs.current = InteractionController(
        kind="secret_pick_update",
        source="secret_manage",
        title="Update credential",
        question="Select a credential to update.",
        fields=[{
            "id": "name",
            "type": "single_select",
            "label": "Credential",
            "required": True,
            "options": [
                {"value": meta.name, "label": meta.name, "description": f"{meta.scope} · {meta.storage}"}
                for meta in seen.values()
            ],
        }],
    )
    app.set_status("Select credential · Enter confirm · Esc cancel", transient=False)


def _begin_pick_for_delete(app: Any) -> None:
    items = app.runner.list_secrets()
    if not items:
        app.state.add_system("No managed credentials to delete.")
        return
    seen: dict[str, SecretMetadata] = {}
    for item in items:
        seen.setdefault(item.name, item)
    app.dialogs.current = InteractionController(
        kind="secret_pick_delete",
        source="secret_manage",
        title="Delete credential",
        question="Select a credential to delete.",
        fields=[{
            "id": "name",
            "type": "single_select",
            "label": "Credential",
            "required": True,
            "options": [
                {"value": meta.name, "label": meta.name, "description": f"{meta.scope} · {meta.storage}"}
                for meta in seen.values()
            ],
        }],
    )
    app.set_status("Select credential · Enter confirm · Esc cancel", transient=False)


def _begin_delete_confirm(app: Any, name: str, items: list[SecretMetadata]) -> None:
    app._secret_draft = {"name": name}
    detail = ", ".join(f"{item.scope}/{item.storage}" for item in items)
    app.dialogs.current = InteractionController(
        kind="secret_delete_confirm",
        source="secret_manage",
        title=f"Delete credential {name}?",
        question=f"Remove {name} ({detail})? Active processes keep existing values until they exit.",
        fields=[{
            "id": "confirm",
            "type": "single_select",
            "label": "Delete",
            "required": True,
            "options": [
                {"value": "cancel", "label": "Cancel", "description": "Keep credential"},
                {"value": "delete", "label": "Delete", "description": f"Remove {name}"},
            ],
        }],
    )
    app.set_status("Confirm credential deletion · Enter confirm · Esc cancel", transient=False)
