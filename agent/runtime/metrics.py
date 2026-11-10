"""Provider-neutral output throughput measured at stream chunk arrival times,
and per-run time accounting derived from run events."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from time import monotonic
from typing import Any, Callable


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _span(first: float | None, last: float | None) -> float | None:
    return last - first if first is not None and last is not None and last >= first else None


def _rate(tokens: int | None, seconds: float | None) -> float | None:
    return tokens / seconds if tokens is not None and seconds is not None and seconds > 0 else None


@dataclass
class GenerationTiming:
    started: float
    model: str = "unknown"
    reasoning_effort: str = "default"
    first: float | None = None
    last: float | None = None
    reasoning_first: float | None = None
    reasoning_last: float | None = None
    answer_first: float | None = None
    answer_last: float | None = None
    summarized_reasoning: bool = False

    def mark(self, now: float, *, reasoning: bool, answer: bool) -> None:
        if not reasoning and not answer:
            return
        if self.first is None:
            self.first = now
        self.last = now
        if reasoning:
            if self.reasoning_first is None:
                self.reasoning_first = now
            self.reasoning_last = now
        if answer:
            if self.answer_first is None:
                self.answer_first = now
            self.answer_last = now

    def snapshot(self, usage: dict[str, Any] | None) -> dict[str, Any]:
        usage = usage or {}
        output = _count(usage.get("output_tokens"))
        details = usage.get("output_token_details") or {}
        if not isinstance(details, dict):
            details = {}
        reasoning = _count(details.get("reasoning"))
        # LangChain prefixes these fields for OpenAI priority/flex service tiers.
        if reasoning is None:
            for field in ("priority_reasoning", "flex_reasoning"):
                reasoning = _count(details.get(field))
                if reasoning is not None:
                    break
        if reasoning is not None and output is not None and reasoning > output:
            reasoning = None
        answer = output - reasoning if output is not None and reasoning is not None else None
        duration = _span(self.first, self.last)
        reasoning_duration = _span(self.reasoning_first, self.reasoning_last)
        answer_duration = _span(self.answer_first, self.answer_last)
        hidden_reasoning = self.summarized_reasoning or (
            reasoning is not None and reasoning > 0 and self.reasoning_first is None
        )
        if hidden_reasoning:
            # Answer/summary delivery cannot stand in for hidden reasoning generation.
            duration = reasoning_duration = None
        thinking = True if self.reasoning_first is not None or (reasoning is not None and reasoning > 0) else (
            False if reasoning == 0 else None
        )
        return {
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "timing_note": (
                "Hidden/summarized reasoning: total and reasoning generation time unavailable."
                if hidden_reasoning else "Chunk arrival timings are approximate; network buffering may affect rates."
            ),
            "thinking": thinking,
            "input_tokens": _count(usage.get("input_tokens")),
            "output_tokens": output,
            "reasoning_tokens": reasoning,
            "answer_tokens": answer,
            "ttft": self.first - self.started if self.first is not None else None,
            "output_duration": duration,
            "reasoning_duration": reasoning_duration,
            "answer_duration": answer_duration,
            "tps": _rate(output, duration),
            "reasoning_tps": _rate(reasoning, reasoning_duration),
            "answer_tps": _rate(answer, answer_duration),
        }


_SUBAGENT_TOOLS = frozenset({"task", "read_subagent_log"})


def tool_usage_by_model(history: list[dict[str, Any]]) -> tuple[list[str], list[tuple[str, dict[str, int]]]]:
    """Count requested built-in tools per model, merging reasoning efforts.

    Returns models in first-seen order and rows sorted by total count, then name.
    MCP and subagent tools are left out; skill reads are already dropped when
    recorded. Subagent model requests never reach the main metrics history.
    """
    from agent.mcp.names import is_external_mcp_tool

    models: list[str] = []
    counts: dict[str, dict[str, int]] = {}
    for metrics in history:
        names = metrics.get("tool_calls")
        if not isinstance(names, list):
            continue
        model = str(metrics.get("model") or "unknown")
        for name in names:
            if (not isinstance(name, str) or not name or is_external_mcp_tool(name)
                    or name in _SUBAGENT_TOOLS):
                continue
            if model not in models:
                models.append(model)
            row = counts.setdefault(name, {})
            row[model] = row.get(model, 0) + 1
    rows = sorted(counts.items(), key=lambda item: (-sum(item[1].values()), item[0]))
    return models, rows


def model_metric_groups(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize independently by requested model and reasoning effort."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for metrics in history:
        key = (str(metrics.get("model") or "unknown"), str(metrics.get("reasoning_effort") or "default"))
        group = groups.setdefault(key, {"model": key[0], "reasoning_effort": key[1], "samples": []})
        value = metrics.get("tps")
        if type(value) is int or type(value) is float:
            if isfinite(value) and value >= 0:
                group["samples"].append(float(value))
    for group in groups.values():
        samples = sorted(group.pop("samples"))
        count = (len(samples) + 19) // 20
        group.update(
            tps_low=sum(samples[:count]) / count if count else None,
            tps_top=sum(samples[-count:]) / count if count else None,
            tps_average=sum(samples) / len(samples) if samples else None,
            sample_count=len(samples),
        )
    return list(groups.values())


_RUN_END_EVENTS = frozenset({"run_completed", "run_failed", "run_cancelled", "interaction_requested"})


def _union_seconds(intervals: list[tuple[float, float]]) -> float:
    total = 0.0
    current_start: float | None = None
    current_end = 0.0
    for start, end in sorted(intervals):
        if current_start is None or start > current_end:
            if current_start is not None:
                total += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_start is not None:
        total += current_end - current_start
    return total


class RunMetrics:
    """Wall, model, tool and scheduling time of one run, derived from RunEvents.

    Use it as (or in front of) a Runner ``on_event`` handler; it never changes
    the events it forwards. Tool time is the union of tool intervals so
    parallel calls are not counted twice; scheduling is the rest of wall time.
    """

    def __init__(self, forward: Callable[[Any], None] | None = None, *,
                 clock: Callable[[], float] = monotonic) -> None:
        self._forward = forward
        self._clock = clock
        self._reset()

    def _reset(self) -> None:
        self._started: float | None = None
        self._finished: float | None = None
        self._model_started: float | None = None
        self._open_tools: dict[str, tuple[str, float]] = {}
        self._tool_intervals: list[tuple[float, float]] = []
        self.model_calls = 0
        self.model_seconds = 0.0
        self.ttft: list[float] = []
        self.tool_seconds: dict[str, list[float]] = {}
        self.input_tokens = 0
        self.output_tokens = 0

    def __call__(self, event: Any) -> None:
        now = self._clock()
        kind = event.type
        if kind == "run_started":
            self._reset()
            self._started = now
        elif kind == "assistant_started":
            self._model_started = now
        elif kind == "assistant_completed" and self._model_started is not None:
            self.model_calls += 1
            self.model_seconds += now - self._model_started
            self._model_started = None
        elif kind == "model_metrics" and isinstance(event.result, dict):
            ttft = event.result.get("ttft")
            if type(ttft) is float and isfinite(ttft) and ttft >= 0:
                self.ttft.append(ttft)
            self.input_tokens += _count(event.result.get("input_tokens")) or 0
            self.output_tokens += _count(event.result.get("output_tokens")) or 0
        elif kind == "tool_started":
            self._open_tools.setdefault(event.tool_call_id, (event.name or "tool", now))
        elif kind == "tool_completed":
            name, started = self._open_tools.pop(event.tool_call_id, (event.name or "tool", now))
            self._tool_intervals.append((started, now))
            self.tool_seconds.setdefault(name, []).append(now - started)
        elif kind in _RUN_END_EVENTS:
            self._finished = now
        if self._forward is not None:
            self._forward(event)

    def summary(self) -> dict[str, Any]:
        wall = _span(self._started, self._finished)
        tool_total = _union_seconds(self._tool_intervals)
        return {
            "wall_seconds": wall,
            "model_calls": self.model_calls,
            "model_seconds": self.model_seconds,
            "ttft_seconds": list(self.ttft),
            "tool_calls": sum(len(items) for items in self.tool_seconds.values()),
            "tool_seconds": tool_total,
            "tool_seconds_by_name": {name: sum(items) for name, items in sorted(self.tool_seconds.items())},
            "scheduling_seconds": max(0.0, wall - self.model_seconds - tool_total) if wall is not None else None,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }
