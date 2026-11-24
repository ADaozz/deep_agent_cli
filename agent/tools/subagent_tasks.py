"""Subagent tools. The delegated task is the description; there is no subagent type."""

from __future__ import annotations

import json
from typing import Any

import threading

from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, Field, ValidationError, model_validator
from pydantic_core import InitErrorDetails

from agent.subagents.log_store import SubAgentLogStore
from agent.subagents.manager import SubAgentManager, TaskRecord
from agent.subagents.runtime import SubAgentRuntime

_INJECTED = frozenset({"runtime"})
_PREVIEW = 200

READ_TASK_DESCRIPTION = """\
List your direct child tasks or inspect a specific task's status, execution mode, final result, and optional paginated execution logs.

Omit `run_id` to list direct child tasks.

Set `include_logs=true` with a specific `run_id` to inspect execution logs.

Use this tool to investigate task progress, failures, and execution details. Do not repeatedly poll running tasks.

The response includes the current subagent capacity for this agent.

You cannot access tasks belonging to other agents or descendants beyond your direct children."""

WAIT_TASK_DESCRIPTION = """\
Wait for the specified background subagents to reach a terminal state and return their final results.

Use this tool when further progress depends on unfinished background tasks.

Already finished tasks return immediately.

Do not use repeated status queries as a substitute for waiting."""

CANCEL_TASK_DESCRIPTION = """\
Request cancellation of a specified background subagent that belongs to the current agent.

Cancellation does not affect other subagents.

A successful cancellation request does not mean that the task has already stopped."""

_COMMON = """\
Subagents are autonomous agents that execute delegated tasks in independent contexts while sharing the workspace with their parent.

#### When to delegate

Proactively delegate when:

- A task can be decomposed into independent workstreams that can execute concurrently.
- Exploration or investigation spans multiple modules, files, or information sources.
- A task produces substantial intermediate output that is unnecessary in the parent context.
- Independent investigation, verification, or review would improve the quality of the result.
- Multiple hypotheses or approaches can be investigated independently.

Launch independent subagents concurrently whenever practical.

#### When not to delegate

Do not delegate when:

- The task is simple, targeted, or requires only a few direct tool calls.
- The target file, symbol, or required operation is already known and can be handled directly.
- The work depends heavily on the current context or requires frequent coordination.
- Delegation would duplicate work already performed or assigned.
- The overhead of delegation outweighs its expected benefit.

Do not create subagents merely because capacity is available.

#### Writing delegation prompts

Each delegated task must be self-contained. Subagents do not inherit the parent's conversation history.

Provide the objective, relevant background, known findings, scope, constraints, and expected deliverables.

Clearly distinguish investigation tasks from implementation tasks.

Describe the problem to solve rather than prescribing every execution step. Allow the subagent to choose its own approach and tools.

Assign non-overlapping responsibilities when multiple subagents work concurrently.

#### Coordination

Do not duplicate work assigned to active subagents.

Do not concurrently modify files assigned to other active subagents.

Only access tasks and execution logs belonging to your own direct children.

The parent agent remains responsible for understanding, integrating, and validating all delegated results, resolving conflicts, and completing the original request."""

_ROOT_EXECUTION = """\
#### Execution mode

Use `run_in_background=false` when the subagent's result is required before proceeding.

Use `run_in_background=true` when the task can execute independently while you continue other useful work.

Never mix synchronous and background subagent calls in the same tool-call batch.

Launch independent synchronous subagents concurrently in a synchronous delegation batch.

Launch independent background subagents concurrently in a separate background-only delegation batch.

Background tasks return immediately after successful submission. Their final results are delivered automatically upon completion, including after the current turn ends.

Continue useful independent work while background subagents execute.

Use `wait_task` when further progress depends on unfinished background tasks. Do not repeatedly poll task status using `read_task`.

#### Recursive delegation

Only the root agent may launch background subagents.

Synchronous subagents may recursively delegate synchronous tasks, subject to runtime limits.

Background subagents are always leaf nodes and cannot delegate any further tasks.

Each delegation increases the agent depth by one."""

_SYNC_EXECUTION = """\
#### Execution mode

All delegated tasks execute synchronously. Each tool call returns after its delegated task has finished.

Launch independent subagents concurrently whenever practical.

#### Recursive delegation

You may only delegate synchronous tasks, subject to runtime concurrency and depth limits.

Each delegation increases the agent depth by one."""

_CAPACITY = "Call `read_task()` to read the current subagent capacity."


def start_task_description(*, allow_background: bool) -> str:
    """One shared prompt. Execution guidance is appended for the agent's real capability."""
    guidance = _ROOT_EXECUTION if allow_background else _SYNC_EXECUTION
    return f"{_COMMON}\n\n{guidance}\n\n{_CAPACITY}"


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


def _task_fields(task: TaskRecord, *, with_result: bool) -> dict[str, Any]:
    item: dict[str, Any] = {
        "run_id": task.run_id,
        "parent_run_id": task.parent_run_id,
        "tool_call_id": task.tool_call_id,
        "depth": task.depth,
        "description": _preview(task.description),
        "mode": task.mode,
        "status": task.status,
        "elapsed_seconds": task.elapsed_seconds,
    }
    if with_result and task.status != "running" and task.result is not None:
        item["result"] = task.result
    return item


def _log_fields(meta: dict[str, Any], *, with_result: bool) -> dict[str, Any]:
    item: dict[str, Any] = {
        "run_id": meta["run_id"],
        "parent_run_id": meta.get("parent_run_id") or "",
        "tool_call_id": meta.get("tool_call_id") or "",
        "depth": meta.get("depth") if type(meta.get("depth")) is int else 1,
        "description": _preview(str(meta.get("description") or "")),
        "mode": meta.get("mode") or "sync",
        "status": meta.get("status") or "unconfirmed",
        "elapsed_seconds": meta.get("elapsed_seconds"),
    }
    if not with_result:
        return item
    if meta.get("summary_truncated"):
        item["result_summary"] = meta.get("result_summary") or ""
        item["summary_truncated"] = True
    elif "result" in meta:
        item["result"] = meta.get("result")
    return item


def _pack(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return _dump(payload), payload


def _wait_for_terminal(tasks: list[TaskRecord], cancel_event: threading.Event) -> None:
    """Block until every task is terminal, or the parent run is cancelled."""
    if all(task.done.is_set() for task in tasks):
        return
    wake = threading.Event()

    def watch(source: threading.Event) -> None:
        source.wait()
        wake.set()

    watchers = [threading.Thread(target=watch, args=(task.done,), daemon=True) for task in tasks]
    watchers.append(threading.Thread(target=watch, args=(cancel_event,), daemon=True))
    for watcher in watchers:
        watcher.start()
    while True:
        if all(task.done.is_set() for task in tasks):
            return
        if cancel_event.is_set():
            raise ToolException("Parent run cancelled")
        wake.wait()
        wake.clear()


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


class WaitTaskSchema(BaseModel):
    run_ids: list[str] = Field(min_length=1, description="Background run ids belonging to this agent.")

    @model_validator(mode="before")
    @classmethod
    def _unknown(cls, data: object) -> object:
        return _reject_unknown(cls, data, hint="only run_ids is accepted.")


class CancelTaskSchema(BaseModel):
    run_id: str = Field(description="Background run id belonging to this agent.")

    @model_validator(mode="before")
    @classmethod
    def _unknown(cls, data: object) -> object:
        return _reject_unknown(cls, data, hint="only run_id is accepted.")


class StartTaskSchema(BaseModel):
    description: str = Field(
        description=(
            "The complete task for the subagent. Include the objective, context, "
            "constraints, and expected deliverable. The subagent sees only this text."
        ),
    )
    run_in_background: bool = Field(
        default=False,
        description=(
            "When false, wait and return the result. When true, return the run_id "
            "immediately and deliver the result later."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _unknown(cls, data: object) -> object:
        return _reject_unknown(cls, data, hint="put all instructions for the subagent in description.")


class StartTaskSyncSchema(BaseModel):
    description: str = Field(
        description=(
            "The complete task for the subagent. Include the objective, context, "
            "constraints, and expected deliverable. The subagent sees only this text."
        ),
    )

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
    if "wait_task" in names:
        tools.append(_wait_tool(manager))
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
                runs = store.list_runs(parent)
                page = runs[offset:offset + limit]
                tasks = []
                for child_id in page:
                    task = manager.task(child_id, parent) if manager is not None else None
                    if task is not None:
                        tasks.append(_task_fields(task, with_result=False))
                        continue
                    meta = store.task_metadata(parent, child_id)
                    if meta is not None:
                        tasks.append(_log_fields(meta, with_result=False))
                return finish({"tasks": tasks, "next_offset": offset + len(page)})
            task = manager.task(run_id, parent) if manager is not None else None
            if task is None:
                meta = store.task_metadata(parent, run_id)
                if meta is None:
                    raise ToolException("Subagent log not found in this parent session")
                item = _log_fields(meta, with_result=True)
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


def _wait_tool(manager: SubAgentManager | None) -> StructuredTool:
    def wait_task(run_ids: list[str], runtime: ToolRuntime) -> tuple[str, dict[str, Any]]:
        parent = _parent_id(runtime)
        if not parent:
            raise ToolException("Parent session ID unavailable")
        if manager is None:
            raise ToolException("Background tasks not found for this agent")
        ordered: list[str] = []
        for run_id in run_ids:
            if run_id not in ordered:
                ordered.append(run_id)
        found: list[TaskRecord] = []
        missing: list[str] = []
        for run_id in ordered:
            task = manager.task(run_id, parent)
            if task is None or task.mode != "background":
                missing.append(run_id)
            else:
                found.append(task)
        if missing:
            raise ToolException("Background tasks not found for this agent: " + ", ".join(missing))
        _wait_for_terminal(found, manager.parent.current_cancel_event())
        manager.consume(found)
        payload = {"tasks": [_task_fields(task, with_result=True) for task in found]}
        return _pack(payload)

    return StructuredTool.from_function(
        wait_task,
        name="wait_task",
        description=WAIT_TASK_DESCRIPTION,
        args_schema=WaitTaskSchema,
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
