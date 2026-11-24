"""Completion notices of background tasks, as they appear in the conversation."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from agent.tools.catalog import HISTORICAL_BACKGROUND_START, TASK_TOOL

# Historical launch name. Restored transcripts still use it; it is not registered.
START_TOOL = HISTORICAL_BACKGROUND_START


def background_requested(arguments: object) -> bool:
    """True when a start_task call asked to run in the background."""
    if not isinstance(arguments, dict):
        return False
    value = arguments.get("run_in_background", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def is_task_tool(name: str) -> bool:
    return name in {TASK_TOOL, START_TOOL}


def is_background_launch(name: str, arguments: object = None) -> bool:
    """A launch whose card stays open until the task itself finishes.

    The old ``start_background_task`` name is always a background launch.
    ``start_task`` is one only when ``run_in_background`` is true.
    """
    if name == START_TOOL:
        return True
    return name == TASK_TOOL and background_requested(arguments)

NOTIFICATION_KEY = "deep_agent_notification"
_SEPARATOR = "\n\n"


def notification_message(item: dict[str, Any]) -> HumanMessage:
    """The result enters the conversation as a marked host message, never as user input."""
    header = (
        f"[Background subagent finished · run_id={item['run_id']} · status={item['status']}]\n"
        "Automatic notification from the host, not a message from the user."
    )
    return HumanMessage(
        content=f"{header}{_SEPARATOR}{item['content']}",
        additional_kwargs={NOTIFICATION_KEY: {
            "run_id": item["run_id"], "tool_call_id": item["tool_call_id"], "status": item["status"],
        }},
    )


def parse_notification(message: BaseMessage) -> tuple[dict[str, Any], str] | None:
    """The marker and the reported result, or None for an ordinary message."""
    marker = getattr(message, "additional_kwargs", {}).get(NOTIFICATION_KEY)
    if not isinstance(marker, dict):
        return None
    content = message.content if isinstance(message.content, str) else ""
    return marker, content.partition(_SEPARATOR)[2]
