"""Merge current-session run events, Timeline metadata and checkpoint evidence."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
import json
from math import isfinite
import re
import sqlite3
from typing import Any, Mapping

from agent.companion.timeline import category
from agent.sessions.summary import redact_summary_input
from agent.sessions.transcript import classify_tool_result

RESPONSE_LIMIT = 6000
OUTPUT_EXCERPT = 800
CALL_CACHE_LIMIT = 128
_TERMINAL = {"success", "failed", "cancelled"}
_SECRET_FIELD = re.compile(r"api.?key|token|secret|password|authorization|credential", re.I)
_SECRET_OPTION = re.compile(
    r"(--[\w-]*(?:api[-_]?key|token|secret|password|authorization|credential)[\w-]*)(?:\s+|=)"
    r"(?:\"[^\"\r\n]*(?:\"|$)|'[^'\r\n]*(?:'|$)|[^\s;]+)", re.I,
)
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_AUTH_HEADER = re.compile(r"\b(?:proxy-)?authorization\s*[:=]\s*(?:Bearer|Basic)\s+[^\s,;]+", re.I)


def diagnostic_error(code, message):
    return {"error": {"code": code, "message": message}, "truncated": False}


def redact_diagnostic_text(text, secrets=()):
    text = _ANSI_ESCAPE.sub("", text)
    text = _PRIVATE_KEY.sub("[已隐藏]", text)
    text = _AUTH_HEADER.sub("[已隐藏]", text)
    text = _SECRET_OPTION.sub(r"\1 [已隐藏]", text)
    return redact_summary_input(text, secrets, preserve_relative_paths=True)


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
    safe = redact_diagnostic_text(head, secrets)
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
    arguments_truncated: bool = False
    output_truncated: bool = False

    @property
    def truncated(self):
        return self.arguments_truncated or self.output_truncated

    def matches_arguments(self, other):
        return (
            bool(self.arguments) and not (self.arguments_truncated or other.arguments_truncated)
            and self.arguments == other.arguments
            and "[已隐藏]" not in json.dumps(self.arguments, ensure_ascii=False)
        )


def call_arguments(arguments, secrets):
    data, truncated = _arguments(arguments, secrets)
    return CallDetails(arguments=data, arguments_truncated=truncated)


def call_result(details, content, artifact, secrets):
    excerpt, truncated = _excerpt(content, secrets, OUTPUT_EXCERPT)
    return replace(details, output_excerpt=excerpt, output_truncated=truncated or bool(artifact.get("truncated")))


@dataclass(frozen=True)
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
    resumed_from_checkpoint: bool = False

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
            "arguments_truncated": self.details.arguments_truncated,
            "result": dict(exit_code=self.exit_code, status=self.status,
                           error_category=self.error_category, output_excerpt=self.details.output_excerpt,
                           truncated=self.details.output_truncated),
            "truncated": self.details.truncated,
        }


def _observation(obs, status, source, details):
    row = obs if isinstance(obs, Mapping) else vars(obs)
    return ToolCallView(
        row["tool_call_id"], row.get("tool_name") or "tool", status, source,
        **{key: row[key] for key in (
            "run_id", "turn_id", "message_id", "agent_id", "started_at", "finished_at",
            "duration_ms", "exit_code", "error_category", "retry_of", "resumed_from_checkpoint",
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

    # A resume can finish before the persisted Timeline has been loaded.
    # The checkpoint's parent AI message identifies that same call across runs.
    origins = defaultdict(list)
    for key, row in sorted(views.items(), key=lambda item: item[1].started_at or 0):
        if row.agent_id != "main" or not row.message_id:
            continue
        origin = row.message_id, row.tool_call_id
        prior = origins[origin]
        if not row.resumed_from_checkpoint or len(prior) != 1:
            prior.append(key)
            continue
        first_key = prior[0]
        first = views[first_key]
        if first.status in _TERMINAL and row.status not in _TERMINAL:
            views.pop(key)
            continue
        views[first_key] = replace(
            row, run_id=first.run_id, turn_id=first.turn_id, started_at=first.started_at,
            details=replace(row.details, arguments=row.details.arguments or first.details.arguments,
                            arguments_truncated=row.details.arguments_truncated or first.details.arguments_truncated),
        )
        views.pop(key)
    return views


def _checkpoint(block, secrets, include_details):
    artifact = block.artifact if isinstance(block.artifact, dict) else {}
    name = block.name or "tool"
    status = result_status(block.status, name=name, content=block.content,
                           artifact=artifact, is_error=block.is_error)
    details = CallDetails()
    if include_details:
        details = call_arguments(block.arguments, secrets)
        if status in _TERMINAL:
            details = call_result(details, block.content, artifact, secrets)
    return ToolCallView(
        block.tool_call_id, name, status, "checkpoint", message_id=block.message_id,
        exit_code=block.exit_code,
        error_category=category(block.content, block.exit_code) if status == "failed" else "",
        details=details,
    )


def merge_calls(blocks, observations, secrets, *, detail_id=None):
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
        checkpoint = _checkpoint(block, secrets, block.tool_call_id == detail_id)
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
        conflict = event.status in _TERMINAL and checkpoint.status in _TERMINAL and event.status != checkpoint.status
        preferred = event if conflict and source == "live" else checkpoint
        output = preferred.details.output_excerpt
        if not conflict:
            output = output or event.details.output_excerpt
        error = preferred.error_category
        if not conflict and event.error_category and error in {"", "exit_code", "tool_error"}:
            error = event.error_category
        code = preferred.exit_code
        if not conflict and code is None:
            code = event.exit_code
        details = CallDetails(
            arguments=checkpoint.details.arguments or event.details.arguments,
            output_excerpt=output,
            arguments_truncated=checkpoint.details.arguments_truncated or event.details.arguments_truncated,
            output_truncated=checkpoint.details.output_truncated or event.details.output_truncated,
        )
        calls.append(replace(
            event, status=status, source=source, details=details,
            exit_code=code, error_category=error if status == "failed" else "",
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
    calls = merge_calls(blocks, observations, secrets, detail_id=tool_call_id if view == "tool_detail" else None)
    if view == "recent_tools":
        matched = [row for row in calls if status is None or row.status == status]
        rows = matched[-limit:]
        if order == "desc":
            rows.reverse()
        return {"view": view, "order": order, "tools": [row.row() for row in rows],
                "truncated": len(matched) > limit}
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
    def encode(data):
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    result = encode(payload)
    if len(result.encode()) <= RESPONSE_LIMIT:
        return result
    data = {**payload, "truncated": True}
    for key in ("tools", "messages", "records"):
        if key not in data:
            continue
        rows = list(data[key])
        data[key] = rows
        while rows:
            rows.pop(0 if key == "messages" or data.get("order") == "asc" else -1)
            result = encode(data)
            if len(result.encode()) <= RESPONSE_LIMIT:
                return result
    if "result" in data and "arguments" in data:
        data["arguments"] = {}
        data["arguments_truncated"] = True
        data["result"] = {**data["result"], "output_excerpt": "", "truncated": True}
        result = encode(data)
        if len(result.encode()) <= RESPONSE_LIMIT:
            return result
    return encode({**diagnostic_error("response_budget", "Response exceeded the diagnostic budget."), "truncated": True})


def build_session_inspector(*, load_blocks, companion_provider, secrets=()):
    """Capture the current identity and event data on the host event loop."""

    def prepare(**args):
        companion = companion_provider()
        thread = companion.thread
        generation = companion.session_generation
        observations = observation_views(companion.timeline, cache=companion.call_cache)
        secret_values = tuple(secrets() if callable(secrets) else secrets) + companion.secrets

        def inspect(budget=None):
            def current():
                return companion.session() == thread and companion.session_generation == generation

            if not current():
                return diagnostic_error("request_expired", "The active session changed.")
            try:
                loaded = load_blocks(thread, budget)
            except TimeoutError:
                raise
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                if budget:
                    budget.check()
                loaded = None
            truncated = False
            if isinstance(loaded, tuple):
                blocks, truncated = loaded
            else:
                blocks = loaded
            if budget:
                budget.check()
            if not current():
                return diagnostic_error("request_expired", "The active session changed.")
            payload = inspect_session_payload(blocks or (), observations=observations, secrets=secret_values, **args)
            if budget:
                budget.check()
            if not current():
                return diagnostic_error("request_expired", "The active session changed.")
            if blocks is None:
                payload.update(checkpoint_unavailable=True, truncated=True)
            if truncated:
                payload.update(checkpoint_truncated=True, truncated=True)
            return payload

        return inspect

    return prepare
