"""Background delegation: start a subagent and keep working while it runs."""

from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agent.subagents.manager import BackgroundTask, SubAgentManager
from agent.subagents.notifications import CANCEL_TOOL, CHECK_TOOL, START_TOOL
from agent.subagents.runtime import SubAgentRuntime


DESCRIPTION_PREVIEW = 200


def _parent(runtime: ToolRuntime) -> str:
    return str(runtime.config.get("configurable", {}).get("thread_id", ""))


def _entry(task: BackgroundTask, *, with_result: bool) -> dict[str, Any]:
    description = " ".join(task.description.split())
    entry: dict[str, Any] = {
        "run_id": task.run_id, "status": task.status,
        "description": description if len(description) <= DESCRIPTION_PREVIEW
        else description[:DESCRIPTION_PREVIEW - 1] + "…",
    }
    if with_result and task.status != "running":
        entry["result"] = task.result
    return entry


def build_background_task_tools(
    runtime_: SubAgentRuntime, manager: SubAgentManager, *, unavailable: str = "",
) -> list[Any]:
    @tool(START_TOOL)
    def start_background_task(runtime: ToolRuntime, description: str) -> str:
        """Start an isolated general-purpose subagent in the background and return immediately.

        Use it for an independent subtask that takes a while and whose result you
        do not need for your next step, so you can keep working or answer the user
        meanwhile. Use `start_task` instead when you must wait for the result anyway.
        The subagent only sees `description`: make it self-contained. It shares
        the workspace with you, so do not edit the files it is working on.
        The result is delivered to you automatically as a host notification when
        the subagent finishes, even after your turn ended. Do not poll for it.
        Background tasks share the concurrency limit with `start_task`.
        """
        parent = _parent(runtime)
        if not parent:
            return "Subagent failed: parent session ID unavailable"
        if not description.strip():
            return "Subagent failed: description is empty"
        if unavailable:
            return f"Subagent failed: {unavailable}"
        try:
            started = runtime_.start_background(description, parent, str(runtime.tool_call_id or ""))
        except Exception as exc:  # noqa: BLE001
            return f"Subagent failed: {str(exc)[:1000]}"
        return (
            f"Background subagent started (run_id={started.run_id}). It runs while you continue. "
            "Its result arrives automatically as a host notification; do not poll."
        )

    @tool(CHECK_TOOL)
    def check_background_task(runtime: ToolRuntime, run_id: str | None = None) -> dict:
        """List this session's background subagents, or read the status and result of one.

        Results are delivered automatically; use this only to look at a result
        again or when the user asks about progress.
        """
        parent = _parent(runtime)
        if run_id is None:
            return {"tasks": [_entry(item, with_result=False) for item in manager.background_tasks(parent)]}
        found = manager.background_task(run_id, parent)
        if found is None:
            return {"error": "Background task not found in this session"}
        return _entry(found, with_result=True)

    @tool(CANCEL_TOOL)
    def cancel_background_task(runtime: ToolRuntime, run_id: str) -> str:
        """Stop a running background subagent. A cancelled task sends no result notification."""
        found = manager.cancel(run_id, _parent(runtime))
        if found is None:
            return "Error: background task not found in this session"
        if found.status != "running":
            return f"Background task {run_id} already finished with status {found.status}."
        return f"Cancellation requested for background task {run_id}."

    return [start_background_task, check_background_task, cancel_background_task]
