"""Merge current-session run events, Timeline metadata and checkpoint evidence."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
import json
from math import isfinite
import re
from typing import Any, Mapping

from agent.companion.timeline import category
from agent.sessions.summary import redact_summary_input
from agent.sessions.transcript import classify_tool_result

RESPONSE_LIMIT = 6000
OUTPUT_EXCERPT = 800
CALL_CACHE_LIMIT = 128
_TERMINAL = {"success", "failed", "cancelled"}
_SECRET_FIELD = re.compile(r"api.?key|token|secret|password|authorization|credential", re.I)


def diagnostic_error(code, message):
    return {"error": {"code": code, "message": message}, "truncated": False}


def _excerpt(text, secrets, limit):
    # Avoid processing a whole tool response. Drop the last partial line so a
    # truncated credential or multi-line key cannot be mistaken for plain text.
    if len(text) > 16384:
        head = text[:16384].rpartition("\n")[0]
        if "-----BEGIN" in head:
            head = head.split("-----BEGIN", 1)[0]
        head = head or "[超长内容已省略]"
        clipped = True
    else:
        head, clipped = text, False
    safe = redact_summary_input(head, secrets)
    safe = "".join(c for c in safe if c.isprintable() or c in "\n\t")
    if len(safe) > limit or clipped:
        return safe[:limit - 1].rstrip() + "…", True
    return safe, False


def _arguments(value, secrets):
    budget = 1000
    truncated = False

    def visit(item, depth=0):
        nonlocal budget, truncated
        if budget <= 0 or depth > 3:
            truncated = True
            return "…"
        budget -= 12
        if isinstance(item, Mapping):
            result = {}
            for index, (key, child) in enumerate(item.items()):
                if index >= 12 or budget <= 0:
                    truncated = True
                    break
                key = str(key)
                name, clipped = _excerpt(key, secrets, 64)
                truncated |= clipped
                budget -= len(name)
                result[name] = "[已隐藏]" if _SECRET_FIELD.search(key[:128]) else visit(child, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            result = []
            for index, child in enumerate(item):
                if index >= 8 or budget <= 0:
                    truncated = True
                    break
                result.append(visit(child, depth + 1))
            return result
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, int) and item.bit_length() < 64 or isinstance(item, float) and isfinite(item):
            return item
        text, clipped = _excerpt(item if isinstance(item, str) else "[未展示]", secrets, max(1, min(budget, 240)))
        budget -= len(text)
        truncated |= clipped
        return text

    return visit(value or {}), truncated


def result_status(status, *, name, content, artifact, is_error=False):
    if status == "cancelled" or artifact.get("cancelled") or artifact.get("status") == "cancelled":
        return "cancelled"
    if is_error or status in {"error", "failed", "completed", "success"}:
        return classify_tool_result(
            status="error" if is_error or status in {"error", "failed"} else status,
            name=name, content=content, artifact=artifact,
        )
    return status if status in {"running", "waiting"} else "unknown"


@dataclass(frozen=True)
class CallDetails:
    arguments: dict = field(default_factory=dict)
    output_excerpt: str = ""
    truncated: bool = False
    completed: bool = False


def call_arguments(arguments, secrets):
    data, truncated = _arguments(arguments, secrets)
    return CallDetails(arguments=data, truncated=truncated)


def call_result(details, content, artifact, secrets):
    excerpt, truncated = _excerpt(content, secrets, OUTPUT_EXCERPT)
    return replace(details, output_excerpt=excerpt, completed=True,
                   truncated=details.truncated or truncated or bool(artifact.get("truncated")))


@dataclass
class ToolCallView:
    tool_call_id: str
    name: str
    status: str
    source: str
    run_id: str = ""
    turn_id: str = ""
    message_id: str = ""
    agent_id: str = "main"
    started_at: float | None = None
    finished_at: float | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    error_category: str = ""
    details: CallDetails = field(default_factory=CallDetails)
    retry_of: str | None = None

    def row(self):
        return dict(
            tool_call_id=self.tool_call_id, run_id=self.run_id or None,
            turn_id=self.turn_id or None, name=self.name, agent_id=self.agent_id,
            status=self.status, outcome=self.status, started_at=self.started_at,
            finished_at=self.finished_at, exit_code=self.exit_code,
            error_category=self.error_category, duration_ms=self.duration_ms,
            source=self.source,
        )

    def detail(self):
        return {
            **self.row(), "tool": self.name, "arguments": self.details.arguments,
            "result": dict(exit_code=self.exit_code, status=self.status,
                           error_category=self.error_category, output_excerpt=self.details.output_excerpt,
                           truncated=self.details.truncated),
            "truncated": self.details.truncated,
        }


def _observation(obs, status, source, details):
    row = obs if isinstance(obs, Mapping) else vars(obs)
    return ToolCallView(
        row["tool_call_id"], row.get("tool_name") or "tool", status, source,
        **{key: row[key] for key in (
            "run_id", "turn_id", "message_id", "agent_id", "started_at", "finished_at",
            "duration_ms", "exit_code", "error_category", "retry_of",
        ) if key in row},
        details=details,
    )


def observation_views(timeline, live_calls=None, cache=None):
    views = {}

    def add(obs, source, status=None):
        row = obs if isinstance(obs, Mapping) else vars(obs)
        if not row.get("tool_call_id"):
            return
        session = getattr(timeline, "session", None)
        if session is not None and row.get("session_id", session) != session:
            return
        key = row.get("run_id", ""), row["tool_call_id"]
        details = (cache or {}).get(key)
        views[key] = _observation(obs, status or row.get("outcome") or "unknown",
                                  "live" if details is not None else source, details or CallDetails())

    if timeline is not None:
        for obs in getattr(timeline, "diagnostic_calls", getattr(timeline, "recent_tool_calls", ())):
            add(obs, "timeline")
        for obs in timeline.failures:
            add(obs, "timeline")
        for obs in getattr(timeline, "interrupted", {}).values():
            add(obs, "timeline", "unknown")
        for obs in timeline.active.values():
            add(obs, "live", "waiting" if obs.outcome == "waiting" else "running")
    for obs in (live_calls or {}).values():
        status = obs.outcome if obs.finished_at is not None or obs.outcome == "waiting" else "running"
        add(obs, "live", status)
    return views


def _checkpoint(block, secrets):
    artifact = block.artifact if isinstance(block.artifact, dict) else {}
    name = block.name or "tool"
    status = result_status(block.status, name=name, content=block.content,
                           artifact=artifact, is_error=block.is_error)
    details = call_arguments(block.arguments, secrets)
    if status in _TERMINAL:
        details = call_result(details, block.content, artifact, secrets)
    return ToolCallView(
        block.tool_call_id, name, status, "checkpoint", message_id=block.message_id,
        exit_code=block.exit_code,
        error_category=category(block.content, block.exit_code) if status == "failed" else "",
        details=details,
    )


def merge_calls(blocks, observations, secrets):
    remaining = dict(observations)
    checkpoints = [block for block in blocks if block.kind == "tool" and block.tool_call_id]
    counts = Counter(block.tool_call_id for block in checkpoints)
    by_id, by_message = defaultdict(list), defaultdict(list)
    for key, row in observations.items():
        if row.agent_id == "main":
            by_id[row.tool_call_id].append(key)
            if row.message_id:
                by_message[row.tool_call_id, row.message_id].append(key)
    calls = []
    for block in checkpoints:
        checkpoint = _checkpoint(block, secrets)
        matches = by_message[block.tool_call_id, block.message_id]
        if not matches and counts[block.tool_call_id] == 1:
            legacy = by_id[block.tool_call_id]
            if len(legacy) == 1 and (not block.message_id or not observations[legacy[0]].message_id):
                matches = legacy
        event = remaining.pop(matches[0], None) if len(matches) == 1 else None
        if event is None:
            calls.append(checkpoint)
            continue
        # Live completion wins. A checkpoint can settle pending observations
        # or supersede metadata restored from Timeline. Unknown proves nothing.
        status = event.status
        source = event.source
        if ((status == "unknown" and checkpoint.status != "unknown")
                or (checkpoint.status in _TERMINAL and (source != "live" or status not in _TERMINAL))):
            status, source = checkpoint.status, "checkpoint"
        details = CallDetails(
            arguments=checkpoint.details.arguments or event.details.arguments,
            output_excerpt=checkpoint.details.output_excerpt or event.details.output_excerpt,
            truncated=checkpoint.details.truncated or event.details.truncated,
            completed=checkpoint.details.completed or event.details.completed,
        )
        calls.append(replace(
            event, status=status, source=source, details=details,
            exit_code=checkpoint.exit_code if source == "checkpoint" and checkpoint.exit_code is not None
            else event.exit_code if event.exit_code is not None else checkpoint.exit_code,
            error_category=(event.error_category or checkpoint.error_category) if status == "failed" else "",
        ))
    calls.extend(remaining.values())
    return sorted(calls, key=lambda row: row.started_at if row.started_at is not None else float("-inf"))


def inspect_session_payload(
    blocks, timeline=None, live_calls=None, *, observations=None, secrets=(),
    view="recent_tools", tool_call_id=None, run_id=None, limit=10, status=None, order="asc",
):
    limit = max(1, min(int(limit or 10), 20))
    if view == "recent_messages":
        messages = []
        for block in blocks:
            if block.kind in {"user", "assistant"} and block.content.strip():
                content, truncated = _excerpt(block.content, secrets, 400)
                messages.append(dict(role=block.kind, content=content, truncated=truncated))
            elif block.kind == "tool":
                messages.append(dict(role="tool", tool_call_id=block.tool_call_id, name=block.name,
                                     status=block.status or "unknown"))
        return {"view": view, "messages": messages[-limit:], "truncated": len(messages) > limit}
    if view not in {"recent_tools", "tool_detail"}:
        return diagnostic_error("invalid_view", "Unknown inspection view.")
    if observations is None:
        observations = observation_views(timeline, live_calls)
    calls = merge_calls(blocks, observations, secrets)
    if view == "recent_tools":
        matched = [row for row in calls if status is None or row.status == status]
        rows = matched[-limit:]
        if order == "desc":
            rows.reverse()
        return {"view": view, "tools": [row.row() for row in rows], "truncated": len(matched) > limit}
    if not tool_call_id:
        return diagnostic_error("missing_tool_call_id", "tool_detail requires tool_call_id.")
    matches = [row for row in calls if row.tool_call_id == tool_call_id and (run_id is None or row.run_id == run_id)]
    if not matches:
        return diagnostic_error("tool_call_not_found", "Tool call was not found in the current session.")
    if len(matches) > 1:
        return {**diagnostic_error("ambiguous_tool_call_id", "Use the run_id from recent_tools."),
                "run_ids": [row.run_id or None for row in matches[-20:]],
                "truncated": len(matches) > 20}
    return {"view": view, **matches[0].detail()}


def format_inspect_response(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def build_session_inspector(*, load_blocks, companion_provider, secrets=()):
    """Capture the current identity and event data on the host event loop."""

    def prepare(**args):
        companion = companion_provider()
        thread = companion.thread
        generation = companion.session_generation
        observations = observation_views(companion.timeline, cache=companion.call_cache)
        secret_values = tuple(secrets() if callable(secrets) else secrets) + companion.secrets

        def inspect():
            def current():
                return companion.session() == thread and companion.session_generation == generation

            if not current():
                return diagnostic_error("request_expired", "The active session changed.")
            try:
                blocks = load_blocks(thread)
            except (OSError, ValueError, RuntimeError):
                blocks = None
            if not current():
                return diagnostic_error("request_expired", "The active session changed.")
            payload = inspect_session_payload(blocks or (), observations=observations, secrets=secret_values, **args)
            if blocks is None:
                payload["checkpoint_unavailable"] = True
            return payload

        return inspect

    return prepare
