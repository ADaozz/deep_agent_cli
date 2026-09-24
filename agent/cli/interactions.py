from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rich.console import Group
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text


@dataclass
class InteractionController:
    kind: str
    title: str
    question: str
    fields: list[dict[str, Any]]
    interaction_id: str = ""
    reason: str = ""
    recommendation: dict[str, Any] = field(default_factory=dict)
    impact: list[str] = field(default_factory=list)
    index: int = 0
    option_index: int = 0
    values: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    tool_call_ids: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def human(cls, payload: dict[str, Any]) -> "InteractionController":
        fields = payload.get("fields") if isinstance(payload.get("fields"), list) else []
        if not fields:
            fields = [{"id": "text", "type": "textarea", "label": "Reply", "required": True, "options": []}]
        fields = [dict(field) for field in fields]
        for field in fields:
            if field.get("type") == "boolean" and not field.get("options"):
                field["options"] = [
                    {"value": False, "label": "No", "description": ""},
                    {"value": True, "label": "Yes", "description": ""},
                ]
        return cls(
            kind="human",
            title=str(payload.get("title") or "Input required"),
            question=str(payload.get("question") or "Please provide the requested information."),
            reason=str(payload.get("reason") or ""),
            fields=fields,
            interaction_id=str(payload.get("interactionId") or ""),
            recommendation=payload.get("recommendation") if isinstance(payload.get("recommendation"), dict) else {},
            impact=[str(item) for item in payload.get("impact", [])],
        )

    @classmethod
    def approval(cls, calls: list[dict[str, Any]]) -> "InteractionController":
        from agent.network import network_requested

        declared: list[str] = []
        for call in calls:
            name = str(call.get("name") or "tool")
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            if name == "execute" and network_requested(args):
                declared.append("HOST NETWORK (internet, localhost, LAN)")
            elif name in {"write_file", "edit_file", "delete"}:
                declared.append(name.upper())
        caps = ", ".join(dict.fromkeys(declared)) if declared else "declared capabilities"
        return cls(
            kind="approval",
            title="Approve tool call?",
            question=f"Approve runs this call with its declared capabilities ({caps}).",
            fields=[{
                "id": "approved", "type": "single_select", "label": "Decision", "required": True,
                "options": [
                    {"value": "reject", "label": "Reject", "description": "Deny this tool call"},
                    {"value": "approve", "label": "Run", "description": "Approve and execute"},
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
        return str(self.current.get("type")) in {"text", "textarea"}

    def move(self, delta: int) -> None:
        options = self.current.get("options") or []
        if options:
            self.option_index = (self.option_index + delta) % len(options)

    def toggle(self) -> None:
        if self.current.get("type") != "multi_select":
            return
        options = self.current.get("options") or []
        if not options:
            return
        field_id = str(self.current.get("id") or "field")
        selected = list(self.values.get(field_id) or [])
        value = str(options[self.option_index].get("value"))
        selected.remove(value) if value in selected else selected.append(value)
        self.values[field_id] = selected

    def accept(self, text: str = "") -> bool:
        field = self.current
        field_id = str(field.get("id") or f"field_{self.index}")
        field_type = str(field.get("type") or "text")
        options = field.get("options") or []
        if field_type in {"text", "textarea"}:
            value: Any = text.strip()
        elif field_type == "multi_select":
            value = self.values.get(field_id) or []
        elif options:
            raw_value = options[self.option_index].get("value")
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

    def render(self) -> Group:
        parts: list[Any] = [Text(self.title, style="bold bright_cyan")]
        if self.reason:
            parts.append(Text(self.reason, style="dim"))
        if self.question:
            parts.append(Text(self.question) if self.kind == "approval" else Markdown(self.question))
        if self.recommendation:
            value = self.recommendation.get("value") or ""
            why = self.recommendation.get("reason") or ""
            parts.append(Text(f"Recommended: {value} {why}".strip(), style="cyan"))
        if self.impact:
            parts.append(Text("Impact:\n" + "\n".join(f"• {item}" for item in self.impact), style="yellow"))
        field = self.current
        parts.append(Text(f"{self.index + 1}/{len(self.fields)}  {field.get('label') or field.get('id')}", style="bold"))
        options = field.get("options") or []
        selected = self.values.get(str(field.get("id") or "")) or []
        for idx, option in enumerate(options):
            pointer = "→" if idx == self.option_index else " "
            mark = "[x]" if option.get("value") in selected else "[ ]" if field.get("type") == "multi_select" else ""
            description = option.get("description") or ""
            style = "bold cyan" if idx == self.option_index else ""
            label = f"{pointer} {mark} {option.get('label')}  {description}".rstrip()
            if right_label := option.get("right_label"):
                row = Table.grid(expand=True, padding=(0, 1))
                row.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
                row.add_column(width=len(str(right_label)), justify="right", no_wrap=True)
                row.add_row(Text(label, style=style), Text(str(right_label), style=style))
                parts.append(row)
            else:
                parts.append(Text(label, style=style))
        if self.error:
            parts.append(Text(self.error, style="red"))
        hint = (
            "↑↓ select  Enter confirm  Esc cancel · F2 to reopen"
            if self.kind in {"approval", "human", "pause"} and options
            else "↑↓ select  Enter confirm  Esc cancel"
            if options
            else "Enter submit  Ctrl+J newline  Esc cancel"
        )
        parts.append(Text(hint, style="dim"))
        return Group(*parts)
