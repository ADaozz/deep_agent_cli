"""Locally select evidence worth interpreting; keep numeric reports in the timeline."""

from collections import Counter
import re
from statistics import median

HAN = re.compile(r"[\u3400-\u9fff]")


def reply_text(value, *, proactive=False, partial=False):
    """Bound visible speech independently of model adherence. Preserve complete sentences."""
    text = " ".join(str(value or "").split())
    # Keep technical identifiers inside Chinese sentences while dropping foreign-language boilerplate.
    sentences = re.split(r"(?<=[。！？!?])|(?<=[.])\s+", text)
    text = "".join(
        s.strip()
        for s in [s for s in sentences if partial or HAN.search(s)][: 2 if proactive else 3]
    )
    limit = 80 if proactive else 220
    if not partial and not HAN.search(text[:limit]):
        return "" if proactive else "我没想好怎么答。你能再说具体一点吗？"
    if len(text) <= limit:
        return text
    endings = list(re.finditer(r"[。！？]", text[:limit]))
    return text[: endings[-1].end()] if endings else text[: limit - 1].rstrip() + "…"


def interpret_turn(summary, failures, samples):
    findings = []
    turn = summary["turn"]
    failures = [f for f in failures if f["turn_id"] == turn]
    for tool in summary["tools"]:
        start = tool.get("start_pct")
        known = tool["outcomes"].get("failed", 0) + tool["outcomes"].get("success", 0)
        if start is not None and known >= 3 and abs(tool["recent_pct"] - start) >= 10:
            direction = "上升" if tool["recent_pct"] > start else "下降"
            findings.append(f"{tool['tool']} 的近期失败率在本轮{direction}。")
    by_id = {f["tool_call_id"]: f for f in failures}
    repeated = Counter()
    for f in failures:
        prior = by_id.get(f["retry_of"])
        if prior and prior["error_category"] == f["error_category"]:
            repeated[f["tool_name"], f["error_category"]] += 1
    for tool, category in repeated:
        findings.append(
            f"{tool} 疑似重试后仍出现同类错误（{category}），原因尚待核对。"
        )

    def near_failure(stamp, tolerance):
        return any(abs(stamp - f["finished_at"]) <= tolerance for f in failures)

    pressure = set()
    for stamp, kind, data in samples:
        if kind == "resources" and near_failure(stamp, 4):
            percents = data.get("percent", []) if isinstance(data, dict) else data
            for name, value in zip(("CPU", "GPU", "RAM", "DISK"), percents):
                if value is not None and value >= 90:
                    pressure.add(name)
        if (
            kind == "git"
            and near_failure(stamp, 5)
            and len(summary["git"]) >= 2
            and data != summary["git"][0]
        ):
            note = "Git 状态在失败附近发生变化；尚未定位到具体改动。"
            if note not in findings:
                findings.append(note)
    if pressure:
        findings.append(
            f"失败附近采样到 {'、'.join(sorted(pressure))} 高占用；尚不能据此判定根因。"
        )
    for incident in summary["incidents"]:
        scope = "整个会话" if incident.get("scope") == "session" else incident["key"][2]
        if scope == "*":
            scope = "当前模型的工具调用"
        if incident["state"] == "resolved":
            findings.append(f"{scope} 的异常已满足本地恢复条件，根因是否消除仍待验证。")
        else:
            findings.append(f"{scope} 的可靠性异常仍在观察，尚未确认恢复。")
    previous = summary["previous_turn_ms"]
    elapsed = summary.get("duration_ms")
    if (
        len(previous) >= 2
        and elapsed
        and elapsed >= max(median(previous) * 1.5, median(previous) + 2000)
    ):
        tools = summary["tools"]
        longest = max(tools, key=lambda t: t["duration_ms"], default=None)
        note = "本轮比近期轮次明显更慢。"
        if longest and longest["duration_ms"]:
            note += f"工具耗时集中在 {longest['tool']}，可先检查它的等待或重试。"
        findings.append(note)
    return findings or ["现有证据未显出值得单独解释的变化。"]
