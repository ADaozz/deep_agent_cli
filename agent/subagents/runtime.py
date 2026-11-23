"""A CompiledSubAgent runnable around the existing graph factory.

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
from agent.runtime.cancellation import set_output_emitter
from agent.models.stream_lifecycle import set_stream_owner
from agent.models.messages import visible_text
from agent.sessions.transcript import classify_tool_result
from agent.subagents.log_store import SubAgentLogStore
from agent.subagents.manager import BackgroundTask, SubAgentManager


SUMMARY_LIMIT = 1000
COMMAND_LIMIT = 300
ERROR_LIMIT = 240


def _brief(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _error_reason(error: BaseException, redact: Callable[[str], str] | None = None) -> str:
    text = str(error) or type(error).__name__
    return _brief(redact(text) if redact is not None else text, ERROR_LIMIT)


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
        self.write("tool_finish", **result)

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        self.write("tool_error", **self._finish_tool(kwargs), status="failed", reason=_error_reason(error, self._redact))


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
    ) -> None:
        self.manager, self.store, self.prepare = manager, store, prepare
        self.model_name = model_name
        self.effort = effort
        self.child_depth = child_depth

    def invoke(self, input: dict, config: RunnableConfig | None = None, **kwargs: Any) -> dict:
        parent = str((config or {}).get("configurable", {}).get("thread_id", ""))
        # Native task supplies only its tag explicitly. Recover the parent ID
        # here, before entering the clean execution context.
        if not parent:
            from langgraph.config import get_config
            parent = str(get_config().get("configurable", {}).get("thread_id", ""))
        if not parent:
            return {"messages": [AIMessage(content="Subagent failed: parent session ID unavailable")]}
        try:
            with self.manager.running(self.child_depth) as (run_id, controller):
                controller.approval_user_request = delegating_user_request.get()
                return Context().run(self._run, input, parent, run_id, controller, observation_scope.get())
        except Exception as exc:
            return {"messages": [AIMessage(content=f"Subagent failed: {str(exc)[:1000]}")]}

    async def ainvoke(self, input: dict, config: RunnableConfig | None = None, **kwargs: Any) -> dict:
        # Keep synchronous task completion semantics even when the caller is async.
        operation = asyncio.create_task(asyncio.to_thread(self.invoke, input, config, **kwargs))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            self.manager.parent.cancel()
            await operation
            raise

    def start_background(self, description: str, parent: str, tool_call_id: str) -> BackgroundTask:
        """Run one delegation on its own thread and report the outcome to the parent.

        Raises when no permit is available; nothing is started in that case.
        """
        run_id, controller = self.manager.admit(self.child_depth)
        controller.approval_user_request = delegating_user_request.get()
        task = BackgroundTask(run_id=run_id, tool_call_id=tool_call_id, parent=parent,
                              description=description, controller=controller)

        scope = observation_scope.get()
        def work() -> None:
            try:
                # Same clean context as the synchronous path: no parent
                # callbacks, stream writer or checkpoint namespace.
                output = Context().run(
                    self._run, {"messages": [HumanMessage(content=description)]}, parent, run_id, controller, scope,
                )
                content = str(output["messages"][-1].content)
            except BaseException as exc:  # noqa: BLE001
                state = "cancelled" if controller.cancel_requested else "failed"
                content = f"Subagent {state} (run_id={run_id}): {str(exc)[:1000]}"
            status = ("cancelled" if content.startswith("Subagent cancelled")
                      else "failed" if content.startswith("Subagent failed") else "completed")
            try:
                self.manager.release(run_id)
            finally:
                self.manager.finish(task, status, content)
                if not task.discarded:
                    payload = {"run_id": run_id, "tool_call_id": tool_call_id, "parent": parent,
                               "status": status, "content": content}
                    # A cancelled task was stopped on request: update its card only.
                    if status != "cancelled":
                        self.manager.parent.notify(payload)
                    self.manager.parent._emit("subagent_finished", payload)

        task.thread = threading.Thread(target=work, name=f"subagent-{run_id[:8]}", daemon=True)
        self.manager.track(task)
        task.thread.start()
        return task

    def _run(self, input: dict, parent: str, run_id: str, controller: Any, scope=None) -> dict:
        def write(event: str, **payload: Any) -> None:
            self.store.append(parent, run_id, event, model=self.model_name, **payload)
            if scope and event in {"tool_start", "tool_finish", "tool_error"}:
                fields = {k:payload[k] for k in ("tool_name","tool_run_id","status","elapsed_seconds","exit_code")
                          if k in payload}
                self.manager.parent._emit("companion_tool", dict(scope=scope, phase=event, agent=run_id,
                                                               model=self.model_name, effort=self.effort, **fields))

        def brief(value: str, limit: int) -> str:
            # Exact known-value replacement precedes truncation so a boundary
            # never leaves a partial API key that the store cannot match.
            return _brief(self.store._redact(value), limit)

        started = monotonic()
        description = visible_text(input["messages"][-1])
        write("start", status="running", description=brief(description, SUMMARY_LIMIT),
              description_truncated=len(" ".join(description.split())) > SUMMARY_LIMIT)
        set_stream_owner(controller)
        set_output_emitter(None)
        callback = LogCallback(write, self.store._redact)
        prepared = None
        status, summary, summary_truncated = "failed", "", False
        try:
            prepared = self.prepare(controller)
            if controller.cancel_requested:
                raise RuntimeError("Cancelled by parent")
            final = {}
            for final in prepared.graph.stream(
                {"messages": input["messages"][-1:]},
                {"configurable": {"thread_id": run_id, "checkpoint_ns": ""},
                 "callbacks": [callback]},
                stream_mode="values", control=controller.run_control,
            ):
                # Deliver a host decision to the parent even if the child
                # model would omit the refusal from its final answer.
                for message in reversed(final.get("messages", [])):
                    if not isinstance(message, ToolMessage):
                        break
                    if message.artifact == {"auto_approval": "requires_parent_approval"}:
                        raise RuntimeError(f"{message.name} ({message.tool_call_id}): {message.content}")
                if final.get("__interrupt__"):
                    raise RuntimeError("Subagents cannot request user interaction")
            if controller.cancel_requested:
                raise RuntimeError("Cancelled by parent")
            messages = final.get("messages", [])
            answer = next((visible_text(m) for m in reversed(messages)
                           if isinstance(m, AIMessage) and not m.tool_calls), "")
            if not answer:
                raise RuntimeError("Subagent did not produce a final answer")
            status = "completed"
            summary = brief(answer, SUMMARY_LIMIT)
            summary_truncated = len(" ".join(answer.split())) > SUMMARY_LIMIT
            # Return only the final response: no child messages or state channels.
            return {"messages": [AIMessage(content=f"[subagent run_id={run_id}]\n{answer}")]}
        except Exception as exc:
            status = "cancelled" if controller.cancel_requested else "failed"
            summary = "Cancelled by parent" if controller.cancel_requested else _error_reason(exc, self.store._redact)
            write("error", status=status, reason=summary)
            return {"messages": [AIMessage(content=f"Subagent {status} (run_id={run_id}): {str(exc)[:1000]}")]}
        finally:
            if prepared is not None and getattr(prepared, "owns_model_clients", False):
                from agent.models.stream_lifecycle import close_model_http_clients
                try:
                    close_model_http_clients(prepared.model)
                except Exception as exc:
                    write("error", phase="cleanup", reason=_error_reason(exc, self.store._redact))
            write("finish", status=status, summary=summary, summary_truncated=summary_truncated,
                  elapsed_seconds=round(max(0.0, monotonic() - started), 3), tool_count=callback.tool_count)
