"""Read-only inspection of the current main-agent session for Lain."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from agent.sessions.summary import redact_summary_input
from agent.sessions.transcript import classify_tool_result

RESPONSE_LIMIT = 6000
OUTPUT_EXCERPT = 800
ARG_LIMIT = 240


def _brief(value: Any, limit: int = ARG_LIMIT) -> Any:
    if isinstance(value, str):
        text = " ".join(value.split())
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
    if isinstance(value, Mapping):
        return {str(key)[:64]: _brief(item, max(40, limit // 2)) for key, item in list(value.items())[:12]}
    if isinstance(value, (list, tuple)):
        return [_brief(item, max(40, limit // 2)) for item in value[:8]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _brief(str(value), limit)


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return redact_summary_input(value, secrets)
    if isinstance(value, Mapping):
        return {key: _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    return value


def _row_from_obs(obs) -> dict[str, Any]:
    if isinstance(obs, Mapping):
        return {
            "tool_call_id": str(obs.get("tool_call_id") or ""),
            "tool_name": obs.get("tool_name") or "",
            "outcome": obs.get("outcome") or "",
            "exit_code": obs.get("exit_code"),
            "error_category": obs.get("error_category") or "",
            "duration_ms": obs.get("duration_ms"),
            "finished_at": obs.get("finished_at"),
        }
    return {
        "tool_call_id": str(getattr(obs, "tool_call_id", "") or ""),
        "tool_name": getattr(obs, "tool_name", "") or "",
        "outcome": getattr(obs, "outcome", "") or "",
        "exit_code": getattr(obs, "exit_code", None),
        "error_category": getattr(obs, "error_category", "") or "",
        "duration_ms": getattr(obs, "duration_ms", None),
        "finished_at": getattr(obs, "finished_at", None),
    }


def _timeline_index(timeline, live_calls: Mapping[str, Any] | None = None) -> dict[str, Any]:
    by_id: dict[str, Any] = {}
    if timeline is not None:
        for obs in getattr(timeline, "failures", ()):
            row = _row_from_obs(obs)
            if row["tool_call_id"]:
                by_id[row["tool_call_id"]] = row
        for obs in getattr(timeline, "recent_tool_calls", ()):
            row = _row_from_obs(obs)
            if row["tool_call_id"]:
                by_id[row["tool_call_id"]] = row
        for obs in getattr(timeline, "active", {}).values():
            row = _row_from_obs(obs)
            if row["tool_call_id"]:
                by_id[row["tool_call_id"]] = row
    for obs in (live_calls or {}).values():
        row = _row_from_obs(obs)
        if row["tool_call_id"]:
            by_id[row["tool_call_id"]] = row
    return by_id


def _tool_rows(blocks, timeline, live_calls=None, *, secrets: tuple[str, ...], limit: int) -> list[dict]:
    live = _timeline_index(timeline, live_calls)
    tools = [block for block in blocks if block.kind == "tool"]
    rows = []
    for block in tools[-limit:]:
        live_row = live.get(block.tool_call_id, {})
        artifact = block.artifact if isinstance(block.artifact, dict) else {}
        outcome = classify_tool_result(
            status="error" if block.is_error or block.status in {"error", "cancelled"} else block.status,
            content=block.content or "",
            name=block.name,
            artifact=artifact,
        )
        if block.status == "running":
            outcome = "running"
            source = "live" if live_row or block.status == "running" else "checkpoint"
        else:
            source = "checkpoint"
            if live_row.get("outcome") in ("success", "failed", "cancelled"):
                outcome = {
                    "success": "success",
                    "failed": "failed",
                    "cancelled": "cancelled",
                }.get(live_row["outcome"], outcome)
                source = "live" if live_row.get("finished_at") else source
        rows.append({
            "tool_call_id": block.tool_call_id,
            "name": block.name or "tool",
            "outcome": outcome,
            "exit_code": live_row.get("exit_code", block.exit_code),
            "error_category": live_row.get("error_category") or "",
            "duration_ms": live_row.get("duration_ms"),
            "source": source,
        })
    return rows


def _tool_detail(blocks, timeline, live_calls=None, *, tool_call_id: str, secrets: tuple[str, ...]) -> dict:
    live = _timeline_index(timeline, live_calls).get(tool_call_id, {})
    block = next((item for item in reversed(blocks) if item.kind == "tool" and item.tool_call_id == tool_call_id), None)
    if block is None and not live:
        return {"error": "tool_call_id not found in the current session"}
    artifact = block.artifact if block is not None and isinstance(block.artifact, dict) else {}
    content = block.content if block is not None else ""
    name = (block.name if block is not None else "") or live.get("tool_name") or "tool"
    status = block.status if block is not None else live.get("outcome") or "unknown"
    outcome = classify_tool_result(
        status="error" if (block is not None and block.is_error) or status in {"error", "failed", "cancelled"} else status,
        content=content or "",
        name=name,
        artifact=artifact,
    )
    if status == "running":
        outcome = "running"
    arguments = _redact(_brief(block.arguments or {} if block is not None else {}), secrets)
    excerpt = _brief(_redact(content, secrets), OUTPUT_EXCERPT)
    return {
        "tool_call_id": tool_call_id,
        "tool": name,
        "arguments": arguments,
        "result": {
            "exit_code": live.get("exit_code", block.exit_code if block is not None else None),
            "status": outcome,
            "error_category": live.get("error_category") or "",
            "output_excerpt": excerpt,
        },
        "duration_ms": live.get("duration_ms"),
        "source": "live" if live.get("finished_at") or status == "running" else "checkpoint",
    }


def _recent_messages(blocks, *, secrets: tuple[str, ...], limit: int) -> list[dict]:
    messages = []
    for block in blocks:
        if block.kind == "user":
            messages.append({
                "role": "user",
                "content": _brief(_redact(block.content, secrets), 400),
            })
        elif block.kind == "assistant":
            text = block.content.strip()
            if not text:
                continue
            messages.append({
                "role": "assistant",
                "content": _brief(_redact(text, secrets), 400),
            })
        elif block.kind == "tool":
            messages.append({
                "role": "tool",
                "tool_call_id": block.tool_call_id,
                "name": block.name or "tool",
                "status": block.status or ("error" if block.is_error else "completed"),
            })
    return messages[-limit:]


def inspect_session_payload(
    blocks,
    timeline=None,
    live_calls=None,
    *,
    secrets: tuple[str, ...] = (),
    view: str = "recent_tools",
    tool_call_id: str | None = None,
    limit: int = 10,
) -> dict:
    limit = max(1, min(int(limit or 10), 20))
    view = str(view or "recent_tools")
    if view == "recent_tools":
        return {
            "view": view,
            "tools": _tool_rows(blocks, timeline, live_calls, secrets=secrets, limit=limit),
        }
    if view == "tool_detail":
        call = str(tool_call_id or "").strip()
        if not call:
            return {"error": "tool_detail requires tool_call_id"}
        return {
            "view": view,
            **_tool_detail(blocks, timeline, live_calls, tool_call_id=call, secrets=secrets),
        }
    if view == "recent_messages":
        return {"view": view, "messages": _recent_messages(blocks, secrets=secrets, limit=limit)}
    return {"error": f"unknown view: {view}"}


def format_inspect_response(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)[:RESPONSE_LIMIT]


def build_session_inspector(
    *,
    load_blocks: Callable[[], list],
    companion_provider: Callable[[], Any],
    secrets: Callable[[], tuple[str, ...]] | tuple[str, ...] = (),
) -> Callable[..., str]:
    """Host-side binder: always inspects the currently attached session."""

    def inspect(view: str = "recent_tools", tool_call_id: str | None = None, limit: int = 10) -> str:
        try:
            blocks = list(load_blocks() or [])
        except Exception as exc:  # noqa: BLE001
            return format_inspect_response({"error": f"session unavailable: {type(exc).__name__}"})
        companion = companion_provider()
        secret_values = secrets() if callable(secrets) else secrets
        payload = inspect_session_payload(
            blocks,
            getattr(companion, "timeline", None),
            getattr(companion, "calls", None),
            secrets=tuple(secret_values or ()),
            view=view,
            tool_call_id=tool_call_id,
            limit=limit,
        )
        return format_inspect_response(payload)

    return inspect
