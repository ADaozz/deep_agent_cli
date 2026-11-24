"""Subagent tools. The delegated task is the description; there is no subagent type."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, Field, ValidationError, model_validator
from pydantic_core import InitErrorDetails

from agent.companion.config import read_prompt
from agent.subagents.log_store import SubAgentLogStore
from agent.subagents.manager import SubAgentManager, TaskRecord
from agent.subagents.runtime import SubAgentRuntime

_INJECTED = frozenset({"runtime"})
_PREVIEW = 200

READ_TASK_DESCRIPTION = """\
List your direct child tasks or inspect a specific task.

Omit run_id to list direct children.

Set include_logs=true with a specific run_id to inspect
its execution logs.

Use offset and limit to paginate task lists
or execution logs.

Use this tool to inspect available capacity, investigate
failures, review execution details, or retrieve
historical results.

The response includes the current subagent capacity.

Background results are delivered automatically.
Do not use this tool to poll or wait for completion.

Only your direct child tasks are accessible."""

CANCEL_TASK_DESCRIPTION = """\
Request cancellation of a background subagent belonging
to the current agent.

Cancellation does not affect other subagents.

A successful cancellation request does not mean
that the task has already stopped."""

_PROMPT_DIR = Path(__file__).parent / "prompts"
_CAPACITY = "Call `read_task()` to read the current subagent capacity."
_DESCRIPTION = (
    "A self-contained task description including the objective, "
    "relevant context, constraints, and expected deliverables. "
    "The subagent does not inherit your conversation history."
)
_BACKGROUND = (
    "False: execute synchronously and return the final result. "
    "True: execute independently in the background, return the run_id immediately, "
    "and automatically deliver the final result upon completion."
)


def start_task_description(*, allow_background: bool) -> str:
    """Shared delegation policy plus the execution mode this agent can actually use."""
    mode = "delegation_root.md" if allow_background else "delegation_sync.md"
    return f"{read_prompt(_PROMPT_DIR / 'delegation.md')}\n\n{read_prompt(_PROMPT_DIR / mode)}\n\n{_CAPACITY}"


def _reject_unknown(cls: type[BaseModel], data: object, *, hint: str) -> object:
    """Refuse keys the schema does not define.

    ``extra="forbid"`` cannot be used: the tool node injects ``runtime`` after
    validation, and that name is not a model field.
    """
    if isinstance(data, dict):
        unknown = sorted({str(key) for key in data} - set(cls.model_fields) - _INJECTED)
        if unknown:
            errors: list[InitErrorDetails] = []
            for key in unknown:
                message = f"Unexpected argument {key!r}; {hint}"
                errors.append({
                    "type": "value_error",
                    "loc": (key,),
                    "input": data[key],
                    "ctx": {"error": ValueError(message)},
                })
            raise ValidationError.from_exception_data(cls.__name__, errors)
    return data


def _parent_id(runtime: ToolRuntime) -> str:
    config = getattr(runtime, "config", None) or {}
    parent = str((config.get("configurable") or {}).get("thread_id") or "")
    if parent:
        return parent
    from langgraph.config import get_config

    return str(get_config().get("configurable", {}).get("thread_id", ""))


def _preview(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= _PREVIEW:
        return compact
    return compact[: _PREVIEW - 1] + "…"


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _public_task(fields: dict[str, Any], *, with_result: bool) -> dict[str, Any]:
    depth = fields.get("depth")
    item: dict[str, Any] = {
        "run_id": fields.get("run_id") or "",
        "parent_run_id": fields.get("parent_run_id") or "",
        "tool_call_id": fields.get("tool_call_id") or "",
        "depth": depth if type(depth) is int and not isinstance(depth, bool) and depth > 0 else 1,
        "description": _preview(str(fields.get("description") or "")),
        "mode": fields.get("mode") or "sync",
        "status": fields.get("status") or "unconfirmed",
        "elapsed_seconds": fields.get("elapsed_seconds"),
    }
    if not with_result:
        return item
    if fields.get("summary_truncated"):
        item["result_summary"] = fields.get("result_summary") or ""
        item["summary_truncated"] = True
    elif fields.get("status") != "running" and fields.get("result") is not None:
        item["result"] = fields.get("result")
    return item


def _task_fields(task: TaskRecord, *, with_result: bool) -> dict[str, Any]:
    return _public_task({
        "run_id": task.run_id,
        "parent_run_id": task.parent_run_id,
        "tool_call_id": task.tool_call_id,
        "depth": task.depth,
        "description": task.description,
        "mode": task.mode,
        "status": task.status,
        "elapsed_seconds": task.elapsed_seconds,
        "result": task.result,
    }, with_result=with_result)


def _pack(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return _dump(payload), payload


class ReadTaskSchema(BaseModel):
    run_id: str | None = Field(
        default=None,
        description="Direct child run to inspect. Omit to list direct children.",
    )
    include_logs: bool = Field(
        default=False,
        description="When true, include one page of that task's execution log. Requires run_id.",
    )
    offset: int = Field(default=0, description="Zero-based page start.")
    limit: int = Field(default=100, description="Page size, from 1 to 100.")

    @model_validator(mode="before")
    @classmethod
    def _unknown(cls, data: object) -> object:
        return _reject_unknown(
            cls, data, hint="only run_id, include_logs, offset, and limit are accepted.",
        )


class CancelTaskSchema(BaseModel):
    run_id: str = Field(description="Background run id belonging to this agent.")

    @model_validator(mode="before")
    @classmethod
    def _unknown(cls, data: object) -> object:
        return _reject_unknown(cls, data, hint="only run_id is accepted.")


class StartTaskSchema(BaseModel):
    description: str = Field(description=_DESCRIPTION)
    run_in_background: bool = Field(default=False, description=_BACKGROUND)

    @model_validator(mode="before")
    @classmethod
    def _unknown(cls, data: object) -> object:
        return _reject_unknown(cls, data, hint="put all instructions for the subagent in description.")


class StartTaskSyncSchema(BaseModel):
    description: str = Field(description=_DESCRIPTION)

    @model_validator(mode="before")
    @classmethod
    def _unknown(cls, data: object) -> object:
        return _reject_unknown(cls, data, hint="put all instructions for the subagent in description.")


def build_subagent_tools(
    runtime: SubAgentRuntime | None,
    manager: SubAgentManager | None,
    store: SubAgentLogStore,
    *,
    names: frozenset[str],
    allow_background: bool,
) -> list[StructuredTool]:
    """Build exactly the task tools in ``names``. The caller chooses the set."""
    tools: list[StructuredTool] = []
    if "read_task" in names:
        tools.append(_read_tool(manager, store))
    if "cancel_task" in names:
        tools.append(_cancel_tool(manager))
    if "start_task" in names:
        if runtime is None:
            raise ValueError("start_task requires a subagent runtime")
        tools.append(_start_tool(runtime, allow_background=allow_background))
    return tools


def _start_tool(runtime_: SubAgentRuntime, *, allow_background: bool) -> StructuredTool:
    description = start_task_description(allow_background=allow_background)

    def start_task(
        description: str,
        runtime: ToolRuntime,
        run_in_background: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        text = description.strip()
        if not text:
            raise ToolException("description is required")
        if run_in_background and not allow_background:
            raise ToolException("background delegation is not available to this agent")
        parent = _parent_id(runtime)
        if not parent:
            raise ToolException("parent session ID unavailable")
        if run_in_background:
            try:
                task = runtime_.start_background(text, parent, str(runtime.tool_call_id or ""))
            except Exception as exc:
                raise ToolException(str(exc)) from exc
            payload = {"run_id": task.run_id, "mode": "background", "status": "running"}
            return _pack(payload)
        output = runtime_.invoke(
            {"messages": [HumanMessage(content=text)]},
            config=runtime.config,
        )
        message = output["messages"][-1]
        recorded = message.additional_kwargs.get("status")
        run_id = str(message.additional_kwargs.get("run_id") or "")
        if recorded not in {"completed", "failed", "cancelled"} or not run_id:
            raise ToolException(str(message.content))
        payload = {
            "run_id": run_id,
            "mode": "sync",
            "status": recorded,
            "result": message.additional_kwargs.get("result", message.content),
        }
        return _pack(payload)

    schema = StartTaskSchema if allow_background else StartTaskSyncSchema
    return StructuredTool.from_function(
        start_task,
        name="start_task",
        description=description,
        args_schema=schema,
        response_format="content_and_artifact",
        handle_tool_error=True,
    )


def _merged_task_ids(
    manager: SubAgentManager | None, store: SubAgentLogStore, parent: str,
) -> tuple[list[str], dict[str, TaskRecord]]:
    """Snapshot live tasks, then merge their IDs with historical runs.

    The result is the paging order. Historical files are not opened here.
    """
    live = manager.tasks_for(parent) if manager is not None else []
    by_id = {task.run_id: task for task in live}
    logged = store.list_runs(parent)
    logged_ids = set(logged)
    ordered = [task.run_id for task in reversed(live) if task.run_id not in logged_ids]
    ordered.extend(logged)
    return ordered, by_id


def _tasks_for_ids(
    store: SubAgentLogStore, parent: str, run_ids: list[str], by_id: dict[str, TaskRecord],
) -> list[dict[str, Any]]:
    """Materialize one page. Live records win; only historical IDs open a log."""
    items: list[dict[str, Any]] = []
    for run_id in run_ids:
        task = by_id.get(run_id)
        if task is not None:
            items.append(_task_fields(task, with_result=False))
            continue
        meta = store.task_metadata(parent, run_id)
        if meta is not None:
            items.append(_public_task(meta, with_result=False))
    return items


def _read_tool(manager: SubAgentManager | None, store: SubAgentLogStore) -> StructuredTool:
    def read_task(
        runtime: ToolRuntime,
        run_id: str | None = None,
        include_logs: bool = False,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[str, dict[str, Any]]:
        parent = _parent_id(runtime)
        if not parent:
            raise ToolException("Parent session ID unavailable")
        if include_logs and not run_id:
            raise ToolException("include_logs requires run_id")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ToolException("offset must be non-negative and limit must be 1..100")

        def finish(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            if manager is not None:
                payload["capacity"] = manager.capacity_snapshot(parent)
            return _pack(payload)

        try:
            if run_id is None:
                ordered, by_id = _merged_task_ids(manager, store, parent)
                page_ids = ordered[offset:offset + limit]
                page = _tasks_for_ids(store, parent, page_ids, by_id)
                return finish({"tasks": page, "next_offset": offset + len(page_ids)})
            task = manager.task(run_id, parent) if manager is not None else None
            if task is None:
                meta = store.task_metadata(parent, run_id)
                if meta is None:
                    raise ToolException("Subagent log not found in this parent session")
                item = _public_task(meta, with_result=True)
            else:
                item = _task_fields(task, with_result=True)
            if include_logs:
                item["logs"] = store.read(parent, run_id, offset, limit)
            return finish(item)
        except ToolException:
            raise
        except (ValueError, OSError) as exc:
            raise ToolException(str(exc)) from exc

    return StructuredTool.from_function(
        read_task,
        name="read_task",
        description=READ_TASK_DESCRIPTION,
        args_schema=ReadTaskSchema,
        response_format="content_and_artifact",
        handle_tool_error=True,
    )


def _cancel_tool(manager: SubAgentManager | None) -> StructuredTool:
    def cancel_task(run_id: str, runtime: ToolRuntime) -> tuple[str, dict[str, Any]]:
        parent = _parent_id(runtime)
        if not parent:
            raise ToolException("Parent session ID unavailable")
        task = manager.task(run_id, parent) if manager is not None else None
        if task is None or task.mode != "background":
            raise ToolException(f"background task {run_id} not found for this agent")
        if task.status != "running":
            payload = {"run_id": run_id, "status": task.status, "cancellation": "already_finished"}
            return _pack(payload)
        manager.cancel(run_id, parent)
        payload = {"run_id": run_id, "status": "running", "cancellation": "requested"}
        return _pack(payload)

    return StructuredTool.from_function(
        cancel_task,
        name="cancel_task",
        description=CANCEL_TASK_DESCRIPTION,
        args_schema=CancelTaskSchema,
        response_format="content_and_artifact",
        handle_tool_error=True,
    )
