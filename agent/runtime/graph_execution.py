"""Execute one graph segment and translate framework outcomes to run contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

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


SegmentKind = Literal["finished", "drained", "raised", "stopped"]


@dataclass
class SegmentStop:
    """How one graph.stream loop ended. Callers apply their own policy."""

    kind: SegmentKind
    error: BaseException | None = None
    value: Any = None


def run_segment(
    graph: Any,
    graph_input: Any,
    config: dict[str, Any],
    *,
    run_control: Any,
    stream_mode: Any,
    durability: str | None,
    on_chunk: Callable[[Any], Any] | None,
    cancel_requested: Callable[[], bool],
) -> SegmentStop:
    """Drive one graph.stream loop without sessions, approvals, or the TUI.

    ``on_chunk`` sees each stream item. A non-None return stops the loop.
    Exceptions raised by ``on_chunk`` propagate. ``GraphDrained`` and other
    stream errors become a ``SegmentStop``.
    """
    try:
        kwargs: dict[str, Any] = {"stream_mode": stream_mode, "control": run_control}
        if durability is not None:
            kwargs["durability"] = durability
        for chunk in graph.stream(graph_input, config, **kwargs):
            if on_chunk is None:
                continue
            stop = on_chunk(chunk)
            if stop is not None:
                return SegmentStop("stopped", value=stop)
    except GraphDrained:
        return SegmentStop("drained")
    except Exception as exc:
        return SegmentStop("raised", error=normalize_stream_error(exc, cancel_requested=cancel_requested()))
    return SegmentStop("finished")


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

        def on_chunk(chunk: Any) -> RunResult | None:
            nonlocal interrupted, pending, human_input
            mode, payload = chunk
            if mode == "values":
                return None
            on_update(payload)
            if not isinstance(payload, dict):
                return None
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
            return None

        stop = run_segment(
            self.graph, graph_input, config,
            run_control=run_control,
            stream_mode=["updates", "values"],
            durability="sync",
            on_chunk=on_chunk,
            cancel_requested=lambda: self.control.cancel_requested,
        )
        if stop.kind == "stopped":
            return stop.value
        if stop.kind == "drained":
            if answer_committed():
                return completed_result(_final_output(self.graph, thread_config))
            return on_cancelled()
        if stop.kind == "raised":
            if stop.error is None:
                return on_cancelled()
            _emit(handler, RunEvent(type="run_failed", content=str(stop.error), is_error=True))
            self.record_status(StopReason.ERROR)
            return failed_result(str(stop.error))
        if interrupted:
            self.control.set_defer_steering(True)
            self.record_status(StopReason.DEFERRED)
            result = interaction_result(interrupted, pending, human_input)
            _emit(handler, RunEvent(type="interaction_requested", result=result))
            return result
        return completed_result(_final_output(self.graph, thread_config))
