"""A CompiledSubAgent runnable around the existing graph factory.

A fresh Context is deliberate: LangGraph otherwise inherits the parent's
callbacks, checkpoint namespace, stream writer and configuration implicitly.
"""
from __future__ import annotations

import asyncio
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


class LogCallback(BaseCallbackHandler):
    def __init__(self, write: Callable[..., None]) -> None:
        self.write = write

    def on_chat_model_start(self, serialized: dict, messages: list, **kwargs: Any) -> None:
        self.write("model_start", input=[[message.model_dump() for message in batch] for batch in messages])

    def on_llm_new_token(self, token: str, *, chunk: Any = None, **kwargs: Any) -> None:
        self.write("model_delta", output=chunk.model_dump() if hasattr(chunk, "model_dump") else token)

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        self.write("model", output=response.model_dump())

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        self.write("model_error", error=str(error))

    def on_tool_start(self, serialized: dict, input_str: str, **kwargs: Any) -> None:
        self.write("tool_start", tool_name=serialized.get("name"), input=input_str)

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        self.write("tool_finish", output=output.model_dump() if hasattr(output, "model_dump") else output)

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        self.write("tool_error", error=str(error))


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

        write("start", status="running", input=input.get("messages", [])[-1].content)
        set_stream_owner(controller)
        set_output_emitter(lambda call_id, content, stream: write(
            "tool_output", tool_call_id=call_id, output=content, stream=stream,
        ))
        prepared = None
        try:
            prepared = self.prepare(controller)
            if controller.cancel_requested:
                raise RuntimeError("Cancelled by parent")
            final = {}
            for final in prepared.graph.stream(
                {"messages": input["messages"][-1:]},
                {"configurable": {"thread_id": run_id, "checkpoint_ns": ""},
                 "callbacks": [LogCallback(write)]},
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
            write("finish", status="completed", output=answer)
            # Return only the final response: no child messages or state channels.
            return {"messages": [AIMessage(content=f"[subagent run_id={run_id}]\n{answer}")]}
        except Exception as exc:
            status = "cancelled" if controller.cancel_requested else "failed"
            write("error", status=status, error=str(exc))
            return {"messages": [AIMessage(content=f"Subagent {status} (run_id={run_id}): {str(exc)[:1000]}")]}
        finally:
            if prepared is not None and getattr(prepared, "owns_model_clients", False):
                from agent.llm_stream import close_model_http_clients
                try:
                    close_model_http_clients(prepared.model)
                except Exception as exc:
                    write("cleanup_error", error=str(exc))
