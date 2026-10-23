"""Pure interrupt classification, message text, and run-event emission.

AgentRunner still owns the graph loop, cancellation, and session lease.
RunResult and RunEvent stay the public contracts; this module only decides
and records them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable, Sequence

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from agent.llm_stream import ModelStreamCancelled
from agent.session import tool_message_is_error
from agent.stream import visible_text


@dataclass(frozen=True)
class RunEvent:
    """Presentation-neutral event emitted while a graph run is progressing."""

    type: str
    content: str = ""
    message_id: str = ""
    tool_call_id: str = ""
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    is_error: bool = False
    stream: str = ""
    # Structured tool result payload (ToolMessage.artifact), forwarded as-is
    # for every tool, not just execute.
    artifact: Any = None


@dataclass(frozen=True)
class TurnTiming:
    elapsed_seconds: float
    finished_at: datetime


RunEventHandler = Callable[[RunEvent], None]


@dataclass
class RunResult:
    status: str
    output: str = ""
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    human_input: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class CompactResult:
    status: str
    used_tokens: int | None = None
    window_tokens: int = 0
    message: str = ""

    @property
    def percent(self) -> float | None:
        return self.used_tokens / self.window_tokens * 100 if self.window_tokens > 0 and self.used_tokens is not None else None


class InterruptKind(StrEnum):
    PAUSED = "paused"
    WAITING_HUMAN = "waiting_human"
    WAITING_CONFIRMATION = "waiting_confirmation"
    RUNTIME_CONFIG_BOUNDARY = "runtime_config_boundary"


class UnknownInterruptError(ValueError):
    """Checkpoint or stream has an interrupt that is not a known protocol."""


@dataclass(frozen=True)
class InterruptState:
    kind: InterruptKind
    payload: dict[str, Any]
    pending_tools: tuple[dict[str, Any], ...] = ()


def interrupt_payloads(interrupts: Any) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    items = interrupts if isinstance(interrupts, (list, tuple)) else [interrupts]
    for item in items:
        value = getattr(item, "value", item)
        if isinstance(value, dict):
            payloads.append(value)
        elif isinstance(value, (list, tuple)):
            for inner in value:
                inner_value = getattr(inner, "value", inner)
                if isinstance(inner_value, dict):
                    payloads.append(inner_value)
    return payloads


def resolve_pending_tool_calls(interrupts: Any, graph: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    requested: list[dict[str, Any]] = []
    for interrupt in interrupts if isinstance(interrupts, (list, tuple)) else [interrupts]:
        value = getattr(interrupt, "value", interrupt)
        if isinstance(value, dict):
            for action in value.get("action_requests", []) or []:
                requested.append({
                    "name": action.get("name"),
                    "args": action.get("args", {}),
                    "description": action.get("description", ""),
                })
    tool_call_ids: dict[str, list[str]] = {}
    try:
        state = graph.get_state(config)
        messages = state.values.get("messages", [])
        for message in reversed(messages):
            if isinstance(message, AIMessage) and message.tool_calls:
                for call in message.tool_calls:
                    tool_call_ids.setdefault(str(call.get("name")), []).append(str(call.get("id")))
                break
    except Exception:  # noqa: BLE001
        pass
    pending: list[dict[str, Any]] = []
    for action in requested:
        name = str(action["name"])
        ids = tool_call_ids.get(name, [])
        pending.append({
            "toolCallId": ids.pop(0) if ids else "",
            "name": name,
            "args": action["args"],
        })
    return pending


def is_valid_hitl_interrupt(payload: Any) -> bool:
    """True only for the locked LangChain HITLRequest schema (no type field)."""
    if not isinstance(payload, dict):
        return False
    if payload.get("type") not in (None, ""):
        return False
    requests = payload.get("action_requests")
    configs = payload.get("review_configs")
    if not isinstance(requests, list) or not requests:
        return False
    if not isinstance(configs, list) or not configs:
        return False
    for item in requests:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            return False
        if not isinstance(item.get("args"), dict):
            return False
    for item in configs:
        if not isinstance(item, dict) or not isinstance(item.get("action_name"), str):
            return False
        if not isinstance(item.get("allowed_decisions"), list):
            return False
    return True


def classify_interrupt(
    interrupts: Any, graph: Any, config: dict[str, Any],
) -> InterruptState:
    payloads = interrupt_payloads(interrupts)
    if not payloads:
        raise UnknownInterruptError("Interrupt has no recognizable payload")
    for payload in payloads:
        kind = str(payload.get("type") or "").strip().lower()
        if kind == "human_input":
            return InterruptState(InterruptKind.WAITING_HUMAN, payload)
        if kind == "pause":
            return InterruptState(InterruptKind.PAUSED, payload)
        if kind == "runtime_config_boundary":
            return InterruptState(InterruptKind.RUNTIME_CONFIG_BOUNDARY, payload)
        if is_valid_hitl_interrupt(payload):
            pending = resolve_pending_tool_calls(interrupts, graph, config)
            return InterruptState(InterruptKind.WAITING_CONFIRMATION, payload, tuple(pending))
        if kind:
            raise UnknownInterruptError(f"Unsupported interrupt type: {kind!r}")
    raise UnknownInterruptError(
        f"Unsupported interrupt payload: {sorted(payloads[0].keys()) if payloads[0] else 'empty dict'}"
    )


def interrupt_kind_from_state(graph: Any, config: dict[str, Any]) -> InterruptState | None:
    state = graph.get_state(config)
    interrupts = getattr(state, "interrupts", ()) or ()
    if not interrupts:
        return None
    return classify_interrupt(interrupts, graph, config)


def _last_message_id(values: dict[str, Any]) -> str | None:
    messages = values.get("messages") or []
    return getattr(messages[-1], "id", None) if messages else None


def _final_output(graph: Any, config: dict[str, Any]) -> str:
    state = graph.get_state(config)
    messages = state.values.get("messages", [])
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not message.tool_calls:
            return visible_text(message) or _message_text(message)
    return ""


def _message_text(message: Any) -> str:
    text = getattr(message, "text", None)
    if isinstance(text, str) and text:
        return text
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts)


def _messages_in_update(value: Any) -> list[BaseMessage]:
    """Find messages in LangGraph update envelopes without depending on node names."""
    found: list[BaseMessage] = []
    if isinstance(value, BaseMessage):
        return [value]
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "__interrupt__":
                continue
            found.extend(_messages_in_update(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_messages_in_update(item))
    return found


_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens")


def _usage_metadata_dict(message: BaseMessage) -> dict[str, int]:
    usage = getattr(message, "usage_metadata", None)
    if not isinstance(usage, dict):
        return {}
    return {key: int(usage[key]) for key in _USAGE_KEYS if type(usage.get(key)) is int and usage[key] >= 0}


def _usage_from_messages(messages: Any) -> dict[str, int]:
    """Most recent reported usage, which is the size of the context the model saw."""
    for message in reversed(list(messages or [])):
        if isinstance(message, AIMessage):
            if message.additional_kwargs.get("manual_compact_completed"):
                return {}
            return _usage_metadata_dict(message)
    return {}


def _emit(handler: RunEventHandler | None, event: RunEvent) -> None:
    if handler is not None:
        try:
            handler(event)
        except Exception:  # noqa: BLE001
            # Presentation observers must never change Agent/Tool execution semantics.
            pass


def _tool_decisions(
    pending: list[dict[str, Any]],
    *,
    decision_type: str,
    tool_call_ids: Sequence[str],
    message: str | None = None,
    others: str = "reject",
) -> dict[str, Any]:
    if decision_type not in ("approve", "reject"):
        raise ValueError("Tool decision must be approve or reject")
    targets = [str(item) for item in tool_call_ids if str(item)]
    if not targets:
        raise ValueError("Tool call id is required")
    pending_ids = [str(call.get("toolCallId", "")) for call in pending]
    for target in targets:
        if target not in pending_ids:
            raise ValueError(f"Tool call is no longer pending approval: {target}")
    item: dict[str, Any] = {"type": decision_type}
    if decision_type == "reject" and message:
        item["message"] = str(message)
    chosen = set(targets) if others == "same" else {targets[0]}
    if len(pending_ids) <= 1:
        return {"decisions": [dict(item) for _ in range(max(len(pending_ids), 1))]}
    return {"decisions": [
        dict(item) if tool_call_id in chosen
        else {"type": "reject", "message": "另一个并发的待确认调用未包含在本次人工决策中，按拒绝处理"}
        for tool_call_id in pending_ids
    ]}


def final_answer_committed(messages: Any, previous_message_id: str | None) -> bool:
    """True when this run checkpointed a final answer before the graph drained.

    After an answer without tool calls, only no-op middleware hooks remain, so a
    late cancel must not report the turn as cancelled.
    """
    if not messages or _last_message_id({"messages": messages}) == previous_message_id:
        return False
    last = messages[-1]
    return isinstance(last, AIMessage) and not last.tool_calls


def completed_result(output: str) -> RunResult:
    return RunResult(status="completed", output=output)


def interaction_result(
    status: str,
    pending: list[dict[str, Any]],
    human_input: dict[str, Any],
) -> RunResult:
    return RunResult(
        status=status,
        pending_tool_calls=list(pending),
        human_input=dict(human_input),
    )


def failed_result(error: str) -> RunResult:
    return RunResult(status="failed", error=error)


def cancelled_result(*, model_stream_closed: bool, unclosed_message: str) -> tuple[str, RunResult]:
    """Error text is empty only after the model request confirms it stopped."""
    error = "" if model_stream_closed else unclosed_message
    return error, RunResult(status="cancelled", error=error)


def normalize_stream_error(exc: BaseException, *, cancel_requested: bool) -> BaseException | None:
    """None means the caller should record a user cancel, not a failure."""
    if cancel_requested:
        return None
    if isinstance(exc, ModelStreamCancelled):
        return RuntimeError(f"Model stream stopped unexpectedly: {exc}")
    return exc


def update_tool_events(chunk: Any, seen: set[str]) -> tuple[list[RunEvent], bool]:
    """Tool and usage events for one graph update.

    Mutates ``seen`` so a replayed tool start is not emitted twice. The bool is
    true when the update carried input-token usage.
    """
    events: list[RunEvent] = []
    saw_input_usage = False
    for message in _messages_in_update(chunk):
        if isinstance(message, AIMessage):
            message_id = str(getattr(message, "id", "") or "")
            usage = _usage_metadata_dict(message)
            if "input_tokens" in usage:
                saw_input_usage = True
                events.append(RunEvent(type="usage", result=usage))
            for call in message.tool_calls or []:
                tool_call_id = str(call.get("id") or "")
                identity = tool_call_id or f"{call.get('name')}:{id(call)}"
                if identity in seen:
                    continue
                seen.add(identity)
                events.append(RunEvent(
                    type="tool_started",
                    message_id=message_id,
                    tool_call_id=tool_call_id,
                    name=str(call.get("name") or "tool"),
                    arguments=call.get("args") if isinstance(call.get("args"), dict) else {},
                ))
        elif isinstance(message, ToolMessage):
            content = _message_text(message)
            events.append(RunEvent(
                type="tool_completed",
                tool_call_id=str(getattr(message, "tool_call_id", "") or ""),
                name=str(getattr(message, "name", "") or "tool"),
                content=content,
                result=getattr(message, "content", content),
                is_error=tool_message_is_error(message),
                artifact=getattr(message, "artifact", None),
            ))
    return events, saw_input_usage
