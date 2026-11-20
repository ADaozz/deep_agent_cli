"""Locally select evidence worth interpreting; keep numeric reports in the timeline."""

from collections import Counter
from statistics import median

def reply_text(value, *, proactive=False, partial=False):
    """Pass model speech through; only strip controls and cap length for the chat log."""
    del partial  # preview and final use the same visible text
    text = "".join(c for c in str(value or "") if c.isprintable() or c == "\n").strip()
    limit = 80 if proactive else 1600
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


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
    failures.sort(key=lambda f: f.get("started_at", f["finished_at"]))
    by_id = {(f.get("run_id"), f["tool_call_id"]): f for f in failures}
    repeated = Counter()
    categories = {}
    for f in failures:
        prior = by_id.get((f.get("run_id"), f["retry_of"])) if f.get("retry_basis") else None
        if prior and prior["error_category"] == f["error_category"]:
            repeated[f["tool_name"], f["error_category"]] += 1
        path = categories.setdefault(f["tool_name"], [])
        if not path or f["error_category"] != path[-1]:
            path.append(f["error_category"])
    for tool, path in categories.items():
        if len(path) > 1:
            findings.append(f"{tool} 的失败分类从 {path[-2]} 变为 {path[-1]}，可核对两次调用的参数和输出。")
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
