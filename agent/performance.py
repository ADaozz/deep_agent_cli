"""Provider-neutral output throughput measured at stream chunk arrival times."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any


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
