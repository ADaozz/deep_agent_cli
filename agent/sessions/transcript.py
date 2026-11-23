"""Translate checkpoint messages to presentation-neutral transcript values."""

from __future__ import annotations

from datetime import datetime

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from agent.sessions.contracts import TranscriptBlock
from agent.sessions.images import refs_from_message
from agent.models.messages import message_text, reasoning_text, visible_text
from agent.subagents.notifications import START_TOOL, parse_notification


def subagent_result_status(content: str, *, is_error: bool = False) -> str:
    """Interpret the runtime's failure envelope, never text inside a child answer.

    A delivered answer is prefixed with ``[subagent ``. Phrases inside that
    answer, including ``cancelled by user``, are the subject of the work.
    ``is_error`` only applies when the tool result itself is not that answer:
    a generic flag computed from body text must not turn a finished review red.
    """
    text = content.lstrip().lower()
    if text.startswith("[subagent "):
        return "completed"
    if text.startswith(("subagent cancelled:", "subagent cancelled (")):
        return "cancelled"
    if text.startswith(("subagent failed:", "subagent failed (")):
        return "error"
    if is_error:
        return "error"
    return "completed"


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
    termination = payload.get("termination_reason")
    # execute: structured artifact wins over free-text phrases in command output.
    if name == "execute":
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
    # A finished child answer often quotes the cancellation path it reviewed.
    # That phrase is not this tool's own cancel envelope.
    if text.lstrip().startswith("[subagent "):
        return "success"
    if termination == "cancelled" or "cancelled by user" in text:
        return "cancelled"
    if status == "error" or text.lstrip().startswith("error:"):
        return "failed"
    return "success"


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
    for message in messages:
        if isinstance(message, HumanMessage):
            notice = parse_notification(message)
            if notice is not None:
                # A host notification is the outcome of a background card,
                # not something the user typed.
                marker, result = notice
                block = tools.get(str(marker.get("tool_call_id") or ""))
                if block is not None and block.name == START_TOOL:
                    block.content = result
                    block.status = subagent_result_status(result)
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
                block = TranscriptBlock(
                    kind="tool",
                    tool_call_id=str(call.get("id") or ""),
                    name=str(call.get("name") or "tool"),
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
            content = message_text(message)
            tool_call_id = str(getattr(message, "tool_call_id", "") or "")
            is_error = tool_message_is_error(message)
            block = tools.get(tool_call_id)
            is_task = message.name == "start_task" or (block is not None and block.name == "start_task")
            status = subagent_result_status(content, is_error=is_error) if is_task else ("error" if is_error else "completed")
            is_error = status in {"error", "cancelled"}
            artifact = getattr(message, "artifact", None)
            code = (
                artifact.get("exit_code")
                if isinstance(artifact, dict) and getattr(message, "name", None) == "execute"
                else None
            )
            exit_code = code if isinstance(code, int) and not isinstance(code, bool) else None
            if message.name == START_TOOL and status == "completed":
                # Only the launch returned. The card stays open until its
                # notification arrives; without one it is settled as interrupted.
                refused = subagent_result_status(content, is_error=is_error)
                if refused == "completed":
                    continue
                status, is_error = refused, True
            if block is not None:
                block.content = content
                block.is_error = is_error
                block.status = status
                block.exit_code = exit_code
                block.artifact = artifact
            else:
                block = TranscriptBlock(
                    kind="tool",
                    tool_call_id=tool_call_id,
                    name="start_task" if is_task else str(getattr(message, "name", "") or "tool"),
                    content=content,
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
