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
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.runnables import Runnable, RunnableConfig

from agent.runtime.auto_approval import delegating_user_request
from agent.runtime.contracts import observation_scope
from agent.runtime.cancellation import get_cancel_context, set_output_emitter
from agent.runtime.metrics import GenerationTiming
from agent.models.stream_lifecycle import set_stream_owner
from agent.models.messages import _chunk_message, _split_think, reasoning_text, visible_text
from agent.sessions.transcript import classify_tool_result
from agent.subagents.log_store import SubAgentLogStore
from agent.subagents.manager import BackgroundTask, SubAgentManager
from agent.subagents.notifications import START_TOOL


SUMMARY_LIMIT = 1000
COMMAND_LIMIT = 300
ERROR_LIMIT = 240
TITLE_LIMIT = 120
REASON_LIMIT = 160
AINVOKE_CANCEL_WAIT = 5.0
_DELEGATION_TOOLS = frozenset({"start_task", START_TOOL})


def _brief(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _error_reason(error: BaseException, redact: Callable[[str], str] | None = None) -> str:
    text = str(error) or type(error).__name__
    return _brief(redact(text) if redact is not None else text, ERROR_LIMIT)


def _active_task_call_id() -> str:
    """The start_task call that is running on this thread, if middleware set one."""
    context = get_cancel_context()
    if context is None or context.tool_name not in _DELEGATION_TOOLS:
        return ""
    return context.tool_call_id or ""


def _usage_pair(usage: Any) -> tuple[int, int]:
    if not isinstance(usage, dict):
        return 0, 0
    def count(key: str) -> int:
        value = usage.get(key)
        return value if type(value) is int and value >= 0 else 0
    return count("input_tokens"), count("output_tokens")


class TaskMetrics(BaseCallbackHandler):
    """Token and throughput totals for one subagent. Never forwards model text."""

    def __init__(self, on_update: Callable[[], None] | None = None) -> None:
        super().__init__()
        self.input_tokens = 0
        self.output_tokens = 0
        self._rates: list[float] = []
        self._timing: GenerationTiming | None = None
        self._summary_runs: set[Any] = set()
        self._message: AIMessageChunk | None = None
        self._raw_text = ""
        self._visible = ""
        self._reasoning = ""
        self.saw_llm = False
        self._on_update = on_update

    def on_chat_model_start(self, serialized: dict, messages: list, **kwargs: Any) -> None:
        self.on_llm_start(serialized, [], **kwargs)

    def on_llm_start(self, serialized: dict, prompts: list | None = None, **kwargs: Any) -> None:
        invocation = kwargs.get("invocation_params") or {}
        metadata = kwargs.get("metadata") or {}
        if (metadata or {}).get("lc_source") == "summarization":
            self._summary_runs.add(kwargs.get("run_id"))
            return
        self._reset_stream()
        self._timing = GenerationTiming(
            monotonic(),
            str(invocation.get("model") or metadata.get("ls_model_name") or "unknown"),
            str(invocation.get("reasoning_effort") or (invocation.get("reasoning") or {}).get("effort") or "default"),
        )

    def on_llm_new_token(self, token: str, *, chunk: Any = None, **kwargs: Any) -> None:
        if kwargs.get("run_id") in self._summary_runs or self._timing is None:
            return
        message = _chunk_message(chunk)
        if isinstance(message, AIMessageChunk):
            self._message = message if self._message is None else self._message + message
            visible = visible_text(self._message)
            reasoning = reasoning_text(self._message)
        else:
            self._raw_text += token or ""
            visible, reasoning = _split_think(self._raw_text)
        chunks = getattr(message, "tool_call_chunks", None) or []
        tool_output = isinstance(message, AIMessageChunk) and any(
            isinstance(item, dict) and (item.get("args") or item.get("name")) for item in chunks
        )
        if (
            reasoning
            and reasoning != self._reasoning
            and isinstance(message, AIMessageChunk)
            and self._message is not None
        ):
            self._note_summarized_reasoning(message)
        # Empty usage tails and other chunks with no new text are not answer output.
        self._timing.mark(
            monotonic(),
            reasoning=bool(reasoning) and reasoning != self._reasoning,
            answer=(bool(visible) and visible != self._visible) or tool_output,
        )
        self._reasoning = reasoning
        self._visible = visible

    def _note_summarized_reasoning(self, message: AIMessageChunk) -> None:
        """Match StreamDeltaCallback: a summary is not the reasoning generation span."""
        assert self._message is not None and self._timing is not None
        kind = message.response_metadata.get("deep_agent_reasoning_kind") or {}
        if not kind:
            from agent.models.client import _STREAM_STATUS
            status = _STREAM_STATUS.get()
            if status is not None and status.reasoning_kind is not None:
                kind = {status.reasoning_kind: True}
        summary = any(isinstance(block, dict) and block.get("summary") for block in (
            message.content if isinstance(message.content, list) else []
        ))
        extra = self._message.additional_kwargs
        uses_summary = (
            isinstance(extra.get("reasoning_summary"), str)
            and bool(extra["reasoning_summary"])
            and not any(
                isinstance(extra.get(key), str) and extra[key]
                for key in ("reasoning_content", "reasoning", "thinking")
            )
        )
        if uses_summary or kind.get("summary") or (summary and not kind.get("full")):
            self._timing.summarized_reasoning = True

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        if self._finish_summary(kwargs.get("run_id")):
            return
        timing = self._timing
        usage = self._message.usage_metadata if self._message is not None else None
        self._timing = None
        self._reset_stream()
        if timing is None:
            return
        generations = getattr(response, "generations", None) or []
        if not isinstance(usage, dict) and generations and generations[0]:
            message = getattr(generations[0][0], "message", None)
            usage = getattr(message, "usage_metadata", None)
        snapshot = timing.snapshot(usage if isinstance(usage, dict) else None)
        self._add(snapshot.get("input_tokens"), snapshot.get("output_tokens"), snapshot.get("tps"))

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        if self._finish_summary(kwargs.get("run_id")):
            return
        self._timing = None
        self._reset_stream()

    def _finish_summary(self, run_id: Any) -> bool:
        if run_id not in self._summary_runs:
            return False
        self._summary_runs.remove(run_id)
        return True

    def _reset_stream(self) -> None:
        self._message = None
        self._raw_text = ""
        self._visible = ""
        self._reasoning = ""

    def add_messages(self, messages: list) -> None:
        """Fallback when the graph never invoked the model callback."""
        for message in messages:
            incoming, outgoing = _usage_pair(getattr(message, "usage_metadata", None))
            self.input_tokens += incoming
            self.output_tokens += outgoing

    def _add(self, incoming: Any, outgoing: Any, rate: Any) -> None:
        self.saw_llm = True
        if type(incoming) is int and incoming >= 0:
            self.input_tokens += incoming
        if type(outgoing) is int and outgoing >= 0:
            self.output_tokens += outgoing
        if type(rate) is float and rate >= 0:
            self._rates.append(rate)
        if self._on_update is not None:
            self._on_update()

    @property
    def tps(self) -> float | None:
        if not self._rates:
            return None
        return sum(self._rates) / len(self._rates)


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
        owner_controller: Any | None = None,
    ) -> None:
        self.manager, self.store, self.prepare = manager, store, prepare
        self.model_name = model_name
        self.effort = effort
        self.child_depth = child_depth
        # Agent that owns this runtime; children register here for cancel propagation.
        self.owner_controller = owner_controller if owner_controller is not None else manager.parent

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
            return {"messages": [AIMessage(content="Subagent failed: parent session ID unavailable")]}
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
            return {"messages": [AIMessage(content=f"Subagent failed: {reason}")]}

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

    def start_background(self, description: str, parent: str, tool_call_id: str) -> BackgroundTask:
        """Run one delegation on its own thread and report the outcome to the parent.

        Raises when no permit is available; nothing is started in that case.
        """
        run_id, controller = self.manager.admit(parent, self.owner_controller)
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
                    tool_call_id,
                )
                content = str(output["messages"][-1].content)
            except BaseException as exc:  # noqa: BLE001
                state = "cancelled" if controller.cancel_requested else "failed"
                content = f"Subagent {state} (run_id={run_id}): {_error_reason(exc, self.store._redact)}"
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
        try:
            task.thread.start()
        except BaseException:
            self.manager.release(run_id)
            self.manager.finish(task, "failed", "Failed to start background thread")
            self.manager.drop_task(run_id)
            raise
        return task

    def _run(
        self, input: dict, parent: str, run_id: str, controller: Any, scope=None, tool_call_id: str = "",
    ) -> dict:
        metrics = TaskMetrics()
        started = monotonic()
        description = visible_text(input["messages"][-1])
        title = " ".join(self.store._redact(description).split())
        if len(title) > TITLE_LIMIT:
            title = title[: TITLE_LIMIT - 1] + "…"
        published = False

        def publish(status: str, *, reason: str = "") -> None:
            # Host monitor metadata only. The answer, tool logs, and model text
            # stay in the direct parent's log and are not copied onto this event.
            payload: dict[str, Any] = {
                "run_id": run_id,
                "parent_run_id": parent,
                "depth": self.child_depth,
                "description": title,
                "status": status,
                "tool_call_id": tool_call_id,
                "elapsed_seconds": round(max(0.0, monotonic() - started), 3),
                "input_tokens": metrics.input_tokens,
                "output_tokens": metrics.output_tokens,
            }
            if metrics.tps is not None:
                payload["tps"] = round(metrics.tps, 1)
            if reason and status in {"failed", "cancelled"}:
                payload["reason"] = _brief(self.store._redact(reason), REASON_LIMIT)
            self.manager.parent._emit("subagent_update", payload)

        def write(event: str, **payload: Any) -> None:
            self.store.append(parent, run_id, event, model=self.model_name, **payload)
            if scope and event in {"tool_start", "tool_finish", "tool_error"}:
                fields = {k:payload[k] for k in ("tool_name","tool_run_id","status","elapsed_seconds","exit_code")
                          if k in payload}
                self.manager.parent._emit("companion_tool", dict(scope=scope, phase=event, agent=run_id,
                                                               model=self.model_name, effort=self.effort, **fields))
            if published and event in {"tool_start", "tool_finish", "tool_error"}:
                publish("running")

        def brief(value: str, limit: int) -> str:
            # Exact known-value replacement precedes truncation so a boundary
            # never leaves a partial API key that the store cannot match.
            return _brief(self.store._redact(value), limit)

        write("start", status="running", description=brief(description, SUMMARY_LIMIT),
              description_truncated=len(" ".join(description.split())) > SUMMARY_LIMIT)
        publish("running")
        published = True

        def on_metrics() -> None:
            if published:
                publish("running")

        metrics._on_update = on_metrics
        set_stream_owner(controller)
        set_output_emitter(None)
        callback = LogCallback(write, self.store._redact)
        prepared = None
        status, summary, summary_truncated = "failed", "", False
        messages_seen: list = []
        try:
            # Bail before prepare so a cancel-before-admit does not build model clients.
            if controller.cancel_requested:
                raise RuntimeError("Cancelled by parent")
            prepared = self.prepare(controller)
            if controller.cancel_requested:
                raise RuntimeError("Cancelled by parent")
            final = {}
            for final in prepared.graph.stream(
                {"messages": input["messages"][-1:]},
                {"configurable": {"thread_id": run_id, "checkpoint_ns": ""},
                 "callbacks": [callback, metrics]},
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
                messages_seen = list(final.get("messages") or [])
            if controller.cancel_requested:
                raise RuntimeError("Cancelled by parent")
            messages = messages_seen
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
            return {"messages": [AIMessage(content=f"Subagent {status} (run_id={run_id}): {summary}")]}
        finally:
            if prepared is not None and getattr(prepared, "owns_model_clients", False):
                from agent.models.stream_lifecycle import close_model_http_clients
                try:
                    close_model_http_clients(prepared.model)
                except Exception as exc:
                    write("error", phase="cleanup", reason=_error_reason(exc, self.store._redact))
            if metrics.input_tokens == 0 and metrics.output_tokens == 0:
                metrics.add_messages(messages_seen)
            # Terminal status is published before the concurrency slot moves to
            # "closing", so a stream that is still shutting down is not shown
            # as a running task.
            publish(status, reason=summary if status in {"failed", "cancelled"} else "")
            published = False
            write("finish", status=status, summary=summary, summary_truncated=summary_truncated,
                  elapsed_seconds=round(max(0.0, monotonic() - started), 3), tool_count=callback.tool_count)
