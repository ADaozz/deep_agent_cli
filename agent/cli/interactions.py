from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import re
from typing import Any, Literal

from rich.console import Group
from rich.table import Table
from rich.text import Text

from agent.cli.highlighting import code_renderer
from agent.redaction import SecretRef, replace_known_secrets, sanitize_display_text
from agent.secrets.store import ManagedStorage, SecretMetadata, SecretSubmission
from agent.tools.catalog import FILE_MUTATION_TOOLS, WEB_TOOLS, WRITE_STDIN_TOOL
from agent.tools.human_interaction import ensure_other_option

_Secrets = tuple[SecretRef, ...] | tuple[str, ...]
PanelMode = Literal["human", "approval", "secret", "picker"]
SecretStoragePolicy = Literal["ephemeral", "session", "persistent", "choose", "persistent_confirm"]

_STORAGE_STEP_PREFIX = "__secret_storage__:"
_OVERWRITE_STEP_ID = "__confirm_overwrite__"
_GENERIC_FIELD_LABELS = frozenset({
    "reply", "answer", "text", "input", "value", "response", "回复", "回答", "输入",
})


def _norm_copy(text: str) -> str:
    return " ".join(text.strip().lower().rstrip("?:：？.。!！").split())


def _is_duplicate_copy(candidate: str, seen: Sequence[str]) -> bool:
    norm = _norm_copy(candidate)
    if not norm:
        return True
    return any(_norm_copy(existing) == norm for existing in seen if existing)


def _is_transient_step(item: Mapping[str, Any]) -> bool:
    fid = str(item.get("id") or "")
    return fid == _OVERWRITE_STEP_ID or fid.startswith(_STORAGE_STEP_PREFIX)


def _approval_summary(
    value: Any,
    kind: str = "command",
    *,
    secrets: _Secrets = (),
) -> str:
    del kind  # Kind remains for call-site clarity; display no longer guesses credentials.
    command = str(value or "<not supplied>")
    command = replace_known_secrets(command, secrets)
    command = " ".join(sanitize_display_text(command, keep_newlines=False).split())
    return command if len(command) <= 180 else command[:180] + "… [truncated; Ctrl+O to expand tool]"


def _mcp_approval_summary(text: str, *, secrets: _Secrets = ()) -> str:
    text = replace_known_secrets(mcp_display_text(text), secrets)
    text = " ".join(sanitize_display_text(text, keep_newlines=False).split())
    return text if len(text) <= 180 else text[:180] + "…"


def mcp_display_text(value: str) -> str:
    """Render untrusted MCP text without terminal control sequences."""
    return sanitize_display_text(value, keep_newlines=True)


def mcp_arguments_preview(args: Any, *, expanded: bool = False) -> str:
    """Bounded display-only traversal; never writes into the original arguments."""
    max_lines, max_chars, max_depth, string_limit = (
        (80, 8000, 8, 1200) if expanded else (8, 700, 4, 100)
    )
    lines: list[str] = []
    remaining = max_chars
    omitted = False
    ancestors: set[int] = set()

    def text(value: str, limit: int) -> str:
        nonlocal omitted
        value = mcp_display_text(value).replace("\n", r"\n").replace("\r", r"\r").replace("\t", r"\t")
        if len(value) > limit:
            omitted = True
            return value[:limit] + "… [omitted]"
        return value

    def emit(line: str) -> None:
        nonlocal remaining, omitted
        if len(line) > remaining:
            remaining = 0
            omitted = True
            return
        if len(lines) >= max_lines:
            omitted = True
            return
        lines.append(line)
        remaining -= len(line) + 1

    def visit(value: Any, prefix: str, depth: int) -> None:
        nonlocal omitted
        if len(lines) >= max_lines or remaining <= 0:
            omitted = True
            return
        if isinstance(value, (dict, list)):
            if not value:
                emit(prefix + ("{}" if isinstance(value, dict) else "[]"))
                return
            if depth >= max_depth or id(value) in ancestors:
                emit(prefix + "[nested content omitted]")
                omitted = True
                return
            if prefix.strip():
                emit(prefix.rstrip())
            ancestors.add(id(value))
            entries = value.items() if isinstance(value, dict) else enumerate(value)
            for key, item in entries:
                if len(lines) >= max_lines or remaining <= 0:
                    omitted = True
                    break
                if isinstance(value, dict):
                    label = text(str(key), 100)
                    # Quote unusual keys so ':' or whitespace cannot forge structure.
                    label = label if re.fullmatch(r"[A-Za-z0-9_.-]+", label) else json.dumps(label, ensure_ascii=False)
                    child = "  " * (depth + 1) + label + ": "
                else:
                    child = "  " * (depth + 1) + "- "
                visit(item, child, depth + 1)
            ancestors.remove(id(value))
        else:
            if isinstance(value, str):
                rendered = text(value, string_limit)
            elif value is None:
                rendered = "null"
            elif isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, (int, float)):
                rendered = str(value)
            else:
                rendered = "[unsupported value]"
            emit(prefix + rendered)

    visit(args, "", 0)
    if omitted:
        lines.append(
            "  … content omitted (bounded preview)"
            + ("" if expanded else "; Esc, Ctrl+O for more; F2 to reopen approval")
        )
    return "\n".join(lines)


def _format_visible_stdin(value: str, *, secrets: _Secrets = ()) -> str:
    redacted = replace_known_secrets(value, secrets)
    sanitized = sanitize_display_text(redacted, keep_newlines=True)
    escaped = sanitized.replace("\r", r"\r").replace("\n", r"\n").replace("\t", r"\t")
    return escaped if len(escaped) <= 180 else escaped[:180] + "…"


def _parse_mcp_display_name(
    name: str,
    mcp_tool_lookup: Callable[[str], tuple[str, str] | None] | None = None,
) -> tuple[str | None, str]:
    if callable(mcp_tool_lookup):
        resolved = mcp_tool_lookup(name)
        if resolved is not None:
            server, remote = resolved
            return (server or None), (remote or name)
    if name.startswith("mcp__"):
        rest = name[len("mcp__"):]
        if "__" in rest:
            server, remote = rest.split("__", 1)
            if server and remote:
                return server, remote
    return None, name


def _format_approval_call(
    call: Mapping[str, Any],
    *,
    secrets: _Secrets = (),
    execution_scope: str | None = None,
    mcp_tool_lookup: Callable[[str], tuple[str, str] | None] | None = None,
    single_call: bool = True,
) -> list[str]:
    from agent.execution.network import network_requested

    name = " ".join(str(call.get("name") or "tool").split())[:80]
    raw_args = call.get("args")
    args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
    lines: list[str] = []

    if name == "execute":
        command = _approval_summary(args.get("command"), secrets=secrets)
        lines.append("Command:")
        lines.append(command)
        if single_call:
            lines.append("")
        if network_requested(args):
            lines.append("Network access: internet, localhost and LAN")
        else:
            lines.append("Network access: off" + (" (default)" if "network" not in args else ""))
        scope = execution_scope or "Sandbox"
        if args.get("tty"):
            scope += " · interactive (PTY)"
        elif args.get("interactive"):
            scope += " · interactive (pipe)"
        lines.append(f"Execution scope: {scope}")
        return lines

    if name in FILE_MUTATION_TOOLS:
        target = _approval_summary(args.get("file_path"), kind="target", secrets=secrets)
        if name == "write":
            lines.append(f"Write file: {target}")
            if isinstance(args.get("content"), str):
                raw_content = args["content"]
                if not raw_content:
                    lines.append("Content: empty file")
                else:
                    line_count = len(raw_content.splitlines())
                    byte_count = len(raw_content.encode("utf-8"))
                    unit = "line" if line_count == 1 else "lines"
                    lines.append(f"Content: {line_count} {unit} ({byte_count} bytes)")
        elif name == "edit":
            lines.append(f"Edit file: {target}")
            old_s = args.get("old_string")
            new_s = args.get("new_string")
            if isinstance(old_s, str) and isinstance(new_s, str):
                old_n = len(old_s.splitlines()) or (1 if old_s else 0)
                new_n = len(new_s.splitlines()) or (1 if new_s else 0)
                old_u = "line" if old_n == 1 else "lines"
                new_u = "line" if new_n == 1 else "lines"
                suffix = " (all occurrences)" if args.get("replace_all") else ""
                lines.append(f"Change: replace {old_n} {old_u} with {new_n} {new_u}{suffix}")
        else:
            lines.append(f"Delete file: {target}")
        return lines

    if name in WEB_TOOLS:
        if name == "web_search":
            lines.append(f'Web search: "{_approval_summary(args.get("query"), secrets=secrets)}"')
            if args.get("ori_question"):
                lines.append(f"Original question: {_approval_summary(args['ori_question'], secrets=secrets)}")
            if args.get("localization"):
                lines.append(f"Localization: {_approval_summary(args['localization'], secrets=secrets)}")
        else:
            lines.append(f"Fetch URL: {_approval_summary(args.get('url'), kind='target', secrets=secrets)}")
        lines.append("Network access: DeepSeek API (host)")
        return lines

    if name == WRITE_STDIN_TOOL:
        exec_id = _approval_summary(args.get("execution_id"), kind="target", secrets=secrets)
        action = str(args.get("action") or "write").strip().lower()
        if action == "cancel":
            lines.append(f"Cancel running process: {exec_id}")
        elif action == "close":
            lines.append(f"Close stdin (EOF) for process: {exec_id}")
        else:
            lines.append(f"Send stdin to process: {exec_id}")
            chars = args.get("chars")
            if isinstance(chars, str) and chars:
                lines.append(f"Input: {_format_visible_stdin(chars, secrets=secrets)}")
            else:
                lines.append("Input: <empty poll>")
        return lines

    if name == "mcp_add":
        server_name = _mcp_approval_summary(str(args.get("name") or "<not supplied>"), secrets=secrets)
        transport = str(args.get("transport") or "<not supplied>")
        lines.append(f"Add MCP server: {server_name}")
        lines.append(f"Transport: {transport}")
        if transport == "stdio":
            command = str(args.get("command") or "")
            argv = args.get("argv") if isinstance(args.get("argv"), list) else args.get("args")
            argv = argv if isinstance(argv, list) else []
            rendered = " ".join([command, *[str(item) for item in argv]]).strip()
            lines.append(
                "Host command (runs outside the sandbox): "
                + _mcp_approval_summary(rendered or "<not supplied>", secrets=secrets)
            )
        else:
            lines.append(f"URL: {_mcp_approval_summary(str(args.get('url') or '<not supplied>'), secrets=secrets)}")
        auth = str(args.get("auth_type") or "none")
        token_env = str(args.get("token_env") or "")
        lines.append(
            f"Auth: {auth}"
            + (f" via environment variable {token_env}" if auth == "bearer" and token_env else "")
        )
        env_pass = args.get("env_passthrough")
        if isinstance(env_pass, list) and env_pass:
            safe_names = ", ".join(_mcp_approval_summary(str(item), secrets=secrets) for item in env_pass if item)
            if safe_names:
                lines.append(f"Env passthrough: {safe_names}")
        lines.append("Target: Host MCP configuration")
        return lines

    if name == "mcp_remove":
        server_name = _mcp_approval_summary(str(args.get("name") or "<not supplied>"), secrets=secrets)
        lines.append(f"Remove MCP server: {server_name}")
        lines.append("Target: Host MCP configuration")
        return lines

    if name.startswith("mcp__"):
        server, remote = _parse_mcp_display_name(name, mcp_tool_lookup)
        label = f"{server} · {remote}" if server else remote
        lines.append(f"MCP tool: {label}")
        lines.append("Execution: Host (outside sandbox)")
        lines.append("Arguments:\n" + mcp_arguments_preview(call.get("args", {})))
        return lines

    lines.append(f"Action: {name}")
    if args:
        lines.append("Arguments:\n" + mcp_arguments_preview(args))
    return lines


def _render_approval_body(question: str) -> list[Text]:
    """Render approval details, highlighting host-execution risk lines in red."""
    rendered: list[Text] = []
    for line in question.splitlines():
        lowered = line.lower()
        if "outside sandbox" in lowered or "outside the sandbox" in lowered:
            rendered.append(Text(line, style="red"))
        else:
            rendered.append(Text(line))
    return rendered


@dataclass
class InteractionController:
    kind: str
    title: str
    question: str
    fields: list[dict[str, Any]]
    interaction_id: str = ""
    reason: str = ""
    source: str = "agent_input"
    recommendation: dict[str, Any] = field(default_factory=dict)
    impact: list[str] = field(default_factory=list)
    index: int = 0
    option_index: int = 0
    values: dict[str, Any] = field(default_factory=dict)
    custom_entry: bool = False
    custom_values: dict[str, str] = field(default_factory=dict)
    error: str = ""
    tool_call_ids: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    # Isolated from ``values`` and excluded from repr so plaintext secrets never leak into logs/events.
    _secret_values: dict[str, str] = field(default_factory=dict, repr=False)
    # Trusted host-determined storage policy & consumers per secret field (never inferred from model text).
    _secret_policies: dict[str, SecretStoragePolicy] = field(default_factory=dict)
    _secret_consumers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _secret_confirmed_storages: dict[str, ManagedStorage] = field(default_factory=dict)
    overwrite_confirmed: bool = False
    persistence_declined: bool = False
    auth_callback: Any = field(default=None, repr=False)

    @classmethod
    def human(
        cls,
        payload: dict[str, Any],
        *,
        auth_callback: Any = None,
        secret_consumers: Mapping[str, Sequence[str]] | None = None,
    ) -> "InteractionController":
        fields = payload.get("fields") if isinstance(payload.get("fields"), list) else []
        if not fields:
            fields = [{"id": "text", "type": "textarea", "label": "Reply", "required": True, "options": []}]
        fields = [dict(item) for item in fields]
        raw_recommendation = payload.get("recommendation")
        source = str(payload.get("source") or "agent_input")
        if source not in {"agent_input", "runtime_auth"}:
            source = "agent_input"
        trusted_consumers = {
            str(k): tuple(str(srv) for srv in v if str(srv))
            for k, v in (secret_consumers or {}).items()
            if str(k)
        }
        policies: dict[str, SecretStoragePolicy] = {}
        for idx, item in enumerate(fields):
            field_type = str(item.get("type") or "text")
            if field_type == "boolean" and not item.get("options"):
                item["options"] = [
                    {"value": False, "label": "No", "description": ""},
                    {"value": True, "label": "Yes", "description": ""},
                ]
            elif field_type in {"single_select", "multi_select"}:
                item["options"] = ensure_other_option(item.get("options") or [])
            elif field_type == "secret":
                field_id = str(item.get("id") or f"field_{idx}")
                if source == "runtime_auth":
                    policies[field_id] = "ephemeral"
                else:
                    secret_name = str(item.get("secret_name") or "").strip()
                    secret_scope = str(item.get("secret_scope") or "sandbox").strip().lower()
                    if secret_scope == "host":
                        if trusted_consumers.get(secret_name):
                            policies[field_id] = "persistent_confirm"
                        else:
                            policies[field_id] = "choose"
                    else:
                        policies[field_id] = "session"
        raw_title = str(payload.get("title") or "").strip()
        raw_question = str(payload.get("question") or "").strip()
        default_title = "Authentication required" if source == "runtime_auth" else "Input required"
        has_secret_only = bool(fields) and all(str(item.get("type") or "") == "secret" for item in fields)
        fallback_question = "" if (raw_title or has_secret_only) else "Please provide the requested information."
        return cls(
            kind="human",
            source=source,
            title=raw_title or default_title,
            question=raw_question or fallback_question,
            reason=str(payload.get("reason") or ""),
            fields=fields,
            interaction_id=str(payload.get("interactionId") or ""),
            recommendation=raw_recommendation if isinstance(raw_recommendation, dict) else {},
            impact=[str(item) for item in payload.get("impact", [])],
            _secret_policies=policies,
            _secret_consumers=trusted_consumers,
            auth_callback=auth_callback if source == "runtime_auth" else None,
        )

    @classmethod
    def approval(
        cls,
        calls: list[dict[str, Any]],
        *,
        secrets: _Secrets = (),
        execution_scope: str | None = None,
        mcp_tool_lookup: Callable[[str], tuple[str, str] | None] | None = None,
    ) -> "InteractionController":
        single = len(calls) == 1
        blocks: list[str] = []
        for idx, call in enumerate(calls, 1):
            call_lines = _format_approval_call(
                call,
                secrets=secrets,
                execution_scope=execution_scope,
                mcp_tool_lookup=mcp_tool_lookup,
                single_call=single,
            )
            if single:
                blocks.append("\n".join(call_lines))
            else:
                indented: list[str] = []
                for line_idx, raw_line in enumerate(call_lines):
                    sub_lines = raw_line.splitlines() or [""]
                    for sub_idx, sub in enumerate(sub_lines):
                        if line_idx == 0 and sub_idx == 0:
                            indented.append(f"{idx}. {sub}")
                        elif sub:
                            indented.append(f"   {sub}")
                        else:
                            indented.append("")
                blocks.append("\n".join(indented))
        title = "Approval required" if single else f"Approval required ({len(calls)} actions)"
        return cls(
            kind="approval",
            title=title,
            question="\n\n".join(blocks),
            fields=[{
                "id": "approved",
                "type": "single_select",
                "label": "",
                "required": True,
                "options": [
                    {"value": "reject", "label": "Reject", "description": ""},
                    {"value": "approve", "label": "Run", "description": ""},
                ],
            }],
            tool_call_ids=[str(call.get("toolCallId") or "") for call in calls if call.get("toolCallId")],
            calls=list(calls),
        )

    @classmethod
    def pause(cls) -> "InteractionController":
        return cls(
            kind="pause",
            title="Paused",
            question="Continue the agent run?",
            fields=[{
                "id": "continue", "type": "single_select", "label": "Decision", "required": True,
                "options": [
                    {"value": "stay", "label": "No", "description": ""},
                    {"value": "continue", "label": "Yes", "description": ""},
                ],
            }],
        )

    @property
    def current(self) -> dict[str, Any]:
        return self.fields[min(self.index, len(self.fields) - 1)]

    @property
    def accepts_text(self) -> bool:
        return self.custom_entry or str(self.current.get("type")) in {"text", "textarea"}

    @property
    def accepts_secret(self) -> bool:
        return not self.custom_entry and str(self.current.get("type")) == "secret"

    @property
    def has_secret_fields(self) -> bool:
        return any(str(item.get("type")) == "secret" for item in self.fields)

    @property
    def panel_mode(self) -> PanelMode:
        if self.kind == "approval":
            return "approval"
        if self.accepts_secret or _is_transient_step(self.current):
            return "secret"
        if self.kind == "human":
            return "human"
        return "picker"

    def _secret_policy(self, field_id: str, field: Mapping[str, Any]) -> SecretStoragePolicy:
        if field_id in self._secret_policies:
            return self._secret_policies[field_id]
        if self.source == "runtime_auth":
            return "ephemeral"
        scope = str(field.get("secret_scope") or "sandbox").strip().lower()
        name = str(field.get("secret_name") or "").strip()
        if scope == "host":
            return "persistent_confirm" if self._secret_consumers.get(name) else "choose"
        return "session"

    def _resolved_secret_storage(self, field_id: str, field: Mapping[str, Any]) -> ManagedStorage:
        if field_id in self._secret_confirmed_storages:
            return self._secret_confirmed_storages[field_id]
        policy = self._secret_policy(field_id, field)
        if policy == "persistent":
            return "persistent"
        return "session"

    def secret_targets(self) -> list[tuple[str, ManagedStorage]]:
        targets: list[tuple[str, ManagedStorage]] = []
        for idx, item in enumerate(self.fields):
            if str(item.get("type")) != "secret":
                continue
            field_id = str(item.get("id") or f"field_{idx}")
            secret_name = str(item.get("secret_name") or "").strip()
            if secret_name and self._secret_values.get(field_id, "") != "":
                targets.append((secret_name, self._resolved_secret_storage(field_id, item)))
        return targets

    def secret_submissions(self) -> dict[str, SecretSubmission]:
        submissions: dict[str, SecretSubmission] = {}
        for idx, item in enumerate(self.fields):
            if str(item.get("type")) != "secret":
                continue
            field_id = str(item.get("id") or f"field_{idx}")
            if field_id not in self._secret_values:
                continue
            raw_val = self._secret_values[field_id]
            if raw_val == "":
                continue
            submissions[field_id] = SecretSubmission(
                value=raw_val,
                storage=self._resolved_secret_storage(field_id, item),
                overwrite=self.overwrite_confirmed,
            )
        return submissions

    def runtime_auth_secret(self) -> str:
        field_id = str(self.fields[0].get("id") or "password") if self.fields else "password"
        return self._secret_values.get(field_id, "")

    def clear_secrets(self) -> None:
        self._secret_values.clear()
        self._secret_confirmed_storages.clear()
        self.auth_callback = None

    def prompt_overwrite_confirmation(self, conflicts: list[SecretMetadata]) -> None:
        names = ", ".join(f"{meta.name} ({meta.storage})" for meta in conflicts)
        confirm_field = {
            "id": _OVERWRITE_STEP_ID,
            "type": "single_select",
            "label": "Overwrite existing credential?",
            "warning": f"Warning: {names} already exists and will be replaced.",
            "required": True,
            "options": [
                {"value": "cancel", "label": "Cancel", "description": ""},
                {"value": "overwrite", "label": "Overwrite", "description": ""},
            ],
        }
        self.fields = [item for item in self.fields if item.get("id") != _OVERWRITE_STEP_ID]
        self.fields.append(confirm_field)
        self.index = len(self.fields) - 1
        self.option_index = 0
        self.error = ""

    def move(self, delta: int) -> None:
        if self.custom_entry or self.accepts_secret:
            return
        options = self.current.get("options") or []
        if options:
            self.option_index = (self.option_index + delta) % len(options)

    def _build_storage_step(
        self,
        *,
        field_id: str,
        secret_name: str,
        secret_scope: str,
        policy: SecretStoragePolicy,
    ) -> dict[str, Any]:
        consumers = self._secret_consumers.get(secret_name, ())
        if policy == "persistent_confirm":
            consumer_text = ", ".join(consumers)
            return {
                "id": f"{_STORAGE_STEP_PREFIX}{field_id}",
                "type": "single_select",
                "target_field_id": field_id,
                "storage_policy": policy,
                "secret_name": secret_name,
                "secret_scope": secret_scope,
                "consumers": consumers,
                "step_title": "Confirm encrypted storage",
                "step_body": (
                    f"Store {secret_name} encrypted on this device for MCP ({consumer_text})?"
                    if consumer_text
                    else f"Store {secret_name} encrypted on this device for future sessions?"
                ),
                "required": True,
                "options": [
                    {"value": "cancel", "label": "Cancel", "description": ""},
                    {"value": "persistent", "label": "Store encrypted on this device", "description": ""},
                ],
            }
        return {
            "id": f"{_STORAGE_STEP_PREFIX}{field_id}",
            "type": "single_select",
            "target_field_id": field_id,
            "storage_policy": policy,
            "secret_name": secret_name,
            "secret_scope": secret_scope,
            "consumers": consumers,
            "step_title": "Credential storage",
            "step_body": (
                f"Choose how to store {secret_name} ({secret_scope}). "
                "Global MCP requires encrypted storage on this device."
                if secret_scope == "host"
                else f"Choose how to store {secret_name} ({secret_scope})."
            ),
            "required": True,
            "options": [
                {"value": "session", "label": "This session only", "description": ""},
                {"value": "persistent", "label": "Store encrypted on this device", "description": ""},
            ],
        }

    def accept_secret(self, secret_text: str) -> bool:
        """Accept a secret value strictly into isolated storage (never ``self.values``)."""
        field = self.current
        field_id = str(field.get("id") or f"field_{self.index}")
        if field.get("required") and secret_text == "":
            self.error = "This field is required."
            return False
        policy = self._secret_policy(field_id, field)
        if secret_text != "":
            self._secret_values[field_id] = secret_text
            if policy in {"choose", "persistent_confirm"}:
                step_id = f"{_STORAGE_STEP_PREFIX}{field_id}"
                self.fields = [item for item in self.fields if item.get("id") != step_id]
                step = self._build_storage_step(
                    field_id=field_id,
                    secret_name=str(field.get("secret_name") or "").strip(),
                    secret_scope=str(field.get("secret_scope") or "host").strip().lower(),
                    policy=policy,
                )
                self.fields.insert(self.index + 1, step)
                self.index += 1
                self.option_index = 0
                self.error = ""
                return False
            if policy == "persistent":
                self._secret_confirmed_storages[field_id] = "persistent"
            elif policy == "session":
                self._secret_confirmed_storages[field_id] = "session"
        else:
            self._secret_values.pop(field_id, None)
            self._secret_confirmed_storages.pop(field_id, None)
        self.error = ""
        if self.index + 1 < len(self.fields):
            self.index += 1
            self.option_index = 0
            return False
        return True

    def toggle(self) -> None:
        if self.custom_entry or self.current.get("type") != "multi_select":
            return
        options = self.current.get("options") or []
        if not options:
            return
        field_id = str(self.current.get("id") or "field")
        selected = list(self.values.get(field_id) or [])
        option = options[self.option_index]
        if option.get("allowsCustomText"):
            if field_id in self.custom_values:
                custom = self.custom_values.pop(field_id)
                selected.pop(len(selected) - 1 - selected[::-1].index(custom))
                self.values[field_id] = selected
            else:
                self.custom_entry = True
            return
        value = str(option.get("value"))
        selected.remove(value) if value in selected else selected.append(value)
        self.values[field_id] = selected

    def accept(self, text: str = "") -> bool:
        field = self.current
        field_id = str(field.get("id") or f"field_{self.index}")
        field_type = str(field.get("type") or "text")
        if not self.custom_entry and field_type == "secret":
            return self.accept_secret(text)
        if field_id.startswith(_STORAGE_STEP_PREFIX):
            options = field.get("options") or []
            choice = str(options[self.option_index].get("value") or "cancel") if options else "cancel"
            target_field_id = str(field.get("target_field_id") or "")
            self.fields.pop(self.index)
            self.error = ""
            self.option_index = 0
            if choice == "cancel":
                self.clear_secrets()
                self.persistence_declined = True
                return True
            if target_field_id and choice in {"session", "persistent"}:
                self._secret_confirmed_storages[target_field_id] = choice  # type: ignore[assignment]
            if self.index < len(self.fields):
                return False
            return True
        options = field.get("options") or []
        value: Any
        if self.custom_entry:
            custom = text.strip()
            if not custom:
                self.error = "Enter a custom answer."
                return False
            self.custom_entry = False
            self.custom_values[field_id] = custom
            self.error = ""
            if field_type == "multi_select":
                selected = list(self.values.get(field_id) or [])
                selected.append(custom)
                self.values[field_id] = selected
                return False
            value = custom
        elif field_type in {"text", "textarea"}:
            value = text.strip()
        elif field_type == "multi_select":
            if options and options[self.option_index].get("allowsCustomText") and field_id not in self.custom_values:
                self.custom_entry = True
                self.error = ""
                return False
            value = self.values.get(field_id) or []
        elif options:
            option = options[self.option_index]
            if option.get("allowsCustomText"):
                self.custom_entry = True
                self.error = ""
                return False
            raw_value = option.get("value")
            value = raw_value if field_type == "boolean" else str(raw_value or "")
        else:
            value = text.strip()
        if field.get("required") and (value == "" or value == []):
            self.error = "This field is required."
            return False
        self.values[field_id] = value
        self.error = ""
        if self.index + 1 < len(self.fields):
            self.index += 1
            self.option_index = 0
            return False
        return True

    def _logical_progress(self) -> tuple[int, int]:
        logical = [item for item in self.fields if not _is_transient_step(item)]
        total = len(logical)
        current = self.current
        if _is_transient_step(current):
            return total, total
        pos = sum(1 for item in self.fields[:self.index + 1] if not _is_transient_step(item))
        return max(1, pos), max(1, total)

    def _option_matches_recommendation(self, option: Mapping[str, Any]) -> bool:
        if option.get("allowsCustomText"):
            return False
        rec_val = str(self.recommendation.get("value") or "").strip()
        if not rec_val:
            return False
        opt_val = str(option.get("value") or "").strip()
        opt_label = str(option.get("label") or "").strip()
        return rec_val == opt_val or rec_val == opt_label or rec_val.lower() in {opt_val.lower(), opt_label.lower()}

    def _theme_styles(self, mode: PanelMode) -> tuple[str, str]:
        if mode in {"approval", "secret"}:
            return "bold yellow", "bold yellow"
        return "bold bright_cyan", "bold cyan"

    def _render_secret_header(self, title_style: str) -> list[Any]:
        field = self.current
        fid = str(field.get("id") or f"field_{self.index}")
        if fid == _OVERWRITE_STEP_ID:
            warning = str(field.get("warning") or "Warning: existing credential will be replaced.")
            return [
                Text("Overwrite existing credential?", style=title_style),
                Text(""),
                Text(warning, style="red"),
                Text(""),
            ]
        if fid.startswith(_STORAGE_STEP_PREFIX):
            step_title = str(field.get("step_title") or "Credential storage")
            step_body = str(field.get("step_body") or "")
            parts: list[Any] = [Text(step_title, style=title_style), Text("")]
            if step_body:
                parts.append(Text(step_body))
                parts.append(Text(""))
            return parts

        pos, total = self._logical_progress()
        heading = "Authentication required" if self.source == "runtime_auth" else "Credential required"
        if total > 1:
            heading = f"{heading} ({pos}/{total})"
        parts = [Text(heading, style=title_style), Text("")]

        seen: list[str] = [heading]
        label_name = str(field.get("label") or "").strip()
        if label_name and _norm_copy(label_name) not in _GENERIC_FIELD_LABELS and not _is_duplicate_copy(label_name, seen):
            parts.append(Text(label_name, style="bold"))
            seen.append(label_name)
        elif self.title and not _is_duplicate_copy(self.title, seen):
            parts.append(Text(self.title, style="bold"))
            seen.append(self.title)

        for candidate in (self.question, self.reason):
            text = candidate.strip()
            if text and not _is_duplicate_copy(text, seen):
                parts.append(Text(text, style="dim" if len(seen) >= 2 else ""))
                seen.append(text)
        return parts

    def _render_header(self, mode: PanelMode, title_style: str, has_options: bool) -> list[Any]:
        if mode == "secret":
            return self._render_secret_header(title_style)

        if mode == "approval":
            parts: list[Any] = [Text(self.title, style=title_style)]
            if self.question:
                parts.append(Text(""))
                parts.extend(_render_approval_body(self.question))
            if has_options:
                parts.append(Text(""))
            return parts

        if mode == "human":
            parts = [Text(self.title, style=title_style)]
            seen: list[str] = [self.title]
            if self.question and not _is_duplicate_copy(self.question, seen):
                parts.append(code_renderer.markdown(self.question))
                seen.append(self.question)
            if self.reason and not _is_duplicate_copy(self.reason, seen):
                parts.append(Text(self.reason, style="dim"))
                seen.append(self.reason)
            if self.impact:
                if len(self.impact) == 1:
                    parts.append(Text(f"Impact: {self.impact[0]}", style="dim"))
                else:
                    parts.append(Text("Impact:\n" + "\n".join(f"• {item}" for item in self.impact), style="dim"))
            pos, total = self._logical_progress()
            label_name = str(self.current.get("label") or self.current.get("id") or "").strip()
            if total > 1:
                if label_name and not _is_duplicate_copy(label_name, seen):
                    parts.append(Text(f"{pos}/{total}  {label_name}", style="bold"))
                else:
                    parts.append(Text(f"{pos}/{total}", style="bold"))
            elif (
                label_name
                and _norm_copy(label_name) not in _GENERIC_FIELD_LABELS
                and not _is_duplicate_copy(label_name, seen)
            ):
                parts.append(Text(label_name, style="bold"))
            if not has_options and not self.custom_entry and self.recommendation:
                rec_val = str(self.recommendation.get("value") or "").strip()
                rec_why = str(self.recommendation.get("reason") or "").strip()
                if rec_val:
                    note = f"Suggested: {rec_val}" + (f" — {rec_why}" if rec_why and not _is_duplicate_copy(rec_why, seen) else "")
                    parts.append(Text(note, style="dim"))
            if has_options:
                parts.append(Text(""))
            return parts

        # Picker mode (model, resume, permission, sandbox, skill, process_attach, mcp_*, pause, /secrets menus)
        parts = [Text(self.title, style=title_style)]
        if self.reason:
            parts.append(Text(self.reason, style="dim"))
        if self.question:
            parts.append(
                Text(self.question) if self.kind == "mcp_confirm" else code_renderer.markdown(self.question)
            )
        if self.recommendation:
            value = self.recommendation.get("value") or ""
            why = self.recommendation.get("reason") or ""
            parts.append(Text(f"Recommended: {value} {why}".strip(), style="cyan"))
        if self.impact:
            parts.append(Text("Impact:\n" + "\n".join(f"• {item}" for item in self.impact), style="yellow"))
        label_name = str(self.current.get("label") or self.current.get("id") or "")
        if len(self.fields) > 1:
            parts.append(Text(f"{self.index + 1}/{len(self.fields)}  {label_name}", style="bold"))
        return parts

    def _render_options(self, mode: PanelMode, selected_style: str) -> list[Any]:
        field = self.current
        if self.custom_entry or self.accepts_secret:
            return []
        options = field.get("options") or []
        if not options:
            return []
        field_id = str(field.get("id") or "")
        selected = self.values.get(field_id) or []
        rec_reason = str(self.recommendation.get("reason") or "").strip()
        seen_context = [self.title, self.question, self.reason]
        rendered: list[Any] = []

        for idx, option in enumerate(options):
            pointer = "→" if idx == self.option_index else " "
            if option.get("allowsCustomText"):
                checked = field_id in self.custom_values
            else:
                checked = option.get("value") in selected
            mark = "[x]" if checked else "[ ]" if field.get("type") == "multi_select" else ""
            option_label = str(option.get("label") or "")
            description = str(option.get("description") or "").strip()
            style = selected_style if idx == self.option_index else ""

            if mode == "picker":
                prefix = f"{pointer} {mark} "
                label = f"{prefix}{option_label}  {description}".rstrip()
                label_text = Text(label, style=style)
                if option_label.endswith(" · current"):
                    marker_start = len(prefix) + len(option_label) - len("· current")
                    label_text.stylize("bold bright_cyan", marker_start, marker_start + len("· current"))
                if right_label := option.get("right_label"):
                    row = Table.grid(expand=True, padding=(0, 1))
                    row.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
                    row.add_column(width=len(str(right_label)), justify="right", no_wrap=True)
                    row.add_row(label_text, Text(str(right_label), style=style))
                    rendered.append(row)
                else:
                    rendered.append(label_text)
                continue

            prefix = f"{pointer} {mark} " if mark else f"{pointer} "
            is_recommended = mode == "human" and self._option_matches_recommendation(option)
            display_label = f"{option_label} (recommended)" if is_recommended else option_label
            if description and _is_duplicate_copy(description, [*seen_context, option_label]):
                description = ""
            line = Text(f"{prefix}{display_label}", style=style)
            if description:
                line.append(f"  {description}", style=style or "dim")
            if is_recommended and rec_reason and not _is_duplicate_copy(rec_reason, [*seen_context, option_label, description]):
                line.append(f"\n{' ' * len(prefix)}  {rec_reason}", style="dim")
            rendered.append(line)
        return rendered

    def _render_secret_notice(self) -> list[Text]:
        if not self.accepts_secret:
            return []
        if self.source == "runtime_auth":
            return [
                Text(""),
                Text("Used for this authentication only. Not stored.", style="dim"),
            ]
        field = self.current
        field_id = str(field.get("id") or f"field_{self.index}")
        secret_name = str(field.get("secret_name") or "").strip()
        secret_scope = str(field.get("secret_scope") or "sandbox").strip().lower()
        policy = self._secret_policy(field_id, field)
        consumers = self._secret_consumers.get(secret_name, ())

        ref_parts: list[str] = []
        if secret_name:
            ref_parts.append(secret_name)
        if consumers:
            ref_parts.append(f"MCP: {', '.join(consumers)}")
        elif secret_scope:
            ref_parts.append(secret_scope)
        ref_suffix = f" ({' · '.join(ref_parts)})" if ref_parts else ""

        if policy == "session":
            storage_line = f"Available only for this session{ref_suffix}."
        elif policy in {"persistent", "persistent_confirm"}:
            storage_line = f"Stored encrypted on this device for future sessions{ref_suffix}."
        else:
            storage_line = f"Choose storage in the next step{ref_suffix}."
        return [
            Text(""),
            Text(storage_line, style="dim"),
            Text("The agent receives only the credential name.", style="dim"),
        ]

    def _footer_hint(self, mode: PanelMode, has_options: bool) -> str:
        field = self.current
        field_id = str(field.get("id") or f"field_{self.index}")
        if self.custom_entry:
            return "Enter submit · Ctrl+J newline · Esc back"
        if mode == "approval":
            return "↑↓ select · Enter confirm · Esc close · F2 reopen"
        if mode == "secret":
            if self.source == "runtime_auth":
                return "Enter submit · Esc cancel"
            if field_id.startswith(_STORAGE_STEP_PREFIX):
                esc = "Esc close · F2 reopen" if self.kind == "human" else "Esc cancel"
                return f"↑↓ select · Enter confirm · Shift+Tab back · {esc}"
            if field_id == _OVERWRITE_STEP_ID:
                esc = "Esc close · F2 reopen" if self.kind == "human" else "Esc cancel"
                return f"↑↓ select · Enter confirm · {esc}"
            back = " · Shift+Tab back" if self.index > 0 else ""
            if self.kind == "human":
                return f"Enter submit{back} · Esc close · F2 reopen"
            return f"Enter submit{back} · Esc cancel"
        if mode == "human":
            back = " · Shift+Tab back" if self.index > 0 else ""
            if field.get("type") == "multi_select" and has_options:
                return f"↑↓ select · Space toggle · Enter confirm{back} · Esc close"
            if has_options:
                return f"↑↓ select · Enter confirm{back} · Esc close"
            return f"Enter submit · Ctrl+J newline{back} · Esc close"
        # Picker mode (keep exact existing hints)
        if self.kind == "model_reasoning" or (self.kind == "model" and self.values.get("source")):
            return "↑↓ select  Enter confirm  Esc back"
        if self.kind == "pause" and has_options:
            return "↑↓ select  Enter confirm  Esc cancel · F2 to reopen"
        if has_options:
            return "↑↓ select  Enter confirm  Esc cancel"
        return "Enter submit  Ctrl+J newline  Esc cancel"

    def _render_footer(self, mode: PanelMode, has_options: bool) -> list[Any]:
        parts: list[Any] = []
        if mode == "secret":
            parts.extend(self._render_secret_notice())
        if self.error:
            if mode in {"human", "approval", "secret"} and not parts:
                parts.append(Text(""))
            parts.append(Text(self.error, style="red"))
        if self.custom_entry:
            if mode == "human" and not parts:
                parts.append(Text(""))
            parts.append(Text("Other: enter your answer", style="cyan"))
        hint = self._footer_hint(mode, has_options)
        if mode in {"human", "approval", "secret"}:
            parts.append(Text(""))
        parts.append(Text(hint, style="dim"))
        return parts

    def render(self) -> Group:
        header, options, footer = self.render_sections()
        return Group(*header, *options, *footer)

    def render_sections(self) -> tuple[list[Any], list[Any], list[Any]]:
        """Keep the heading and hints separate from the selectable list."""
        mode = self.panel_mode
        title_style, selected_style = self._theme_styles(mode)
        options = self._render_options(mode, selected_style)
        has_options = bool(options)
        header = self._render_header(mode, title_style, has_options)
        footer = self._render_footer(mode, has_options)
        return header, options, footer
