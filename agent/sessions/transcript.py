"""Translate checkpoint messages to presentation-neutral transcript values."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

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


def is_execution_terminal(artifact: object, status: str) -> bool:
    """True once an execute record has reached a confirmed terminal state."""
    return (
        isinstance(artifact, dict)
        and artifact.get("running") is not True
        and status in {"completed", "error"}
    )


def execution_exit_code(artifact: object) -> int | None:
    if not isinstance(artifact, dict):
        return None
    code = artifact.get("exit_code")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def execution_max_output_bytes(artifact: object) -> int | None:
    if not isinstance(artifact, dict):
        return None
    cap = artifact.get("max_output_bytes")
    return cap if isinstance(cap, int) and not isinstance(cap, bool) else None


def project_execution_update(
    *,
    current_output: str,
    current_artifact: object,
    current_status: str,
    update_artifact: dict[str, Any],
    output_delta: str | None = None,
    fallback_output: str = "",
    output_mode: Literal["append", "replace", "replace_if_empty", "keep"] = "keep",
    event_is_error: bool = False,
) -> tuple[str, dict[str, Any], str, bool, int | None] | None:
    """Unified execution state projection shared by live events and history restore.

    Returns ``(output, artifact, status, is_error, exit_code)``, or ``None`` if a
    late ``running=True`` update arrives after a confirmed terminal state.
    """
    incoming_running = update_artifact.get("running") is True
    if incoming_running and is_execution_terminal(current_artifact, current_status):
        return None

    existing = current_artifact if isinstance(current_artifact, dict) else {}
    merged = {**existing, **update_artifact}
    merged.pop("thread_id", None)
    cap = execution_max_output_bytes(merged)

    output = current_output
    clipped = False
    if output_mode == "append":
        if output_delta:
            output, clipped = _append_bounded_execution_output(
                current_output, output_delta, max_output_bytes=cap,
            )
    elif output_mode == "replace" or (output_mode == "replace_if_empty" and not current_output):
        if output_delta is not None:
            output, clipped = _append_bounded_execution_output(
                "", output_delta, max_output_bytes=cap,
            )
        elif fallback_output:
            output = fallback_output
    if clipped:
        merged["truncated"] = True

    if incoming_running:
        merged["running"] = True
        return output, merged, "running", False, None

    if "running" in merged or "execution_id" in merged:
        merged["running"] = False
    exit_code = execution_exit_code(merged)
    outcome = classify_tool_result(
        status="error" if event_is_error else "",
        name="execute",
        artifact=merged,
    )
    is_error = outcome != "success"
    status = "error" if is_error else "completed"
    return output, merged, status, is_error, exit_code


def _apply_transcript_execution(
    block: TranscriptBlock,
    projected: tuple[str, dict[str, Any], str, bool, int | None] | None,
) -> None:
    if projected is None:
        return
    block.content, block.artifact, block.status, block.is_error, block.exit_code = projected


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
                        _apply_transcript_execution(
                            owner,
                            project_execution_update(
                                current_output=owner.content,
                                current_artifact=owner.artifact,
                                current_status=owner.status,
                                update_artifact=artifact,
                                output_delta=_execution_output(artifact) or "",
                                output_mode="append",
                            ),
                        )
                continue
            content = message_text(message)
            tool_call_id = str(getattr(message, "tool_call_id", "") or "")
            is_error = tool_message_is_error(message)
            block = tools.get(tool_call_id)
            is_task = message.name == "start_task" or (block is not None and block.name == "start_task")
            if getattr(message, "name", None) == "execute" and isinstance(artifact, dict):
                if block is None:
                    block = TranscriptBlock(
                        kind="tool",
                        tool_call_id=tool_call_id,
                        name="execute",
                        status="running",
                    )
                    blocks.append(block)
                    tools[tool_call_id] = block
                raw_output = _execution_output(artifact)
                _apply_transcript_execution(
                    block,
                    project_execution_update(
                        current_output=block.content,
                        current_artifact=block.artifact,
                        current_status=block.status,
                        update_artifact=artifact,
                        output_delta=content if (artifact.get("running") is True and raw_output is None) else raw_output,
                        fallback_output=content,
                        output_mode="replace",
                        event_is_error=is_error,
                    ),
                )
                exec_id = str(artifact.get("execution_id") or "")
                if exec_id:
                    executions[exec_id] = block
                continue
            status = (
                subagent_card_status(is_error=is_error, artifact=artifact)
                if is_task else ("error" if is_error else "completed")
            )
            is_error = status in {"error", "cancelled"}
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
                block.content = content
                block.is_error = is_error
                block.status = status
                block.exit_code = None
                block.artifact = artifact
            else:
                block = TranscriptBlock(
                    kind="tool",
                    tool_call_id=tool_call_id,
                    name="start_task" if is_task else str(getattr(message, "name", "") or "tool"),
                    content=content,
                    is_error=is_error,
                    status=status,
                    exit_code=None,
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
