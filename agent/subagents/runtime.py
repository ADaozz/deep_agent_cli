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
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable, RunnableConfig

from agent.cancel import set_output_emitter
from agent.llm_stream import set_stream_owner
from agent.stream import visible_text
from agent.subagents.log_store import SubAgentLogStore
from agent.subagents.manager import SubAgentManager


SUMMARY_LIMIT = 1000
COMMAND_LIMIT = 300
ERROR_LIMIT = 240


def _brief(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _error_reason(error: BaseException) -> str:
    return _brief(str(error) or type(error).__name__, ERROR_LIMIT)


class LogCallback(BaseCallbackHandler):
    """Record tool operations and outcomes, never model or raw output data."""

    def __init__(self, write: Callable[..., None]) -> None:
        self.write = write
        self._lock = threading.Lock()
        self._tools: dict[Any, tuple[str, float]] = {}
        self.tool_count = 0

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        self.write("model_error", reason=_error_reason(error))

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
                operation["command"] = _brief(command, COMMAND_LIMIT)
        else:
            for key in ("file_path", "path", "directory", "query"):
                target = arguments.get(key)
                if isinstance(target, str):
                    operation["query" if key == "query" else "target"] = _brief(target, COMMAND_LIMIT)
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
        failed = (getattr(output, "status", None) == "error" or bool(termination)
                  or (type(exit_code) is int and exit_code != 0)
                  or content.lower().startswith("error") or "cancelled by user" in content.lower())
        result["status"] = "cancelled" if termination == "cancelled" or "cancelled by user" in content.lower() else (
            "failed" if failed else "completed"
        )
        if type(exit_code) is int:
            result["exit_code"] = exit_code
        if result["tool_name"] == "execute":
            log_path = artifact.get("agent_log_path") or artifact.get("host_log_path")
            if isinstance(log_path, str):
                result["log_path"] = _brief(log_path, COMMAND_LIMIT)
        # Successful file reads/edits and commands never copy their output.
        result["summary"] = "Completed" if not failed else "Tool failed"
        if failed:
            if termination:
                reason = str(termination)
            elif type(exit_code) is int:
                reason = f"Command exited with code {exit_code}"
            else:
                reason = content.splitlines()[0] if content else "Tool returned an error"
            result["reason"] = _brief(reason, ERROR_LIMIT)
        self.write("tool_finish", **result)

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        self.write("tool_error", **self._finish_tool(kwargs), status="failed", reason=_error_reason(error))


class SubAgentRuntime(Runnable):
    def __init__(self, manager: SubAgentManager, store: SubAgentLogStore,
                 prepare: Callable, model_name: str) -> None:
        self.manager, self.store, self.prepare = manager, store, prepare
        self.model_name = model_name

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
            with self.manager.running() as (run_id, controller):
                return Context().run(self._run, input, parent, run_id, controller)
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

    def _run(self, input: dict, parent: str, run_id: str, controller: Any) -> dict:
        def write(event: str, **payload: Any) -> None:
            self.store.append(parent, run_id, event, model=self.model_name, **payload)

        started = monotonic()
        description = visible_text(input["messages"][-1])
        write("start", status="running", description=_brief(description, SUMMARY_LIMIT),
              description_truncated=len(" ".join(description.split())) > SUMMARY_LIMIT)
        set_stream_owner(controller)
        set_output_emitter(None)
        callback = LogCallback(write)
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
            summary = _brief(answer, SUMMARY_LIMIT)
            summary_truncated = len(" ".join(answer.split())) > SUMMARY_LIMIT
            # Return only the final response: no child messages or state channels.
            return {"messages": [AIMessage(content=f"[subagent run_id={run_id}]\n{answer}")]}
        except Exception as exc:
            status = "cancelled" if controller.cancel_requested else "failed"
            summary = "Cancelled by parent" if controller.cancel_requested else _error_reason(exc)
            write("error", status=status, reason=summary)
            return {"messages": [AIMessage(content=f"Subagent {status} (run_id={run_id}): {str(exc)[:1000]}")]}
        finally:
            if prepared is not None and getattr(prepared, "owns_model_clients", False):
                from agent.llm_stream import close_model_http_clients
                try:
                    close_model_http_clients(prepared.model)
                except Exception as exc:
                    write("error", phase="cleanup", reason=_error_reason(exc))
            write("finish", status=status, summary=summary, summary_truncated=summary_truncated,
                  elapsed_seconds=round(max(0.0, monotonic() - started), 3), tool_count=callback.tool_count)
