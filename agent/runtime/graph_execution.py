"""Execute one graph segment and translate framework outcomes to run contracts."""

from __future__ import annotations

from typing import Any, Callable

from langgraph.errors import GraphDrained

from agent.runtime.contracts import InterruptKind, RunEvent, RunEventHandler, RunResult
from agent.runtime.events import (
    _emit,
    _final_output,
    classify_interrupt,
    completed_result,
    failed_result,
    interaction_result,
    interrupt_payloads,
    normalize_stream_error,
)
from agent.sessions.contracts import StopReason


class GraphExecution:
    def __init__(self, *, graph: Any, control: Any,
                 record_status: Callable[[StopReason], None]) -> None:
        self.graph = graph
        self.control = control
        self.record_status = record_status

    def run(self, graph_input: Any, config: dict[str, Any], *,
            thread_config: dict[str, Any], run_control: Any,
            on_update: Callable[[Any], None], handler: RunEventHandler | None,
            on_cancelled: Callable[[], RunResult],
            answer_committed: Callable[[], bool]) -> RunResult:
        interrupted = ""
        pending: list[dict[str, Any]] = []
        human_input: dict[str, Any] = {}
        try:
            for mode, payload in self.graph.stream(
                graph_input, config, stream_mode=["updates", "values"],
                control=run_control, durability="sync",
            ):
                if mode == "values":
                    continue
                on_update(payload)
                if not isinstance(payload, dict):
                    continue
                for node, update in payload.items():
                    if node != "__interrupt__":
                        continue
                    if any(item.get("type") == "runtime_config_boundary" for item in interrupt_payloads(update)):
                        return RunResult(status="runtime_config_boundary")
                    interrupt = classify_interrupt(update, self.graph, thread_config)
                    interrupted = interrupt.kind.value
                    pending = list(interrupt.pending_tools)
                    if interrupt.kind is InterruptKind.WAITING_HUMAN:
                        human_input = interrupt.payload
        except GraphDrained:
            if answer_committed():
                return completed_result(_final_output(self.graph, thread_config))
            return on_cancelled()
        except Exception as exc:
            failure = normalize_stream_error(exc, cancel_requested=self.control.cancel_requested)
            if failure is None:
                return on_cancelled()
            _emit(handler, RunEvent(type="run_failed", content=str(failure), is_error=True))
            self.record_status(StopReason.ERROR)
            return failed_result(str(failure))
        if interrupted:
            self.control.set_defer_steering(True)
            self.record_status(StopReason.DEFERRED)
            result = interaction_result(interrupted, pending, human_input)
            _emit(handler, RunEvent(type="interaction_requested", result=result))
            return result
        return completed_result(_final_output(self.graph, thread_config))
