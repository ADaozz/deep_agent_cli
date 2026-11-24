"""Run one delegated task. The child sees only the description.

A fresh Context is deliberate: LangGraph otherwise inherits the parent's
callbacks, checkpoint namespace, stream writer and configuration implicitly.
"""

from __future__ import annotations

import asyncio
import json
import threading
from time import monotonic
from contextvars import Context
from typing import Any, Callable

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable, RunnableConfig

from agent.runtime.auto_approval import delegating_user_request
from agent.runtime.contracts import observation_scope
from agent.runtime.cancellation import get_cancel_context, set_output_emitter
from agent.models.stream_lifecycle import set_stream_owner
from agent.models.messages import final_answer, visible_text
from agent.runtime.graph_execution import run_segment
from agent.sessions.transcript import classify_tool_result
from agent.subagents.log_store import SubAgentLogStore
from agent.subagents.manager import SubAgentManager, TaskRecord
from agent.tools.catalog import DELEGATION_TOOLS


SUMMARY_LIMIT = 1000
COMMAND_LIMIT = 300
ERROR_LIMIT = 240
REASON_LIMIT = 160
AINVOKE_CANCEL_WAIT = 5.0


def _brief(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _error_reason(error: BaseException, redact: Callable[[str], str] | None = None) -> str:
    text = str(error) or type(error).__name__
    return _brief(redact(text) if redact is not None else text, ERROR_LIMIT)


def _active_task_call_id() -> str:
    """The start_task call that is running on this thread, if middleware set one."""
    context = get_cancel_context()
    if context is None or context.tool_name not in DELEGATION_TOOLS:
        return ""
    return context.tool_call_id or ""


def _task_message(content: str, status: str, *, run_id: str = "", result: str = "") -> AIMessage:
    return AIMessage(content=content, additional_kwargs={
        "status": status,
        "run_id": run_id,
        "result": result,
    })


def _error_category(text: str, exit_code: Any) -> str:
    from agent.companion.timeline import category

    code = exit_code if type(exit_code) is int else None
    return category(text if isinstance(text, str) else "", code)


class LogCallback(BaseCallbackHandler):
    """Record tool operations and outcomes, never model or raw output data."""

    def __init__(self, write: Callable[..., None], redact: Callable[[str], str] | None = None) -> None:
        self.write = write
        self._redact = redact or (lambda value: value)
        self._lock = threading.Lock()
        self._tools: dict[Any, tuple[str, float]] = {}
        self.tool_count = 0

    def _brief(self, value: str, limit: int) -> str:
        return _brief(self._redact(value), limit)

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        self.write("model_error", reason=_error_reason(error, self._redact))

    def on_tool_start(self, serialized: dict, input_str: str, **kwargs: Any) -> None:
        name = (serialized or {}).get("name") or "tool"
        arguments = kwargs.get("inputs")
        if not isinstance(arguments, dict):
            try:
                arguments = json.loads(input_str)
            except (ValueError, TypeError):
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        operation = {}
        if name == "execute":
            command = arguments.get("command")
            if isinstance(command, str):
                operation["command"] = self._brief(command, COMMAND_LIMIT)
        else:
            for key in ("file_path", "path", "directory", "query"):
                target = arguments.get(key)
                if isinstance(target, str):
                    operation["query" if key == "query" else "target"] = self._brief(target, COMMAND_LIMIT)
                    break
        with self._lock:
            self._tools[kwargs.get("run_id")] = (name, monotonic())
            self.tool_count += 1
        self.write("tool_start", tool_name=name, tool_run_id=str(kwargs.get("run_id") or ""), **operation)

    def _finish_tool(self, kwargs: dict) -> dict:
        with self._lock:
            name, started = self._tools.pop(kwargs.get("run_id"), ("tool", monotonic()))
        return dict(tool_name=name, tool_run_id=str(kwargs.get("run_id") or ""),
                    elapsed_seconds=round(max(0.0, monotonic() - started), 3))

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        result = self._finish_tool(kwargs)
        artifact = getattr(output, "artifact", None)
        artifact = artifact if isinstance(artifact, dict) else {}
        exit_code = artifact.get("exit_code")
        termination = artifact.get("termination_reason")
        content = getattr(output, "content", output if isinstance(output, str) else "")
        content = content if isinstance(content, str) else ""
        outcome = classify_tool_result(
            status=str(getattr(output, "status", "") or ""),
            content=content,
            name=str(result["tool_name"] or ""),
            artifact=artifact,
        )
        failed = outcome != "success"
        result["status"] = {"cancelled": "cancelled", "failed": "failed"}.get(outcome, "completed")
        if type(exit_code) is int:
            result["exit_code"] = exit_code
        if result["tool_name"] == "execute":
            log_path = artifact.get("agent_log_path") or artifact.get("host_log_path")
            if isinstance(log_path, str):
                result["log_path"] = self._brief(log_path, COMMAND_LIMIT)
        # Successful file reads/edits and commands never copy their output.
        result["summary"] = "Completed" if not failed else "Tool failed"
        if failed:
            if termination:
                reason = str(termination)
            elif type(exit_code) is int:
                reason = f"Command exited with code {exit_code}"
            else:
                reason = content.splitlines()[0] if content else "Tool returned an error"
            result["reason"] = self._brief(reason, ERROR_LIMIT)
            result["error_category"] = _error_category(content, exit_code)
        self.write("tool_finish", **result)

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        reason = _error_reason(error, self._redact)
        self.write(
            "tool_error", **self._finish_tool(kwargs), status="failed", reason=reason,
            error_category=_error_category(reason, None),
        )


class SubAgentRuntime(Runnable):
    def __init__(
        self,
        manager: SubAgentManager,
        store: SubAgentLogStore,
        prepare: Callable,
        model_name: str,
        effort: str = "default",
        *,
        child_depth: int = 1,
        owner_controller: Any | None = None,
    ) -> None:
        self.manager, self.store, self.prepare = manager, store, prepare
        self.model_name = model_name
        self.effort = effort
        self.child_depth = child_depth
        # Agent that owns this runtime; children register here for cancel propagation.
        self.owner_controller = owner_controller if owner_controller is not None else manager.parent

    def _borrow_environment(self) -> Any:
        """Install the root's already-probed snapshot for this child context only."""
        cache = getattr(self.manager.parent, "environment_cache", None)
        snapshot = getattr(cache, "snapshot", None)
        if snapshot is None:
            return None
        from agent.execution.environment import EnvironmentCache, set_environment_cache
        return set_environment_cache(EnvironmentCache(snapshot=snapshot))

    def invoke(self, input: dict, config: RunnableConfig | None = None, **kwargs: Any) -> dict:
        return self._invoke_tracked({"cancel": threading.Event()}, input, config, **kwargs)

    def _invoke_tracked(
        self,
        call: dict[str, Any],
        input: dict,
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> dict:
        parent = str((config or {}).get("configurable", {}).get("thread_id", ""))
        # Native task supplies only its tag explicitly. Recover the parent ID
        # here, before entering the clean execution context.
        if not parent:
            from langgraph.config import get_config
            parent = str(get_config().get("configurable", {}).get("thread_id", ""))
        if not parent:
            return {"messages": [_task_message("Subagent failed: parent session ID unavailable", "failed")]}
        cancel = call.get("cancel")
        # Context().run below drops context vars. Capture the launching call first
        # so the root card binds by tool_call_id, not by start order.
        tool_call_id = _active_task_call_id()
        try:
            with self.manager.running(parent, self.owner_controller) as (run_id, controller):
                # Publish immediately so ainvoke CancelledError can target this child only.
                call["controller"] = controller
                # Persist cancel-before-admit: flag may already be set while we were blocked.
                if isinstance(cancel, threading.Event) and cancel.is_set():
                    controller.cancel()
                controller.approval_user_request = delegating_user_request.get()
                return Context().run(
                    self._run, input, parent, run_id, controller, observation_scope.get(), tool_call_id,
                )
        except Exception as exc:
            reason = _error_reason(exc, self.store._redact)
            return {"messages": [_task_message(f"Subagent failed: {reason}", "failed")]}

    async def ainvoke(self, input: dict, config: RunnableConfig | None = None, **kwargs: Any) -> dict:
        # Keep synchronous task completion semantics even when the caller is async.
        call: dict[str, Any] = {"controller": None, "cancel": threading.Event()}
        operation = asyncio.create_task(
            asyncio.to_thread(self._invoke_tracked, call, input, config, **kwargs),
        )
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Record cancel immediately so a late admit still sees it.
            cancel = call["cancel"]
            if isinstance(cancel, threading.Event):
                cancel.set()
            # Cancel only this invocation's admitted child — never owner / Root / siblings.
            deadline = asyncio.get_running_loop().time() + AINVOKE_CANCEL_WAIT
            while call["controller"] is None and not operation.done():
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.01)
            controller = call["controller"]
            if controller is not None:
                controller.cancel()
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            try:
                await asyncio.wait_for(asyncio.shield(operation), timeout=remaining)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            raise

    def start_background(self, description: str, parent: str, tool_call_id: str) -> TaskRecord:
        """Run one delegation on its own thread and report the outcome to the parent.

        Raises when no permit is available; nothing is started in that case.
        """
        run_id, controller = self.manager.admit(parent, self.owner_controller)
        # The child is a leaf: it gets no delegation tools and no delegation prompt.
        controller.background_leaf = True
        controller.approval_user_request = delegating_user_request.get()
        task = TaskRecord(
            run_id=run_id, parent_run_id=parent, tool_call_id=tool_call_id,
            description=description, depth=self.child_depth, mode="background",
            controller=controller,
        )

        scope = observation_scope.get()
        def work() -> None:
            try:
                # Same clean context as the synchronous path: no parent
                # callbacks, stream writer or checkpoint namespace.
                Context().run(
                    self._run, {"messages": [HumanMessage(content=description)]}, parent, run_id, controller, scope,
                    tool_call_id,
                )
            except BaseException as exc:  # noqa: BLE001
                state = "cancelled" if controller.cancel_requested else "failed"
                content = _error_reason(exc, self.store._redact)
                if not task.done.is_set():
                    self.manager.record_terminal(run_id, state, content, None)
            status = task.status
            content = task.result
            try:
                self.manager.release(run_id)
            finally:
                if not task.discarded:
                    # The host notification, when one is due, was sent with the
                    # terminal record. This event only updates the card.
                    self.manager.parent._emit("subagent_finished", {
                        "run_id": run_id, "tool_call_id": tool_call_id, "parent_run_id": parent,
                        "status": status, "content": content,
                    })

        task.thread = threading.Thread(target=work, name=f"subagent-{run_id[:8]}", daemon=True)
        self.manager.track(task)
        try:
            task.thread.start()
        except BaseException:
            task.discarded = True
            self.manager.release(run_id)
            self.manager.record_terminal(run_id, "failed", "Failed to start background thread", None)
            self.manager.drop_task(run_id)
            raise
        return task

    def _run(
        self, input: dict, parent: str, run_id: str, controller: Any, scope=None, tool_call_id: str = "",
    ) -> dict:
        started = monotonic()
        description = visible_text(input["messages"][-1])
        redacted = self.store._redact(description)
        published = False

        def publish(status: str, *, reason: str = "", result: str = "") -> None:
            # Progress stays metadata. The final answer is attached once, so the
            # owning card can show it under this task. Tool logs stay in the log.
            payload: dict[str, Any] = {
                "run_id": run_id,
                "parent_run_id": parent,
                "depth": self.child_depth,
                "description": redacted,
                "status": status,
                "tool_call_id": tool_call_id,
                "elapsed_seconds": round(max(0.0, monotonic() - started), 3),
            }
            if reason and status in {"failed", "cancelled"}:
                payload["reason"] = _brief(self.store._redact(reason), REASON_LIMIT)
            if result and status in {"completed", "failed", "cancelled"}:
                payload["result"] = self.store._redact(result)
            self.manager.parent._emit("subagent_update", payload)

        def write(event: str, **payload: Any) -> None:
            self.store.append(parent, run_id, event, model=self.model_name, **payload)
            if scope and event in {"tool_start", "tool_finish", "tool_error"}:
                fields = {k:payload[k] for k in (
                    "tool_name", "tool_run_id", "status", "elapsed_seconds", "exit_code", "error_category",
                ) if k in payload}
                self.manager.parent._emit("companion_tool", dict(scope=scope, phase=event, agent=run_id,
                                                               model=self.model_name, effort=self.effort, **fields))
            if published and event in {"tool_start", "tool_finish", "tool_error"}:
                publish("running")

        def brief(value: str, limit: int) -> str:
            # Exact known-value replacement precedes truncation so a boundary
            # never leaves a partial API key that the store cannot match.
            return _brief(self.store._redact(value), limit)

        record = self.manager.open_record(
            run_id=run_id, parent_run_id=parent, description=description, tool_call_id=tool_call_id,
            controller=controller, mode="sync", depth=self.child_depth,
        )
        write(
            "start", status="running", description=brief(description, SUMMARY_LIMIT),
            description_truncated=len(" ".join(description.split())) > SUMMARY_LIMIT,
            mode=record.mode, parent_run_id=parent, tool_call_id=tool_call_id, depth=record.depth,
        )
        publish("running")
        published = True
        set_stream_owner(controller)
        set_output_emitter(None)
        callback = LogCallback(write, self.store._redact)
        prepared = None
        status, summary, summary_truncated = "failed", "", False
        messages_seen: list = []
        answer_text = ""
        outcome = {"content": ""}
        env_token = None
        try:
            # Bail before prepare so a cancel-before-admit does not build model clients.
            if controller.cancel_requested:
                raise RuntimeError("Cancelled by parent")
            prepared = self.prepare(controller)
            if controller.cancel_requested:
                raise RuntimeError("Cancelled by parent")
            def on_chunk(final: Any) -> None:
                if not isinstance(final, dict):
                    return None
                # Deliver a host decision to the parent even if the child
                # model would omit the refusal from its final answer.
                for message in reversed(final.get("messages", [])):
                    if not isinstance(message, ToolMessage):
                        break
                    if message.artifact == {"auto_approval": "requires_parent_approval"}:
                        raise RuntimeError(f"{message.name} ({message.tool_call_id}): {message.content}")
                if final.get("__interrupt__"):
                    raise RuntimeError("Subagents cannot request user interaction")
                messages_seen[:] = list(final.get("messages") or [])
                return None

            env_token = self._borrow_environment()
            stop = run_segment(
                prepared.graph,
                {"messages": input["messages"][-1:]},
                {"configurable": {"thread_id": run_id, "checkpoint_ns": ""}, "callbacks": [callback]},
                run_control=controller.run_control,
                stream_mode="values",
                durability=None,
                on_chunk=on_chunk,
                cancel_requested=lambda: controller.cancel_requested,
            )
            if stop.kind == "raised":
                if stop.error is None or controller.cancel_requested:
                    raise RuntimeError("Cancelled by parent")
                raise stop.error
            # A parent cancel wins over a drained or finished answer.
            if controller.cancel_requested:
                raise RuntimeError("Cancelled by parent")
            answer = final_answer(messages_seen)
            if not answer:
                raise RuntimeError("Subagent did not produce a final answer")
            answer_text = answer
            status = "completed"
            summary = brief(answer, SUMMARY_LIMIT)
            summary_truncated = len(" ".join(answer.split())) > SUMMARY_LIMIT
            # Return only the final response: no child messages or state channels.
            outcome["content"] = answer
            return {"messages": [_task_message(answer, status, run_id=run_id, result=answer)]}
        except Exception as exc:
            status = "cancelled" if controller.cancel_requested else "failed"
            summary = "Cancelled by parent" if controller.cancel_requested else _error_reason(exc, self.store._redact)
            answer_text = summary
            write("error", status=status, reason=summary)
            outcome["content"] = summary
            return {"messages": [_task_message(summary, status, run_id=run_id, result=summary)]}
        finally:
            if env_token is not None:
                from agent.execution.environment import reset_environment_cache
                reset_environment_cache(env_token)
            if prepared is not None and getattr(prepared, "owns_model_clients", False):
                from agent.models.stream_lifecycle import close_model_http_clients
                try:
                    close_model_http_clients(prepared.model)
                except Exception as exc:
                    write("error", phase="cleanup", reason=_error_reason(exc, self.store._redact))
            # Terminal status is published before the concurrency slot moves to
            # "closing", so a stream that is still shutting down is not shown
            # as a running task.
            if status != "completed" and controller.cancel_requested:
                status = "cancelled"
                if not outcome["content"]:
                    outcome["content"] = "Cancelled by parent"
            elapsed = round(max(0.0, monotonic() - started), 3)
            publish(
                status,
                reason=summary if status in {"failed", "cancelled"} else "",
                result=answer_text,
            )
            published = False
            finish_fields: dict[str, Any] = {
                "status": status,
                "elapsed_seconds": elapsed,
                "tool_count": callback.tool_count,
                "mode": record.mode,
                "parent_run_id": parent,
                "tool_call_id": tool_call_id,
                "depth": record.depth,
                "description": brief(description, SUMMARY_LIMIT),
            }
            if summary_truncated:
                finish_fields["result_summary"] = summary
                finish_fields["summary_truncated"] = True
            else:
                finish_fields["result"] = outcome["content"]
            write("finish", **finish_fields)
            self.manager.record_terminal(run_id, status, outcome["content"], elapsed)
