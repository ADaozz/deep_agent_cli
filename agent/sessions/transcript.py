"""Translate checkpoint messages to presentation-neutral transcript values."""

from __future__ import annotations

from datetime import datetime

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from agent.sessions.contracts import TranscriptBlock
from agent.sessions.images import refs_from_message
from agent.models.messages import message_text, reasoning_text, visible_text
from agent.subagents.notifications import is_background_launch, parse_notification
from agent.tools.catalog import ROOT_TASK_TOOLS


_CARD_FROM_STATUS = {
    "running": "running",
    "completed": "completed",
    "failed": "error",
    "cancelled": "cancelled",
    "unconfirmed": "unconfirmed",
}
_TASK_STATUSES = frozenset({"running", "completed", "failed", "cancelled"})


def task_result_text(*, artifact: object = None, content: str = "", status: str = "") -> str:
    """The task body shown on a card. Launch JSON is not a result."""
    if isinstance(artifact, dict) and isinstance(artifact.get("result"), str):
        return artifact["result"]
    recorded = artifact.get("status") if isinstance(artifact, dict) else None
    if recorded == "running" or status == "running":
        return ""
    return content


def subagent_card_status(
    *,
    is_error: bool = False,
    status: str | None = None,
    artifact: object = None,
) -> str:
    """Card status from a structured task record. Missing data stays unconfirmed."""
    recorded = artifact.get("status") if isinstance(artifact, dict) else None
    if recorded not in _CARD_FROM_STATUS and status in _CARD_FROM_STATUS:
        recorded = status
    if recorded in _CARD_FROM_STATUS:
        return _CARD_FROM_STATUS[recorded]
    if is_error:
        return "error"
    return "unconfirmed"


def classify_tool_result(
    *,
    status: str = "",
    content: str = "",
    name: str = "",
    artifact: object = None,
) -> str:
    """Classify a tool result for transcripts, Companion, and subagent events.

    Returns ``cancelled``, ``failed``, or ``success``. Prefer structured
    ``status`` / execute artifacts over free text. Content matching only
    accepts the project's ``Error:`` envelope so a successful read of a file
    that starts with ``Error handling…`` is not counted as a failure.
    """
    status = str(status or "")
    text = content.lower() if isinstance(content, str) else ""
    payload = artifact if isinstance(artifact, dict) else {}
    # A task payload reports the subagent. The tool call itself fails only
    # when the message status is error.
    task_payload = payload.get("mode") in {"sync", "background"} and payload.get("status") in _TASK_STATUSES
    if name in ROOT_TASK_TOOLS or task_payload:
        return "failed" if status == "error" else "success"
    termination = payload.get("termination_reason")
    # execute: structured artifact wins over free-text phrases in command output.
    if name == "execute":
        if payload.get("running") is True:
            return "success"
        exit_code = payload.get("exit_code")
        if termination == "cancelled":
            return "cancelled"
        if (
            status == "error"
            or termination is not None
            or (isinstance(exit_code, int) and exit_code != 0)
        ):
            return "failed"
        return "success"
    if name == "write_stdin":
        if status == "error" or payload.get("stdin_error"):
            if termination == "cancelled":
                return "cancelled"
            return "failed"
        # Tool Call Outcome: stdin delivery succeeded regardless of command output.
        return "success"
    if termination == "cancelled" or "cancelled by user" in text:
        return "cancelled"
    if status == "error" or text.lstrip().startswith("error:"):
        return "failed"
    return "success"


def _execution_output(artifact: object) -> str | None:
    """Return the raw per-call output stored in the ToolMessage artifact."""
    if isinstance(artifact, dict) and isinstance(artifact.get("output"), str):
        return artifact["output"]
    return None


def _append_bounded_execution_output(
    existing: str,
    delta: str,
    *,
    max_output_bytes: int | None,
) -> tuple[str, bool]:
    """Append exact raw output while honoring UTF-8 ``max_output_bytes``."""
    if not delta:
        return existing, False
    combined = existing + delta
    if max_output_bytes is None or max_output_bytes <= 0:
        return combined, False
    raw = bytearray(combined.encode("utf-8"))
    if len(raw) <= max_output_bytes:
        return combined, False
    del raw[:-max_output_bytes]
    head = 0
    while head < len(raw) and (raw[head] & 0xC0) == 0x80:
        head += 1
    del raw[:head]
    return bytes(raw).decode("utf-8", errors="replace"), True


def tool_message_is_error(message: ToolMessage) -> bool:
    """Match live tool_completed error classification, including execute artifacts."""
    return classify_tool_result(
        status=str(getattr(message, "status", "") or ""),
        content=message_text(message),
        name=str(getattr(message, "name", "") or ""),
        artifact=getattr(message, "artifact", None),
    ) != "success"


def messages_to_transcript(
    messages: list[BaseMessage], *, turn_timings: dict[str, tuple[float, datetime]] | None = None,
    include_thinking: bool = True,
) -> list[TranscriptBlock]:
    """Rebuild a presentation-neutral transcript from LangGraph checkpoint messages."""
    blocks: list[TranscriptBlock] = []
    tools: dict[str, TranscriptBlock] = {}
    executions: dict[str, TranscriptBlock] = {}
    for message in messages:
        if isinstance(message, HumanMessage):
            notice = parse_notification(message)
            if notice is not None:
                # A host notification is the outcome of a background card,
                # not something the user typed.
                marker, result = notice
                block = tools.get(str(marker.get("tool_call_id") or ""))
                if block is not None and is_background_launch(block.name, block.arguments):
                    block.content = result
                    block.status = subagent_card_status(status=str(marker.get("status") or ""))
                    block.is_error = block.status in {"error", "cancelled"}
                continue
            blocks.append(TranscriptBlock(
                kind="user",
                content=message_text(message),
                attachments=refs_from_message(message),
            ))
        elif isinstance(message, AIMessage):
            thinking = reasoning_text(message) if include_thinking else ""
            text = visible_text(message)
            if thinking or text:
                blocks.append(TranscriptBlock(kind="assistant", content=text, thinking=thinking))
            for call in message.tool_calls or []:
                name = str(call.get("name") or "tool")
                if name == "write_stdin":
                    # Model-visible tool record stays in messages; TUI cards stay on execute.
                    continue
                block = TranscriptBlock(
                    kind="tool",
                    tool_call_id=str(call.get("id") or ""),
                    name=name,
                    arguments=call.get("args") if isinstance(call.get("args"), dict) else {},
                    status="running",
                    message_id=message.id or "",
                )
                blocks.append(block)
                tools[block.tool_call_id] = block
            timing = (turn_timings or {}).get(message.id or "")
            if timing is not None:
                blocks.append(TranscriptBlock(
                    kind="turn_summary", elapsed_seconds=timing[0], finished_at=timing[1],
                ))
        elif isinstance(message, ToolMessage):
            artifact = getattr(message, "artifact", None)
            if getattr(message, "name", None) == "write_stdin":
                if isinstance(artifact, dict) and artifact.get("execution_id"):
                    exec_id = str(artifact["execution_id"])
                    owner = executions.get(exec_id)
                    if owner is not None and not artifact.get("stdin_error") and not tool_message_is_error(message):
                        existing = owner.artifact if isinstance(owner.artifact, dict) else {}
                        merged = {**existing, **artifact}
                        cap_raw = merged.get("max_output_bytes")
                        cap = int(cap_raw) if isinstance(cap_raw, int) and not isinstance(cap_raw, bool) else None
                        owner.content, clipped = _append_bounded_execution_output(
                            owner.content, _execution_output(artifact) or "", max_output_bytes=cap,
                        )
                        if clipped:
                            merged["truncated"] = True
                        if artifact.get("running") is False:
                            merged["running"] = False
                            outcome = classify_tool_result(name="execute", artifact=merged)
                            owner.is_error = outcome != "success"
                            owner.status = "error" if owner.is_error else "completed"
                            code = merged.get("exit_code")
                            owner.exit_code = code if isinstance(code, int) and not isinstance(code, bool) else None
                        owner.artifact = merged
                continue
            content = message_text(message)
            tool_call_id = str(getattr(message, "tool_call_id", "") or "")
            is_error = tool_message_is_error(message)
            block = tools.get(tool_call_id)
            is_task = message.name == "start_task" or (block is not None and block.name == "start_task")
            if (
                getattr(message, "name", None) == "execute"
                and isinstance(artifact, dict)
                and artifact.get("running") is True
            ):
                if block is not None:
                    exec_id = str(artifact.get("execution_id") or "")
                    cap_raw = artifact.get("max_output_bytes")
                    cap = int(cap_raw) if isinstance(cap_raw, int) and not isinstance(cap_raw, bool) else None
                    output = _execution_output(artifact)
                    bounded, clipped = _append_bounded_execution_output(
                        "", content if output is None else output, max_output_bytes=cap,
                    )
                    block.content = bounded
                    block.artifact = {**artifact, "truncated": True} if clipped else dict(artifact)
                    block.status = "running"
                    block.is_error = False
                    block.exit_code = None
                    if exec_id:
                        executions[exec_id] = block
                continue
            status = (
                subagent_card_status(is_error=is_error, artifact=artifact)
                if is_task else ("error" if is_error else "completed")
            )
            is_error = status in {"error", "cancelled"}
            code = (
                artifact.get("exit_code")
                if isinstance(artifact, dict) and getattr(message, "name", None) == "execute"
                else None
            )
            exit_code = code if isinstance(code, int) and not isinstance(code, bool) else None
            display_content = content
            if getattr(message, "name", None) == "execute" and isinstance(artifact, dict):
                output = _execution_output(artifact)
                if output is not None:
                    display_content = output
            if block is not None and is_background_launch(block.name, block.arguments):
                # A running launch leaves the card open. A tool error settles it.
                card = subagent_card_status(is_error=is_error, artifact=artifact)
                if card == "running":
                    block.content = content
                    block.artifact = artifact
                    block.status = "running"
                    block.is_error = False
                    continue
                status, is_error = card, card in {"error", "cancelled"}
            if block is not None:
                block.content = display_content
                block.is_error = is_error
                block.status = status
                block.exit_code = exit_code
                block.artifact = artifact
            else:
                block = TranscriptBlock(
                    kind="tool",
                    tool_call_id=tool_call_id,
                    name="start_task" if is_task else str(getattr(message, "name", "") or "tool"),
                    content=display_content,
                    is_error=is_error,
                    status=status,
                    exit_code=exit_code,
                    artifact=artifact,
                )
                blocks.append(block)
                tools[tool_call_id] = block
    return blocks


def settle_restored_tools(
    blocks: list[TranscriptBlock], *, waiting_ids: set[str] | None = None,
    waiting_human: bool = False,
) -> None:
    """Freeze tool calls without results when showing a saved checkpoint."""
    pending = waiting_ids or set()
    seen = set()
    for block in reversed(blocks):
        if block.kind != "tool" or block.status != "running":
            continue
        key = block.tool_call_id
        if key not in seen and (key in pending or (waiting_human and block.name == "request_human_input")):
            block.status = "waiting"
        else:
            block.status = "interrupted"
        seen.add(key)
