"""Throughput policy for providers with partial or hidden reasoning metadata."""
import pytest
from agent.performance import GenerationTiming


@pytest.mark.parametrize("details", [{"reasoning": 20}, {"priority_reasoning": 20}, {"flex_reasoning": 20}])
def test_hidden_reasoning_preserves_counts_but_does_not_inflate_total_tps(details):
    timing = GenerationTiming(0.0)
    timing.mark(30.0, reasoning=False, answer=True)
    timing.mark(40.0, reasoning=False, answer=True)
    report = timing.snapshot({"output_tokens": 30, "output_token_details": details})
    assert report["thinking"] is True
    assert report["tps"] is report["reasoning_tps"] is report["output_duration"] is None
    assert report["answer_tps"] == 1 and report["ttft"] == 30


@pytest.mark.parametrize("usage", [None, {}, {"output_tokens": 10},
                                   {"output_tokens": 10, "output_token_details": {"reasoning": 100}},
                                   {"output_tokens": 10, "output_token_details": {"reasoning": -1}}])
def test_missing_or_invalid_reasoning_breakdown_is_unknown(usage):
    timing = GenerationTiming(0.0)
    timing.mark(1.0, reasoning=False, answer=True)
    timing.mark(2.0, reasoning=False, answer=True)
    report = timing.snapshot(usage)
    assert report["reasoning_tokens"] is report["answer_tokens"] is report["answer_tps"] is None


def test_single_chunk_does_not_invent_generation_duration():
    timing = GenerationTiming(0.0)
    timing.mark(10.0, reasoning=True, answer=True)
    report = timing.snapshot({"output_tokens": 30, "output_token_details": {"reasoning": 20}})
    assert report["output_duration"] == 0
    assert report["tps"] is report["reasoning_tps"] is report["answer_tps"] is None


def test_metrics_group_by_model_and_effort_with_independent_percentiles():
    from agent.performance import model_metric_groups

    history = [{"model": "deepseek-flash", "reasoning_effort": "low", "tps": float(n)} for n in range(1, 22)]
    history += [{"model": "deepseek-flash", "reasoning_effort": "high", "tps": 100.0},
                {"model": "qwen", "reasoning_effort": "low", "tps": 200.0}]
    groups = model_metric_groups(history)
    assert len(groups) == 3
    assert groups[0]["tps_low"] == 1.5 and groups[0]["tps_top"] == 20.5
    assert groups[0]["tps_average"] == 11.0 and groups[0]["sample_count"] == 21
    assert groups[1]["tps_average"] == 100.0 and groups[2]["tps_average"] == 200.0
    for value in [None, -1, float("nan"), float("inf"), True, "10"]:
        history.append({"model": "deepseek-flash", "reasoning_effort": "low", "tps": value})
    group = model_metric_groups(history)[0]
    assert group["sample_count"] == 21 and group["tps_average"] == 11.0
