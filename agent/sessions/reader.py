"""Read a session snapshot without acquiring or changing its live lease."""

from __future__ import annotations

from typing import Any

from agent.sessions.contracts import SessionInfo
from agent.sessions.snapshots import SessionSnapshot
from agent.sessions.ports import SessionRepository
from agent.sessions.transcript import messages_to_transcript, settle_restored_tools
from agent.runtime.contracts import InterruptKind
from agent.runtime.events import interrupt_kind_from_state


def read_snapshot(graph: Any, info: SessionInfo, repository: SessionRepository | None) -> SessionSnapshot:
    config = {"configurable": {"thread_id": info.id}}
    try:
        state = graph.get_state(config)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed to load session {info.id}: {exc}") from exc
    messages = list((state.values or {}).get("messages", []) or [])
    interrupt = interrupt_kind_from_state(graph, config)
    transcript = messages_to_transcript(
        messages,
        turn_timings=repository.turn_timings(info.id) if repository is not None else None,
    )
    settle_restored_tools(
        transcript,
        waiting_ids={
            str(call.get("toolCallId") or "")
            for call in interrupt.pending_tools
        } if interrupt is not None else set(),
        waiting_human=interrupt is not None and interrupt.kind is InterruptKind.WAITING_HUMAN,
    )
    return SessionSnapshot(
        info=info,
        transcript=transcript,
        model_metrics_history=repository.model_metrics_history(info.id) if repository is not None else [],
        interrupt_kind=interrupt.kind if interrupt is not None else None,
        human_input=interrupt.payload if interrupt is not None and interrupt.kind is InterruptKind.WAITING_HUMAN else {},
        pending_tool_calls=list(interrupt.pending_tools) if interrupt is not None else [],
    )
