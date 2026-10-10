# Stream the compiled graph, classify LangGraph interrupts, resume with Command.
# Token-level reasoning/assistant deltas use LangChain callbacks (ChatOpenAI streaming=True).
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import StrEnum
from functools import wraps
from pathlib import Path
import os
import tempfile
import time
from threading import Lock
from typing import Any, Callable, Sequence
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.errors import GraphDrained
from langgraph.types import Command

from deepagents.backends.protocol import BackendProtocol
from agent.cancel import set_output_emitter
from agent.attachments import (
    ATTACHMENT_META_KEY,
    MAX_IMAGES_PER_MESSAGE,
    AttachmentCleanupResult,
    AttachmentStore,
    ImageAttachment,
    ImageAttachmentRef,
    refs_to_dicts,
)
from agent.config import InputKind, ModelProfile, SandboxConfig, Settings
from agent.control import RunController
from agent.factory import AgentSpec, PreparedAgent, build_agent
from agent.sandbox_pool import SandboxPool
from agent.llm import build_chat_model
from agent.middleware.attachments import reset_attachment_store, set_attachment_store
from agent.middleware.recovery import RecoveryContext, reset_recovery_context, set_recovery_context
from agent.permission import (
    PermissionMode,
    allow_mode_available,
    allow_mode_unavailable_reason,
    interrupt_on_for_mode,
    parse_permission_mode,
    permission_mode_from_interrupt_on,
)
from agent.session import (
    SessionInfo,
    SessionStore,
    StopReason,
    TranscriptBlock,
    messages_to_transcript,
    settle_restored_tools,
    tool_message_is_error,
    workspace_state_path,
)
from agent.session_lock import SessionLockBusyError
from agent.session_runtime import RestorePlan, SessionLeaseLike, SessionRuntime
from agent.stream import DeltaHandler, StreamDeltaCallback, merge_stream_callbacks, visible_text

HUMAN_TOOLS = frozenset({"request_human_input"})

# Maximum time close() waits for an active operation to drain. Timing out never
# releases the session lease early: close() raises, keeps the lease, and can be
# retried once the run finally drained.
CLOSE_DRAIN_SECONDS = 30.0


def _exclusive_operation(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def guarded(self: AgentRunner, *args: Any, **kwargs: Any) -> Any:
        if self._closed:
            raise RuntimeError("Runner is closed")
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError("Runner already has an active operation")
        try:
            return method(self, *args, **kwargs)
        finally:
            self._operation_lock.release()
    return guarded


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


@dataclass
class SessionSnapshot:
    info: SessionInfo
    transcript: list[TranscriptBlock]
    todos: list[dict[str, str]] = field(default_factory=list)
    interrupt_kind: InterruptKind | None = None
    human_input: dict[str, Any] = field(default_factory=dict)
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SessionSwitchResult:
    """Phase one of a session switch.

    ``busy`` means the target thread is owned elsewhere: the runner has already
    released its own lease and refuses operations until the wait completes or
    another session is selected.
    """

    status: str  # "switched" | "busy"
    target_id: str = ""
    snapshot: SessionSnapshot | None = None

    @property
    def busy(self) -> bool:
        return self.status == "busy"


class AgentRunner:
    def __init__(
        self,
        *,
        prepared: PreparedAgent | None = None,
        model: BaseChatModel | None = None,
        checkpointer: Any | None = None,
        backend: BackendProtocol | None = None,
        sandbox_config: SandboxConfig | None = None,
        thread_id: str | None = None,
        on_delta: DeltaHandler | None = None,
        on_event: RunEventHandler | None = None,
        session_store: SessionStore | None = None,
        enable_sessions: bool = False,
        persist_on_first_message: bool = False,
        workspace: Path | None = None,
        settings: Settings | None = None,
        model_id: str | None = None,
    ) -> None:
        if prepared is not None and (backend is not None or sandbox_config is not None):
            raise ValueError("prepared already defines the backend and sandbox configuration")
        self.control = prepared.run_controller if prepared is not None and prepared.run_controller is not None else RunController()
        self.control.set_event_handler(self._on_control_event)
        self.on_delta = on_delta
        self.on_event = on_event
        self._event_handler: RunEventHandler | None = None
        self._seen_tool_calls: set[str] = set()
        self._todo_call_ids: set[str] = set()
        self.settings = settings
        self._sandbox_config = sandbox_config or (settings.sandbox if settings else None)
        self._busy = False
        self._operation_lock = Lock()
        self._permission_mode = PermissionMode.ASK
        self._pending_config_lock = Lock()
        self._reasoning_effort = "default"
        self._pending_reasoning_effort: str | None = None
        self._pending_model_id: str | None = None
        self._pending_permission_mode: PermissionMode | None = None
        self._context_usage_invalidated = False
        self._resume_context: RecoveryContext | None = None
        self._closed = False
        self._close_completed = False
        self._runtime: SessionRuntime | None = None

        self.session_store = session_store
        if self.session_store is None and enable_sessions:
            root = workspace or (
                self._sandbox_config.workspace if self._sandbox_config else Path.cwd()
            )
            self.session_store = SessionStore.for_workspace(root)

        self._attachment_tempdir: tempfile.TemporaryDirectory[str] | None = None
        if self.session_store is not None:
            self.attachment_store = self.session_store.attachment_store
        else:
            self._attachment_tempdir = tempfile.TemporaryDirectory(prefix="deep-agent-attachments-")
            self.attachment_store = AttachmentStore(Path(self._attachment_tempdir.name))

        saver = checkpointer
        if saver is not None and self.session_store is not None and saver is not self.session_store.checkpointer:
            raise ValueError("checkpointer must be the session store checkpointer")
        if saver is None and self.session_store is not None:
            saver = self.session_store.checkpointer
        if saver is None and prepared is not None:
            saver = prepared.checkpointer

        cfg = settings or Settings()
        profile = cfg.get_profile(model_id) if model_id else cfg.active_profile
        self._current_model_id = profile.id
        initial_model = model or (prepared.model if prepared is not None else build_chat_model(
            profile, attachment_store=self.attachment_store,
        ))
        object.__setattr__(initial_model, "_deep_agent_model_id", self._current_model_id)
        self._spec = prepared.spec if prepared is not None else AgentSpec(
            instructions=cfg.agent_instructions,
            backend=backend, sandbox=self._sandbox_config or cfg.sandbox,
            web_search_api_key=os.environ.get("TAVILY_API_KEY") or cfg.tavily_api_key,
        )
        self._pause_condition = prepared.pause_condition if prepared is not None else (lambda: False)
        self._custom_interrupt_on = None
        if prepared is not None and prepared.interrupt_on and prepared.interrupt_on != interrupt_on_for_mode(PermissionMode.ASK):
            self._custom_interrupt_on = dict(prepared.interrupt_on)
        if prepared is not None:
            inferred = permission_mode_from_interrupt_on(prepared.interrupt_on)
            if inferred is PermissionMode.ALLOW and not allow_mode_available(prepared.execution_mode):
                raise ValueError(allow_mode_unavailable_reason(prepared.execution_mode))
            self._permission_mode = inferred
        self.prepared = prepared if prepared is not None and saver is prepared.checkpointer and initial_model is prepared.model else build_agent(
            self._spec, initial_model, self._permission_mode, saver,
            self.control, self._pause_condition,
            interrupt_on_override=prepared.interrupt_on if prepared is not None else None,
            backend_selection=prepared.backend_selection if prepared is not None else None,
        )
        self._checkpointer = self.prepared.checkpointer
        self._chat_model = initial_model

        # Every runner owns exactly one lease for the thread it may write.
        self._runtime = SessionRuntime(
            session_store=self.session_store, checkpointer=self._checkpointer,
            settings=self.settings,
            persist_on_first_message=persist_on_first_message,
        )
        self._runtime.acquire_initial(
            thread_id, model_id=self._current_model_id,
            permission_mode=self._permission_mode.value,
        )
        if self.session_store is not None:
            saved = self.session_store.get(self.thread_id)
            if saved is not None:
                plan = self._runtime.restore_plan(saved, execution_mode=self.prepared.execution_mode)
                try:
                    self._apply_restore_plan(plan)
                except Exception:
                    self._runtime.release()
                    raise
                for notice in plan.notices:
                    _emit(self.on_event, RunEvent(type="runtime_config_failed", content=notice))
                self._pending_reasoning_effort = saved.pending_reasoning_effort
                self._pending_model_id = saved.pending_model_id
                self._pending_permission_mode = parse_permission_mode(saved.pending_permission_mode or "")
        self.control.set_pending_config_check(self._has_pending_runtime_config)

        self.state_path = (
            self.session_store.path if self.session_store is not None
            else workspace_state_path(workspace or Path.cwd())
        )

    @property
    def thread_id(self) -> str:
        """The thread this runner currently points at (lease may be absent)."""
        assert self._runtime is not None and self._runtime.thread_id is not None
        return self._runtime.thread_id

    def _require_active_session(self) -> None:
        """Invariant guard: writes to a persisted thread need its lease held."""
        assert self._runtime is not None
        if self._runtime.lease is None:
            raise RuntimeError("This runner owns no session; start or switch to one first")
        if self._runtime.lease.thread_id != self.thread_id:
            raise RuntimeError("The session lease does not cover the active thread")
        if self._runtime.pending_switch is not None:
            raise RuntimeError("A session switch is in progress")

    def list_models(self) -> list[ModelProfile]:
        if self.settings is None:
            return []
        return list(self.settings.list_profiles())

    def current_model(self) -> ModelProfile | None:
        if self.settings is None:
            return None
        try:
            return self.settings.get_profile(self._current_model_id)
        except KeyError:
            return self.settings.active_profile

    def pending_model(self) -> ModelProfile | None:
        with self._pending_config_lock:
            model_id = self._pending_model_id
        if model_id is None or self.settings is None:
            return None
        try:
            return self.settings.get_profile(model_id)
        except KeyError:
            return None

    def pending_permission_mode(self) -> PermissionMode | None:
        with self._pending_config_lock:
            return self._pending_permission_mode

    def _has_pending_runtime_config(self) -> bool:
        with self._pending_config_lock:
            return (self._pending_model_id is not None or self._pending_permission_mode is not None
                    or self._pending_reasoning_effort is not None)

    def _save_pending_config(self) -> None:
        if self.session_store is not None:
            self.session_store.set_pending_config(
                self.thread_id, model_id=self._pending_model_id,
                reasoning_effort=self._pending_reasoning_effort,
                permission_mode=self._pending_permission_mode.value if self._pending_permission_mode else None,
            )

    def reasoning_effort(self) -> str:
        return self._reasoning_effort

    def pending_reasoning_effort(self) -> str | None:
        with self._pending_config_lock:
            return self._pending_reasoning_effort

    def request_model_change(
        self, id_or_prefix: str, *, reasoning_effort: str | None = None,
    ) -> ModelProfile:
        if self._closed:
            raise RuntimeError("Runner is closed")
        self._require_active_session()
        if self.settings is None:
            raise RuntimeError("Model switching requires Settings with llm.models")
        profile = self.settings.get_profile(id_or_prefix)
        with self._pending_config_lock:
            effort = reasoning_effort if reasoning_effort is not None else (
                self._reasoning_effort if profile.id == self._current_model_id else "default"
            )
            if effort != "default" and effort not in profile.reasoning_efforts:
                raise ValueError(f"Unsupported reasoning effort for {profile.id}: {effort}")
            self._pending_model_id = profile.id if profile.id != self._current_model_id else None
            self._pending_reasoning_effort = effort if (
                self._pending_model_id is not None or effort != self._reasoning_effort
            ) else None
            self._save_pending_config()
        return profile

    def request_permission_change(self, mode: PermissionMode | str) -> PermissionMode:
        if self._closed:
            raise RuntimeError("Runner is closed")
        self._require_active_session()
        if isinstance(mode, str):
            parsed = parse_permission_mode(mode)
            if parsed is None:
                raise ValueError(f"Unknown permission mode: {mode}")
            mode = parsed
        if mode is PermissionMode.ALLOW and not allow_mode_available(self.prepared.execution_mode):
            raise ValueError(allow_mode_unavailable_reason(self.prepared.execution_mode))
        with self._pending_config_lock:
            self._pending_permission_mode = mode if mode is not self._permission_mode else None
            self._save_pending_config()
        return mode

    def _apply_pending_runtime_config(self, handler: RunEventHandler | None = None) -> None:
        failures: list[str] = []
        with self._pending_config_lock:
            model_id = self._pending_model_id
            mode = self._pending_permission_mode
            effort = self._pending_reasoning_effort
            if model_id is None and mode is None and effort is None:
                return
            profile = self.current_model()
            if model_id is not None:
                try:
                    if self.settings is None:
                        raise KeyError(model_id)
                    profile = self.settings.get_profile(model_id)
                    # Pending switches saved before effort support use the new model's default.
                    if effort is None:
                        effort = "default"
                except KeyError:
                    failures.append(f"Pending model {model_id} is unavailable")
                    model_id = None
                    effort = None
            if effort is not None and effort != "default" and (
                profile is None or effort not in profile.reasoning_efforts
            ):
                failures.append(f"Pending reasoning effort {effort} is unavailable; using default")
                effort = "default"
            if mode is PermissionMode.ALLOW and not allow_mode_available(self.prepared.execution_mode):
                failures.append(allow_mode_unavailable_reason(self.prepared.execution_mode))
                mode = None
            previous = self.prepared, self._chat_model
            try:
                if model_id is not None or mode is not None or effort is not None:
                    target_mode = mode or self._permission_mode
                    target_effort = effort if effort is not None else self._reasoning_effort
                    chat = self._chat_model
                    if profile is not None and (model_id is not None or effort is not None):
                        chat = build_chat_model(
                            profile, attachment_store=self.attachment_store,
                            reasoning_effort=None if target_effort == "default" else target_effort,
                        )
                    self._rebuild_prepared(model=chat, permission_mode=target_mode)
                    if self.session_store is not None:
                        self.session_store.touch(
                            self.thread_id, model_id=profile.id if model_id is not None else self._current_model_id,
                            permission_mode=target_mode.value, reasoning_effort=target_effort,
                        )
            except Exception as exc:
                self.prepared, self._chat_model = previous
                failures.append(str(exc))
                model_id = None
                mode = None
                effort = None
            if model_id is not None:
                self._context_usage_invalidated = True
                self._current_model_id = profile.id
            if mode is not None:
                self._permission_mode = mode
            if effort is not None:
                self._reasoning_effort = effort
            self._pending_model_id = None
            self._pending_permission_mode = None
            self._pending_reasoning_effort = None
            self._save_pending_config()
        for failure in failures:
            _emit(handler or self.on_event, RunEvent(type="runtime_config_failed", content=failure, is_error=True))
        if model_id is not None or mode is not None or effort is not None:
            _emit(handler or self.on_event, RunEvent(
                type="runtime_config_applied",
                result={"model_id": self._current_model_id, "permission_mode": self._permission_mode.value,
                        "reasoning_effort": self._reasoning_effort, "reasoning_changed": effort is not None,
                        "model_changed": model_id is not None, "permission_changed": mode is not None},
            ))

    def supports_input(self, kind: InputKind) -> bool:
        profile = self.current_model()
        return kind == "text" if profile is None else profile.supports_input(kind)

    def supports_next_input(self, kind: InputKind) -> bool:
        """Capability of the model the next inference will use, pending switch included."""
        profile = self.pending_model() or self.current_model()
        return kind == "text" if profile is None else profile.supports_input(kind)

    def context_window(self) -> int:
        """Configured context window in tokens; 0 when the profile does not declare one."""
        profile = self.current_model()
        return 0 if profile is None else profile.context_window

    def latest_usage(self) -> dict[str, int]:
        """Token usage of the most recent model call still held in the checkpoint."""
        try:
            state = self.prepared.graph.get_state(self._thread_config())
            if self._context_usage_invalidated:
                return {}
            values = state.values or {}
            messages = list(values.get("messages", []) or [])
            middleware = self.prepared.compact_middleware
            if middleware is not None:
                messages = middleware._summarization._apply_event_to_messages(
                    messages, values.get("_summarization_event"),
                )
            last = next((item for item in reversed(messages) if isinstance(item, AIMessage)), None)
            if last is None:
                return {}
            model_id = last.response_metadata.get("deep_agent_model_id")
            if model_id != self._current_model_id:
                # Legacy checkpoints remain readable; without a source ID their
                # usage cannot be safely attributed after a model/source switch.
                return {}
            if self.session_store is not None:
                saved = self.session_store.get(self.thread_id)
                if saved and saved.last_run_status in {StopReason.ABORTED, StopReason.ERROR}:
                    return {}
            return _usage_from_messages(messages)
        except Exception:  # noqa: BLE001
            # 恢复时压缩中间件私有接口或存储读取失败，保守返回 Unknown。
            return {}

    @_exclusive_operation
    def compact_context(self) -> CompactResult:
        """Run Deep Agents' compact tool in the graph, then close its tool turn."""
        self._require_active_session()
        self._require_empty_input_queue()
        middleware = self.prepared.compact_middleware
        if middleware is None:
            raise RuntimeError("Manual compaction middleware is unavailable")
        config = self._thread_config()
        state = self.prepared.graph.get_state(config)
        if getattr(state, "next", ()) or self.current_interrupt() is not None:
            raise RuntimeError("Finish the pending interaction before compacting")
        values = state.values or {}
        messages = list(values.get("messages", []) or [])
        effective = middleware._summarization._apply_event_to_messages(
            messages, values.get("_summarization_event"),
        )
        usage = _usage_from_messages(effective)
        used = usage.get("input_tokens")
        window = self.context_window()
        if not window and self.prepared.model is not None:
            profile = self.prepared.model.profile
            if isinstance(profile, dict) and isinstance(profile.get("max_input_tokens"), int):
                window = profile["max_input_tokens"]
        if not middleware._is_eligible_for_compaction(effective):
            return CompactResult("ineligible", used, window)
        if not middleware._summarization._determine_cutoff_index(effective):
            return CompactResult("nothing_to_compact", used, window)

        # The tool must execute inside LangGraph so StateBackend can archive old
        # messages. A synthetic tool call enters the tools node; interrupting
        # after that node avoids an unnecessary ordinary model response.
        call_id = f"manual-compact-{uuid4()}"
        last_ai = next((item for item in reversed(effective) if isinstance(item, AIMessage)), None)
        metadata = dict(last_ai.response_metadata) if last_ai is not None else {}
        self.prepared.graph.update_state(config, {"messages": [AIMessage(
            content="",
            tool_calls=[{"name": "compact_conversation", "args": {}, "id": call_id}],
            usage_metadata=usage or None,
            response_metadata=metadata,
        )]}, as_node="model")
        self.prepared.graph.invoke(None, config, interrupt_after=["tools"])
        after = self.prepared.graph.get_state(config)
        tool_result = next((
            item for item in reversed((after.values or {}).get("messages", []) or [])
            if isinstance(item, ToolMessage) and item.tool_call_id == call_id
        ), None)
        message = str(tool_result.content) if tool_result is not None else ""
        status = "compacted" if message.startswith("Conversation compacted.") else "failed"
        # Supply a terminal assistant turn, then let after-model hooks finish
        # without entering the model node again.
        self.prepared.graph.update_state(
            config, {"messages": [AIMessage(
                content="", additional_kwargs={"manual_compact_completed": True} if status == "compacted" else {},
            )]}, as_node="model",
        )
        self.prepared.graph.invoke(None, config, interrupt_before=["model"])
        if self.prepared.graph.get_state(config).next:
            raise RuntimeError("Compaction left the agent with a pending graph step")
        if tool_result is None:
            raise RuntimeError("Compaction tool did not return a result")
        if status == "compacted":
            self._context_usage_invalidated = True
        if status == "compacted" and self.session_store is not None:
            self.session_store.touch(self.thread_id, last_run_status=StopReason.STOP)
        return CompactResult(status, used, window, message)

    def switch_model(self, id_or_prefix: str) -> ModelProfile:
        """Deferred model switch, applied at the next safe boundary."""
        return self.request_model_change(id_or_prefix)

    def permission_mode(self) -> PermissionMode:
        return self._permission_mode

    def set_permission_mode(self, mode: PermissionMode | str) -> PermissionMode:
        """Deferred permission switch, applied at the next safe boundary."""
        return self.request_permission_change(mode)

    def _rebuild_prepared(
        self,
        *,
        model: BaseChatModel | None = None,
        permission_mode: PermissionMode | None = None,
    ) -> None:
        mode = permission_mode or self._permission_mode
        chat = model if model is not None else self._chat_model
        if chat is None and self.settings is not None:
            chat = build_chat_model(
                self.settings.get_profile(self._current_model_id),
                reasoning_effort=None if self._reasoning_effort == "default" else self._reasoning_effort,
                attachment_store=self.attachment_store,
            )
        self.prepared = build_agent(
            self._spec, chat, mode, self._checkpointer,
            self.control, self._pause_condition,
            interrupt_on_override=self._custom_interrupt_on if mode is PermissionMode.ASK else None,
            backend_selection=self.prepared.backend_selection,
        )
        self._chat_model = chat

    @property
    def sandbox_pool(self) -> SandboxPool | None:
        """本 Runner 的 Agent 会话所持有的持久沙箱。"""
        return self.prepared.sandbox_pool

    def _end_sandbox_session(self, reason: str) -> None:
        pool = self.sandbox_pool
        if pool is not None:
            pool.shutdown(reason)

    def request_pause(self) -> None:
        self.control.request_pause()

    def request_cancel(self) -> None:
        """Request hard cancel for execute / cancellable tools and drain the graph."""
        self.control.cancel()
        if self._busy:
            self._touch_status(StopReason.ABORTED)

    def steer(self, text: str) -> None:
        # No _exclusive_operation: steering must enter while a run holds the
        # operation lock, but never while the runner is detached or closed.
        if self._closed:
            raise RuntimeError("Runner is closed")
        self._require_active_session()
        self.control.steer(text)

    def follow_up(self, text: str) -> None:
        if self._closed:
            raise RuntimeError("Runner is closed")
        self._require_active_session()
        self.control.follow_up(text)

    def take_unapplied_messages(self) -> list[str]:
        return [item.text for item in self.control.take_unapplied()]

    def list_sessions(self, *, limit: int = 50) -> list[SessionInfo]:
        assert self._runtime is not None
        return self._runtime.list_sessions(limit=limit)

    def thread_has_content(self, thread_id: str | None = None) -> bool:
        tid = thread_id or self.thread_id
        if self.session_store is not None:
            assert self._runtime is not None
            return self._runtime.thread_has_checkpoint(tid)
        try:
            state = self.prepared.graph.get_state({"configurable": {"thread_id": tid}})
        except Exception:  # noqa: BLE001
            return False
        messages = list((state.values or {}).get("messages", []) or [])
        return any(bool(getattr(message, "content", None) or getattr(message, "tool_calls", None)) for message in messages)

    def load_session(self, session_id: str) -> SessionSnapshot | None:
        assert self._runtime is not None
        info = self._runtime.resolve(session_id)
        if info is None:
            return None
        config = {"configurable": {"thread_id": info.id}}
        try:
            state = self.prepared.graph.get_state(config)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed to load session {info.id}: {exc}") from exc
        messages = list((state.values or {}).get("messages", []) or [])
        interrupt = interrupt_kind_from_state(self.prepared.graph, config)
        transcript = messages_to_transcript(messages)
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
            todos=list((state.values or {}).get("todos", []) or []),
            interrupt_kind=interrupt.kind if interrupt is not None else None,
            human_input=interrupt.payload if interrupt is not None and interrupt.kind is InterruptKind.WAITING_HUMAN else {},
            pending_tool_calls=list(interrupt.pending_tools) if interrupt is not None else [],
        )

    @_exclusive_operation
    def switch_session(self, session_id: str) -> SessionSnapshot:
        """Switch synchronously; a busy target raises instead of detaching."""
        result = self._begin_session_switch(session_id, detach_on_busy=False)
        if result.status == "busy":
            raise SessionLockBusyError(
                f"Session {result.target_id} is already open in another window"
            )
        assert result.snapshot is not None
        return result.snapshot

    @_exclusive_operation
    def begin_session_switch(self, session_id: str) -> SessionSwitchResult:
        """Phase one of a switch: either finish immediately or detach to wait."""
        return self._begin_session_switch(session_id, detach_on_busy=True)

    @_exclusive_operation
    def complete_session_switch(
        self,
        session_id: str,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> SessionSnapshot | None:
        """Phase two: wait for a busy target, then load it and take ownership.

        Returns None when the wait was cancelled or superseded; the runner stays
        detached (no lease) in that case.
        """
        assert self._runtime is not None
        if self._runtime.pending_switch != session_id or self._runtime.lease is not None:
            return None
        lease = self._runtime.wait_acquire(session_id, cancelled=cancelled)
        if lease is None:
            return None
        if (
            self._closed
            or self._runtime.pending_switch != session_id
            or self._runtime.lease is not None
            or (cancelled is not None and cancelled())
        ):
            lease.release()
            return None
        info = self._runtime.get(session_id)
        if info is None:
            self._runtime.clear_pending()
            lease.release()
            raise KeyError(f"Unknown session: {session_id}")
        return self._load_and_commit_switch(info, lease)

    def _begin_session_switch(self, session_id: str, *, detach_on_busy: bool) -> SessionSwitchResult:
        assert self._runtime is not None
        info = self._runtime.resolve(session_id)
        if info is None:
            raise KeyError(f"Unknown session: {session_id}")
        if info.id != self.thread_id:
            self._require_empty_input_queue()
        self._runtime.clear_pending()
        # A→A with the lease still held only reloads the snapshot; every other
        # target is acquired first so the old thread never sees a lock gap.
        already_owned = self._runtime.owns(info.id)
        lease = None if already_owned else self._runtime.try_acquire(info.id)
        if lease is None and not already_owned:
            if not detach_on_busy:
                return SessionSwitchResult(status="busy", target_id=info.id)
            # The old thread must accept no further operations while waiting.
            self._runtime.detach_for_wait(info.id)
            self._end_sandbox_session("session switched")
            return SessionSwitchResult(status="busy", target_id=info.id)
        snapshot = self._load_and_commit_switch(info, lease)
        return SessionSwitchResult(status="switched", target_id=info.id, snapshot=snapshot)

    def _load_and_commit_switch(
        self, info: SessionInfo, lease: SessionLeaseLike | None,
    ) -> SessionSnapshot:
        """Prepare everything, then commit the switch as the final step.

        All fallible graph / catalog / checkpointer reads happen while the old
        session is still held and before the target row is written, so a
        failure rolls back cleanly and never dirties the target's catalog row.
        ``touch`` is the only fallible operation left, and ``adopt`` — followed
        by in-memory updates only — is the commit point.
        """
        assert self._runtime is not None
        previous = (self._chat_model, self._current_model_id, self._permission_mode,
                    self._pending_model_id, self._pending_permission_mode,
                    self._reasoning_effort, self._pending_reasoning_effort)
        plan = self._runtime.restore_plan(info, execution_mode=self.prepared.execution_mode)
        try:
            snapshot = self.load_session(info.id)
            if snapshot is None:
                raise KeyError(f"Unknown session: {info.id}")
            self._apply_restore_plan(plan)
            has_checkpoint = self._runtime.thread_has_checkpoint(info.id)
            recovery = self._runtime.recovery_context(
                info,
                has_checkpoint=has_checkpoint,
                interrupt_active=snapshot.interrupt_kind is not None,
            )
            refreshed = self._runtime.get(info.id)
            self.session_store.touch(
                info.id, model_id=self._current_model_id,
                permission_mode=self._permission_mode.value, reasoning_effort=self._reasoning_effort,
            )
        except Exception:
            old_model, old_id, old_mode, old_pending_model, old_pending_mode, old_effort, old_pending_effort = previous
            self._rebuild_prepared(model=old_model, permission_mode=old_mode)
            self._current_model_id = old_id
            self._permission_mode = old_mode
            self._pending_model_id = old_pending_model
            self._pending_permission_mode = old_pending_mode
            self._reasoning_effort = old_effort
            self._pending_reasoning_effort = old_pending_effort
            if lease is not None:
                lease.release()
            raise
        if refreshed is not None:
            snapshot.info = refreshed
        if snapshot.info.id != self._runtime.thread_id or lease is not None:
            self._end_sandbox_session("session switched")
        if lease is not None:
            self._runtime.adopt(snapshot.info.id, lease)
        else:
            self._runtime.bind(snapshot.info.id)
        self._pending_reasoning_effort = info.pending_reasoning_effort
        self._context_usage_invalidated = False
        self._pending_model_id = info.pending_model_id
        self._pending_permission_mode = parse_permission_mode(info.pending_permission_mode or "")
        if self._pending_model_id and self.settings is not None:
            try:
                self.settings.get_profile(self._pending_model_id)
            except KeyError:
                snapshot.notices.append(f"Pending model {self._pending_model_id} is unavailable; switch cancelled.")
                self._pending_model_id = None
                self._pending_reasoning_effort = None
        if self._pending_permission_mode is PermissionMode.ALLOW and not allow_mode_available(self.prepared.execution_mode):
            snapshot.notices.append("Pending allow permission is unavailable here; switch cancelled.")
            self._pending_permission_mode = None
        self._save_pending_config()
        self.control.clear_pause()
        self.control.set_defer_steering(bool(snapshot.interrupt_kind))
        snapshot.notices.extend(plan.notices)
        self._resume_context = recovery
        return snapshot

    def _apply_restore_plan(self, plan: RestorePlan) -> None:
        if self.settings is not None and plan.model_id and (
            plan.model_id != self._current_model_id or plan.reasoning_effort != self._reasoning_effort
        ):
            profile = self.settings.get_profile(plan.model_id)
            self._rebuild_prepared(
                model=build_chat_model(
                    profile, attachment_store=self.attachment_store,
                    reasoning_effort=None if plan.reasoning_effort == "default" else plan.reasoning_effort,
                ),
                permission_mode=plan.permission_mode,
            )
            self._current_model_id = profile.id
        elif plan.permission_mode is not self._permission_mode:
            self._rebuild_prepared(permission_mode=plan.permission_mode)
        self._reasoning_effort = plan.reasoning_effort
        self._permission_mode = plan.permission_mode

    @_exclusive_operation
    def new_session(self, *, title: str = "") -> SessionInfo:
        assert self._runtime is not None
        self._require_empty_input_queue()
        self._resume_context = None
        self.control.clear_pause()
        self.control.set_defer_steering(False)
        if self._reasoning_effort != "default" and self.settings is not None:
            self._apply_restore_plan(RestorePlan(
                model_id=self._current_model_id, permission_mode=self._permission_mode,
            ))
        self._runtime.clear_pending()
        self._pending_reasoning_effort = None
        self._pending_model_id = None
        self._pending_permission_mode = None
        self._end_sandbox_session("new session started")
        return self._runtime.create_new(
            title=title, model_id=self._current_model_id,
            permission_mode=self._permission_mode.value,
        )

    def close(self) -> None:
        """Forbid new operations, drain an active run, then drop the lease.

        The lease is never released while a run can still write the thread: a
        close that times out keeps the lease (and the temporary attachment
        store) and raises, so no other window can take the thread mid-write.
        The lock then either drops when this process exits or when close() is
        retried after the run finally drained.
        """
        assert self._runtime is not None
        if self._closed and self._close_completed:
            return
        self._closed = True
        self._runtime.abort_wait()
        if self._busy:
            self.request_cancel()
        drained = self._operation_lock.acquire(timeout=CLOSE_DRAIN_SECONDS)
        if drained:
            self._operation_lock.release()
        # 卡住的命令不得在 close 之后继续占用沙箱，即便租约必须保留。
        self._end_sandbox_session("agent session closed")
        if not drained:
            raise RuntimeError(
                "Runner close timed out with a run still active; the session "
                "lease is kept until the run drains and close is retried"
            )
        self._close_completed = True
        self._runtime.close()
        if self._attachment_tempdir is not None:
            self._attachment_tempdir.cleanup()
            self._attachment_tempdir = None

    def _require_empty_input_queue(self) -> None:
        if self.control.pending_steering_count() or self.control.pending_follow_up_count():
            raise RuntimeError("Unapplied input belongs to the current session; reclaim it before switching")

    @_exclusive_operation
    def invoke(
        self,
        text: str,
        *,
        images: Sequence[ImageAttachment] = (),
        on_delta: DeltaHandler | None = None,
        on_event: RunEventHandler | None = None,
    ) -> RunResult:
        if images and not self.supports_next_input("image"):
            raise ValueError("The current model does not declare image input support")
        if len(images) > MAX_IMAGES_PER_MESSAGE:
            raise ValueError(f"A message can contain at most {MAX_IMAGES_PER_MESSAGE} images")
        if images and not self._can_materialize_attachments():
            raise RuntimeError(
                "The current custom model does not implement Deep-Agent image attachment resolution"
            )
        refs = tuple(self.attachment_store.put(image) for image in images)
        return self._invoke_with_attachment_refs(
            text,
            image_refs=refs,
            on_delta=on_delta,
            on_event=on_event,
        )

    @_exclusive_operation
    def invoke_with_attachment_refs(
        self,
        text: str,
        *,
        image_refs: Sequence[ImageAttachmentRef] = (),
        on_delta: DeltaHandler | None = None,
        on_event: RunEventHandler | None = None,
    ) -> RunResult:
        return self._invoke_with_attachment_refs(
            text, image_refs=image_refs, on_delta=on_delta, on_event=on_event,
        )

    def _invoke_with_attachment_refs(
        self,
        text: str,
        *,
        image_refs: Sequence[ImageAttachmentRef] = (),
        on_delta: DeltaHandler | None = None,
        on_event: RunEventHandler | None = None,
    ) -> RunResult:
        self._require_active_session()
        current_interrupt = self.current_interrupt()
        if current_interrupt is not None and current_interrupt.kind is InterruptKind.RUNTIME_CONFIG_BOUNDARY:
            raise RuntimeError("Pending runtime config recovery must resume before new input")
        if current_interrupt is None:
            self._apply_pending_runtime_config(on_event)
        refs = tuple(image_refs)
        if refs and not self.supports_next_input("image"):
            raise ValueError("The current model does not declare image input support")
        if len(refs) > MAX_IMAGES_PER_MESSAGE:
            raise ValueError(f"A message can contain at most {MAX_IMAGES_PER_MESSAGE} images")
        if refs and not self._can_materialize_attachments():
            raise RuntimeError(
                "The current custom model does not implement Deep-Agent image attachment resolution"
            )
        additional = {ATTACHMENT_META_KEY: refs_to_dicts(refs)} if refs else {}
        graph_input: dict[str, Any] = {
            "messages": [HumanMessage(content=text, additional_kwargs=additional)],
        }
        if self._resume_context is not None:
            self._resume_context.armed = True
        if self.session_store is not None:
            self._runtime.persist_for_message(
                model_id=self._current_model_id, permission_mode=self._permission_mode.value,
                reasoning_effort=self._reasoning_effort,
            )
            title = text.strip().splitlines()[0][:80] if text.strip() else None
            self.session_store.touch(self.thread_id, title=title,
                                     last_run_status=StopReason.PENDING)
        return self._stream(graph_input, on_delta=on_delta, on_event=on_event)

    def _can_materialize_attachments(self) -> bool:
        return isinstance(self._chat_model, ChatOpenAI) or bool(
            getattr(self._chat_model, "materializes_attachment_refs", False)
        )

    def store_image(self, image: ImageAttachment) -> ImageAttachmentRef:
        if not self.supports_next_input("image"):
            raise ValueError("The current model does not declare image input support")
        return self.attachment_store.put(image)

    @_exclusive_operation
    def cleanup_attachments(
        self,
        *,
        protected: Sequence[ImageAttachmentRef] = (),
    ) -> AttachmentCleanupResult:
        if self.session_store is None:
            return self.attachment_store.cleanup(
                set(), protected_storage_keys=(ref.storage_key for ref in protected),
            )
        return self.session_store.cleanup_attachments(
            protected=tuple(protected), release_runtime_lease=True,
        )

    @_exclusive_operation
    def continue_run(
        self,
        *,
        on_delta: DeltaHandler | None = None,
        on_event: RunEventHandler | None = None,
    ) -> RunResult:
        self._require_active_session()
        self._require_interrupt(InterruptKind.PAUSED, "continue_run")
        self.control.clear_pause()
        return self._resume(True, on_delta=on_delta, on_event=on_event)

    @_exclusive_operation
    def approve_tool(
        self,
        tool_call_id: str,
        *,
        on_delta: DeltaHandler | None = None,
        on_event: RunEventHandler | None = None,
    ) -> RunResult:
        self._require_active_session()
        state = self._require_interrupt(InterruptKind.WAITING_CONFIRMATION, "approve_tool")
        return self._resume(
            _tool_decisions(list(state.pending_tools), decision_type="approve", tool_call_ids=[tool_call_id]),
            on_delta=on_delta,
            on_event=on_event,
        )

    @_exclusive_operation
    def reject_tool(
        self,
        tool_call_id: str,
        message: str | None = None,
        *,
        on_delta: DeltaHandler | None = None,
        on_event: RunEventHandler | None = None,
    ) -> RunResult:
        self._require_active_session()
        state = self._require_interrupt(InterruptKind.WAITING_CONFIRMATION, "reject_tool")
        return self._resume(
            _tool_decisions(
                list(state.pending_tools), decision_type="reject", tool_call_ids=[tool_call_id], message=message,
            ),
            on_delta=on_delta,
            on_event=on_event,
        )

    @_exclusive_operation
    def submit_human_input(
        self,
        values: dict[str, Any],
        *,
        on_delta: DeltaHandler | None = None,
        on_event: RunEventHandler | None = None,
    ) -> RunResult:
        self._require_active_session()
        state = self._require_interrupt(InterruptKind.WAITING_HUMAN, "submit_human_input")
        if _has_pending_legacy_handoff(self.prepared.graph, self._thread_config()):
            raise RuntimeError(
                "This session uses the removed handoff_to_human tool. "
                "Start a new session and ask again with request_human_input."
            )
        if not isinstance(values, dict):
            raise ValueError("Human input response must contain a values object")
        envelope: dict[str, Any] = {"type": "human_input", "values": dict(values)}
        if state.payload.get("interactionId"):
            envelope["interactionId"] = state.payload["interactionId"]
        return self._resume(envelope, on_delta=on_delta, on_event=on_event)

    @_exclusive_operation
    def _decide_listed_tools(  # HITL adapter: one Command must carry the full decisions list.
        self,
        tool_call_ids: Sequence[str],
        *,
        approved: bool,
        message: str | None = None,
        on_delta: DeltaHandler | None = None,
        on_event: RunEventHandler | None = None,
    ) -> RunResult:
        action = "approve_tool" if approved else "reject_tool"
        state = self._require_interrupt(InterruptKind.WAITING_CONFIRMATION, action)
        return self._resume(
            _tool_decisions(
                list(state.pending_tools),
                decision_type="approve" if approved else "reject",
                tool_call_ids=tool_call_ids,
                message=message,
                others="same",
            ),
            on_delta=on_delta,
            on_event=on_event,
        )

    def _require_interrupt(self, expected: InterruptKind, action: str) -> InterruptState:
        state = self.current_interrupt()
        if state is None or state.kind is not expected:
            current = state.kind.value if state is not None else "none"
            raise ValueError(f"{action} requires a {expected.value} interrupt, current is {current}")
        return state

    def _resume(
        self,
        value: Any,
        *,
        on_delta: DeltaHandler | None = None,
        on_event: RunEventHandler | None = None,
    ) -> RunResult:
        self._require_active_session()
        self.control.set_defer_steering(False)
        if self.session_store is not None:
            self.session_store.touch(self.thread_id, last_run_status=StopReason.PENDING)
        return self._stream(Command(resume=value), on_delta=on_delta, on_event=on_event)

    @_exclusive_operation
    def resume_runtime_config(
        self, *, on_delta: DeltaHandler | None = None, on_event: RunEventHandler | None = None,
    ) -> RunResult:
        self._require_active_session()
        self._require_interrupt(InterruptKind.RUNTIME_CONFIG_BOUNDARY, "resume_runtime_config")
        self._apply_pending_runtime_config(on_event)
        return self._resume(True, on_delta=on_delta, on_event=on_event)

    def current_interrupt(self) -> InterruptState | None:
        return interrupt_kind_from_state(self.prepared.graph, self._thread_config())

    def _thread_config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self.thread_id}}

    def _run_config(
        self,
        on_delta: DeltaHandler | None,
        on_event: RunEventHandler | None,
    ) -> dict[str, Any]:
        handler = on_delta or self.on_delta
        event_handler = on_event or self.on_event
        config = self._thread_config()
        def stream_usage(usage: dict[str, int]) -> None:
            if not usage:
                self._context_usage_invalidated = True
            elif "input_tokens" in usage:
                self._context_usage_invalidated = False
            _emit(event_handler, RunEvent(type="usage", result=usage))

        callback = StreamDeltaCallback(
            handler or (lambda _kind, _text: None),
            on_usage=stream_usage,
            on_compaction=(
                (lambda active: _emit(event_handler, RunEvent(
                    type="compaction_started" if active else "compaction_finished",
                ))) if event_handler else None
            ),
            on_reasoning=(
                (lambda text: _emit(event_handler, RunEvent(type="thinking_delta", content=text)))
                if event_handler else None
            ),
            on_assistant=(
                (lambda text: _emit(event_handler, RunEvent(type="assistant_delta", content=text)))
                if event_handler else None
            ),
            on_start=(
                (lambda: _emit(event_handler, RunEvent(type="assistant_started")))
                if event_handler else None
            ),
            on_end=(
                (lambda assistant, reasoning: _emit(event_handler, RunEvent(
                    type="assistant_completed", content=assistant, result={"thinking": reasoning},
                )))
                if event_handler else None
            ),
        )
        return merge_stream_callbacks(config, callback)

    def _stream(
        self,
        graph_input: Any,
        *,
        on_delta: DeltaHandler | None = None,
        on_event: RunEventHandler | None = None,
    ) -> RunResult:
        event_handler = on_event or self.on_event
        self._event_handler = event_handler
        config = self._run_config(on_delta, on_event)
        self._seen_tool_calls.clear()
        self._todo_call_ids.clear()
        run_control = self.control.begin_run()
        set_output_emitter(self._emit_tool_output)
        attachment_token = set_attachment_store(self.attachment_store)
        recovery_token = set_recovery_context(self._resume_context)
        self._busy = True
        self._context_usage_invalidated = True
        _emit(event_handler, RunEvent(type="run_started"))

        result: RunResult | None = None
        turn_started = time.monotonic()

        def emit_turn_completed(output: str) -> None:
            _emit(event_handler, RunEvent(
                type="turn_completed",
                content=output,
                result=TurnTiming(
                    elapsed_seconds=max(0.0, time.monotonic() - turn_started),
                    finished_at=datetime.now(timezone.utc),
                ),
            ))

        try:
            result = self._stream_once(graph_input, config, run_control, event_handler)
            while result.status == "runtime_config_boundary":
                self._apply_pending_runtime_config(event_handler)
                result = self._stream_once(Command(resume=True), config, run_control, event_handler)
            # Consume queued input at the same run boundary for every client.
            while result.status == "completed" and not self.control.cancel_requested:
                text = self.control.pop_steering()
                if text is None:
                    text = self.control.pop_follow_up()
                if text is None:
                    break
                emit_turn_completed(result.output)
                run_control = self.control.begin_run()
                self._seen_tool_calls.clear()
                self._todo_call_ids.clear()
                turn_started = time.monotonic()
                self._apply_pending_runtime_config(event_handler)
                result = self._stream_once(
                    {"messages": [HumanMessage(content=text)]},
                    config,
                    run_control,
                    event_handler,
                )
                while result.status == "runtime_config_boundary":
                    self._apply_pending_runtime_config(event_handler)
                    result = self._stream_once(Command(resume=True), config, run_control, event_handler)
            if result.status == "completed":
                _emit(event_handler, RunEvent(type="run_completed", content=result.output))
                emit_turn_completed(result.output)
                self._touch_status(StopReason.STOP)
            return result
        finally:
            self._busy = False
            self.control.end_run()
            set_output_emitter(None)
            reset_attachment_store(attachment_token)
            reset_recovery_context(recovery_token)
            if (result is not None and result.status == "completed") or (
                self._resume_context is not None and self._resume_context.text is None
            ):
                self._resume_context = None
            self._event_handler = None

    def _stream_once(
        self,
        graph_input: Any,
        config: dict[str, Any],
        run_control: Any,
        event_handler: RunEventHandler | None,
    ) -> RunResult:
        interrupted = ""
        pending: list[dict[str, Any]] = []
        human_input: dict[str, Any] = {}
        previous_todos = _todos_in_state(
            self.prepared.graph.get_state(self._thread_config()).values or {}
        )
        try:
            for chunk in self.prepared.graph.stream(
                graph_input, config, stream_mode=["updates", "values"], control=run_control,
                durability="sync",
            ):
                mode, payload = chunk
                if mode == "values":
                    todos = _todos_in_state(payload)
                    if todos is not None and todos != previous_todos:
                        _emit(event_handler, RunEvent(type="todos_updated", result=todos))
                        previous_todos = todos
                    continue
                chunk = payload
                self._emit_update_events(chunk, event_handler)
                if not isinstance(chunk, dict):
                    continue
                for node, update in chunk.items():
                    if node != "__interrupt__":
                        continue
                    if any(item.get("type") == "runtime_config_boundary" for item in interrupt_payloads(update)):
                        return RunResult(status="runtime_config_boundary")
                    interrupt = classify_interrupt(
                        update, self.prepared.graph, self._thread_config(),
                    )
                    interrupted = interrupt.kind.value
                    pending = list(interrupt.pending_tools)
                    if interrupt.kind is InterruptKind.WAITING_HUMAN:
                        human_input = interrupt.payload
        except GraphDrained:
            _emit(event_handler, RunEvent(type="run_cancelled"))
            self._touch_status(StopReason.ABORTED)
            return RunResult(status="cancelled")
        except Exception as exc:  # noqa: BLE001
            if self.control.cancel_requested:
                _emit(event_handler, RunEvent(type="run_cancelled"))
                self._touch_status(StopReason.ABORTED)
                return RunResult(status="cancelled")
            _emit(event_handler, RunEvent(type="run_failed", content=str(exc), is_error=True))
            self._touch_status(StopReason.ERROR)
            return RunResult(status="failed", error=str(exc))

        if self.control.cancel_requested and not interrupted:
            _emit(event_handler, RunEvent(type="run_cancelled"))
            self._touch_status(StopReason.ABORTED)
            return RunResult(status="cancelled")

        if interrupted:
            self.control.set_defer_steering(True)
            self._touch_status(StopReason.DEFERRED)
            result = RunResult(
                status=interrupted,
                pending_tool_calls=pending,
                human_input=human_input,
            )
            _emit(event_handler, RunEvent(type="interaction_requested", result=result))
            return result

        output = _final_output(self.prepared.graph, self._thread_config())
        return RunResult(status="completed", output=output)

    def _emit_tool_output(self, tool_call_id: str, content: str, stream: str) -> None:
        _emit(self._event_handler or self.on_event, RunEvent(
            type="tool_output_delta",
            tool_call_id=tool_call_id,
            content=content,
            stream=stream,
        ))

    def _on_control_event(self, event_type: str, payload: dict[str, Any]) -> None:
        _emit(self._event_handler or self.on_event, RunEvent(
            type=event_type,
            content=str(payload.get("content") or ""),
            result=payload,
        ))

    def _touch_status(self, reason: StopReason) -> None:
        if self.session_store is not None:
            self.session_store.touch(self.thread_id, last_run_status=reason)
        if reason in {StopReason.ABORTED, StopReason.ERROR}:
            self._context_usage_invalidated = True
            self._resume_context = RecoveryContext.for_stop_reason(reason)

    def _emit_update_events(self, chunk: Any, handler: RunEventHandler | None) -> None:
        for message in _messages_in_update(chunk):
            if isinstance(message, AIMessage):
                message_id = str(getattr(message, "id", "") or "")
                usage = _usage_metadata_dict(message)
                # 图更新可能省略 Usage，不能覆盖同次调用已收到的流式输入用量。
                if "input_tokens" in usage:
                    self._context_usage_invalidated = False
                    _emit(handler, RunEvent(type="usage", result=usage))
                for call in message.tool_calls or []:
                    tool_call_id = str(call.get("id") or "")
                    if call.get("name") == "write_todos":
                        self._todo_call_ids.add(tool_call_id)
                        continue
                    identity = tool_call_id or f"{call.get('name')}:{id(call)}"
                    if identity in self._seen_tool_calls:
                        continue
                    self._seen_tool_calls.add(identity)
                    _emit(handler, RunEvent(
                        type="tool_started",
                        message_id=message_id,
                        tool_call_id=tool_call_id,
                        name=str(call.get("name") or "tool"),
                        arguments=call.get("args") if isinstance(call.get("args"), dict) else {},
                    ))
            elif isinstance(message, ToolMessage):
                tool_name = str(getattr(message, "name", "") or "tool")
                if tool_name == "write_todos" or str(
                    getattr(message, "tool_call_id", "") or ""
                ) in self._todo_call_ids:
                    continue
                content = _message_text(message)
                artifact = getattr(message, "artifact", None)
                is_error = tool_message_is_error(message)
                _emit(handler, RunEvent(
                    type="tool_completed",
                    tool_call_id=str(getattr(message, "tool_call_id", "") or ""),
                    name=tool_name,
                    content=content,
                    result=getattr(message, "content", content),
                    is_error=is_error,
                    artifact=artifact,
                ))

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


def _todos_in_state(value: Any) -> list[dict[str, str]] | None:
    """Project middleware-owned todos from a LangGraph values snapshot."""
    if isinstance(value, dict) and isinstance(value.get("todos"), list):
        return [dict(item) for item in value["todos"] if isinstance(item, dict)]
    return None


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


def _has_pending_legacy_handoff(graph: Any, config: dict[str, Any]) -> bool:
    state = graph.get_state(config)
    if not getattr(state, "interrupts", ()):
        return False
    for message in reversed((state.values or {}).get("messages", []) or []):
        if isinstance(message, AIMessage):
            return any(call.get("name") == "handoff_to_human" for call in message.tool_calls or [])
    return False


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
