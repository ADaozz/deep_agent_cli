# Stream the compiled graph and resume with Command.
# Interrupt classification and terminal RunResult / RunEvent construction live in run_events.
# Token-level reasoning/assistant deltas use LangChain callbacks (ChatOpenAI streaming=True).
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass, field, replace
from functools import wraps
from pathlib import Path
import os
import tempfile
import time
from threading import Lock
from typing import Any, Callable, Sequence
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
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
from agent.config import InputKind, ModelProfile, SandboxConfig, SandboxMode, Settings, parse_sandbox_mode
from agent.control import RunController
from agent.environment import (
    EnvironmentCache, bubblewrap_backend, probe_sandbox, reset_environment_cache, set_environment_cache,
)
from agent.factory import AgentSpec, PreparedAgent, build_agent
from agent.sandbox import WorkspaceCompositeBackend
from agent.sandbox_pool import SandboxPool
from agent.llm import build_chat_model
from agent.llm_stream import reset_stream_owner, set_stream_owner
from agent.middleware.attachments import reset_attachment_store, set_attachment_store
from agent.middleware.recovery import RecoveryContext, reset_recovery_context, set_recovery_context
from agent.run_events import (
    CompactResult,
    InterruptKind,
    InterruptState,
    RunEvent,
    RunEventHandler,
    RunResult,
    TurnTiming,
    UnknownInterruptError as UnknownInterruptError,
    _emit,
    _final_output,
    _last_message_id,
    _tool_decisions,
    _usage_from_messages,
    cancelled_result,
    classify_interrupt,
    completed_result,
    failed_result,
    final_answer_committed,
    interaction_result,
    normalize_stream_error,
    update_tool_events,
    interrupt_kind_from_state,
    interrupt_payloads,
    is_valid_hitl_interrupt as is_valid_hitl_interrupt,
)
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
    workspace_state_path,
)
from agent.session_lock import SessionLockBusyError
from agent.session_runtime import RestorePlan, SessionLeaseLike, SessionRuntime
from agent.stream import DeltaHandler, StreamDeltaCallback, merge_stream_callbacks
from agent.tools.human_interaction import validate_human_input_values

HUMAN_TOOLS = frozenset({"request_human_input"})

# Maximum time close() waits for an active operation to drain. Timing out never
# releases the session lease early: close() raises, keeps the lease, and can be
# retried once the run finally drained.
CLOSE_DRAIN_SECONDS = 30.0
# How long a cancelled run waits for its model HTTP request to confirm it has
# closed before reporting the cleanup as unconfirmed.
MODEL_STREAM_CLOSE_SECONDS = 5.0
MODEL_STREAM_UNCLOSED = "Cancelled, but the model request did not confirm it stopped"


def _exclusive_operation(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def guarded(self: AgentRunner, *args: Any, **kwargs: Any) -> Any:
        if self._closed:
            raise RuntimeError("Runner is closed")
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError("Runner already has an active operation")
        # The reservation in force on entry was made for this operation. One
        # made while it runs belongs to the caller's next run and must survive.
        reservation = self.control.reserved_generation
        try:
            return method(self, *args, **kwargs)
        finally:
            # An operation that ended before begin_run must not leave its
            # reservation (and a cancel aimed at it) for a later run.
            if reservation is not None:
                self.control.release_reservation(reservation)
            self._operation_lock.release()
    return guarded


@dataclass
class SessionSnapshot:
    info: SessionInfo
    transcript: list[TranscriptBlock]
    interrupt_kind: InterruptKind | None = None
    human_input: dict[str, Any] = field(default_factory=dict)
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    model_metrics_history: list[dict[str, Any]] = field(default_factory=list)


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
        mcp_manager: Any | None = None,
    ) -> None:
        if prepared is not None and (backend is not None or sandbox_config is not None):
            raise ValueError("prepared already defines the backend and sandbox configuration")
        self.control = prepared.run_controller if prepared is not None and prepared.run_controller is not None else RunController()
        self.control.set_event_handler(self._on_control_event)
        self.on_delta = on_delta
        self.on_event = on_event
        self._event_handler: RunEventHandler | None = None
        self._seen_tool_calls: set[str] = set()
        self._subagent_tool_ids: set[str] = set()
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
        configured_mode: SandboxMode = (
            self._sandbox_config.mode if self._sandbox_config is not None else "restricted"
        )
        self._configured_sandbox_mode = configured_mode
        self._sandbox_mode = configured_mode
        self._pending_sandbox_mode: SandboxMode | None = None
        self._context_usage_invalidated = False
        self._resume_context: RecoveryContext | None = None
        self._environment = EnvironmentCache()
        self._closed = False
        self._close_completed = False
        self.mcp = mcp_manager
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
            profile, attachment_store=self.attachment_store, trust_env=cfg.trust_env,
            timeout=cfg.request_timeout,
        ))
        object.__setattr__(initial_model, "_deep_agent_model_id", self._current_model_id)
        self._spec = prepared.spec if prepared is not None else AgentSpec(
            settings=cfg,
            trust_env=cfg.trust_env,
            instructions=cfg.agent_instructions,
            hidden_tools=cfg.hidden_tools,
            backend=backend, sandbox=self._sandbox_config or cfg.sandbox,
            web_search_api_key=os.environ.get("TAVILY_API_KEY") or cfg.tavily_api_key,
        )
        self._pause_condition = prepared.pause_condition if prepared is not None else (lambda: False)
        self._custom_interrupt_on = None
        if prepared is not None and prepared.interrupt_on:
            from agent.mcp.names import is_managed_mcp_name

            native_interrupt = {
                name: rule for name, rule in prepared.interrupt_on.items() if not is_managed_mcp_name(name)
            }
            if native_interrupt and native_interrupt != (interrupt_on_for_mode(PermissionMode.ASK) or {}):
                self._custom_interrupt_on = native_interrupt
        if prepared is not None:
            inferred = permission_mode_from_interrupt_on(prepared.interrupt_on)
            if inferred is PermissionMode.ALLOW and not allow_mode_available(prepared.execution_mode):
                raise ValueError(allow_mode_unavailable_reason(prepared.execution_mode))
            self._permission_mode = inferred
        reuse_prepared = (
            prepared is not None
            and saver is prepared.checkpointer
            and initial_model is prepared.model
            and mcp_manager is None
            and (settings is None or prepared.spec.settings == settings)
        )
        override = prepared.interrupt_on if prepared is not None else None
        if mcp_manager is not None:
            override = self._custom_interrupt_on if self._permission_mode is PermissionMode.ASK else None
        self.prepared = prepared if reuse_prepared else build_agent(
            self._spec, initial_model, self._permission_mode, saver,
            self.control, self._pause_condition,
            interrupt_on_override=override,
            backend_selection=prepared.backend_selection if prepared is not None else None,
            mcp=mcp_manager, settings=settings,
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

    def sandbox_mode(self) -> SandboxMode:
        return self._sandbox_mode

    def pending_sandbox_mode(self) -> SandboxMode | None:
        with self._pending_config_lock:
            return self._pending_sandbox_mode

    def request_sandbox_mode(self, mode: str) -> SandboxMode:
        if self._closed:
            raise RuntimeError("Runner is closed")
        self._require_active_session()
        parsed = parse_sandbox_mode(mode)
        if parsed is None:
            raise ValueError("sandbox mode must be restricted or full-access")
        with self._pending_config_lock:
            self._pending_sandbox_mode = None if parsed == self._sandbox_mode else parsed
        return parsed

    def _has_pending_runtime_config(self) -> bool:
        with self._pending_config_lock:
            pending = (self._pending_model_id is not None or self._pending_permission_mode is not None
                       or self._pending_reasoning_effort is not None or self._pending_sandbox_mode is not None)
        return pending or (self.mcp is not None and self.mcp.toolset_dirty())

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

    def apply_pending_config_if_idle(
        self,
        handler: RunEventHandler | None = None,
        *,
        lock_timeout: float = 0,
    ) -> bool:
        """Apply a queued model, permission, sandbox, or MCP toolset change.

        The operation lock is taken here, so callers do not touch it. Returns
        False when a run holds the lock or an interrupt is still pending; the
        queued change stays in place for the next safe point. A closed runner
        raises instead of applying.
        """
        if self._closed:
            raise RuntimeError("Runner is closed")
        if not self._operation_lock.acquire(timeout=lock_timeout):
            return False
        try:
            if self.current_interrupt() is not None:
                return False
            self._apply_pending_runtime_config(handler)
            return True
        finally:
            self._operation_lock.release()

    def _apply_pending_runtime_config(self, handler: RunEventHandler | None = None) -> None:
        failures: list[str] = []
        applied_sandbox = None
        with self._pending_config_lock:
            model_id = self._pending_model_id
            mode = self._pending_permission_mode
            effort = self._pending_reasoning_effort
            sandbox_mode = self._pending_sandbox_mode
            mcp_dirty = self.mcp is not None and self.mcp.toolset_dirty()
            if model_id is None and mode is None and effort is None and sandbox_mode is None and not mcp_dirty:
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
            applied_mcp = False
            try:
                if model_id is not None or mode is not None or effort is not None or mcp_dirty:
                    target_mode = mode or self._permission_mode
                    target_effort = effort if effort is not None else self._reasoning_effort
                    chat = self._chat_model
                    if profile is not None and (model_id is not None or effort is not None):
                        chat = build_chat_model(
                            profile, attachment_store=self.attachment_store,
                            trust_env=self._spec.trust_env,
                            reasoning_effort=None if target_effort == "default" else target_effort,
                            timeout=self.settings.request_timeout,
                        )
                    self._rebuild_prepared(model=chat, permission_mode=target_mode)
                    applied_mcp = mcp_dirty
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
                applied_mcp = False
            if model_id is not None:
                self._context_usage_invalidated = True
                self._current_model_id = profile.id
            if mode is not None:
                self._permission_mode = mode
            if effort is not None:
                self._reasoning_effort = effort
            applied_sandbox = None
            if sandbox_mode is not None:
                try:
                    self._apply_sandbox_mode(sandbox_mode)
                    applied_sandbox = sandbox_mode
                except Exception as exc:  # noqa: BLE001
                    failures.append(str(exc))
            self._pending_model_id = None
            self._pending_permission_mode = None
            self._pending_reasoning_effort = None
            self._pending_sandbox_mode = None
            if applied_sandbox is not None:
                self._sandbox_mode = applied_sandbox
            self._save_pending_config()
        for failure in failures:
            _emit(handler or self.on_event, RunEvent(type="runtime_config_failed", content=failure, is_error=True))
        if model_id is not None or mode is not None or effort is not None or applied_sandbox is not None or applied_mcp:
            _emit(handler or self.on_event, RunEvent(
                type="runtime_config_applied",
                result={"model_id": self._current_model_id, "permission_mode": self._permission_mode.value,
                        "reasoning_effort": self._reasoning_effort, "reasoning_changed": effort is not None,
                        "model_changed": model_id is not None, "permission_changed": mode is not None,
                        "sandbox_mode": self._sandbox_mode, "sandbox_changed": applied_sandbox is not None,
                        "mcp_changed": applied_mcp},
            ))

    def _apply_sandbox_mode(self, mode: SandboxMode) -> None:
        backend = self.prepared.backend
        if not isinstance(backend, WorkspaceCompositeBackend):
            raise ValueError("Sandbox mode can only be changed for a Bubblewrap backend")
        backend.apply_sandbox_mode(mode)
        if self._sandbox_config is not None:
            self._sandbox_config = replace(self._sandbox_config, mode=mode)
        if self._spec.sandbox is not None:
            self._spec = replace(self._spec, sandbox=replace(self._spec.sandbox, mode=mode))

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

    def model_metrics_history(self) -> list[dict[str, Any]]:
        return self.session_store.model_metrics_history(self.thread_id) if self.session_store is not None else []

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
                # Without a source ID, usage cannot be safely attributed
                # after a model/source switch.
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
                trust_env=self._spec.trust_env,
                reasoning_effort=None if self._reasoning_effort == "default" else self._reasoning_effort,
                attachment_store=self.attachment_store,
                timeout=self.settings.request_timeout,
            )
        self.prepared = build_agent(
            self._spec, chat, mode, self._checkpointer,
            self.control, self._pause_condition,
            interrupt_on_override=self._custom_interrupt_on if mode is PermissionMode.ASK else None,
            backend_selection=self.prepared.backend_selection,
            mcp=self.mcp, settings=self.settings,
        )
        self._chat_model = chat

    @property
    def sandbox_pool(self) -> SandboxPool | None:
        """本 Runner 跨会话复用的持久沙箱。"""
        return self.prepared.sandbox_pool

    def _end_sandbox_session(self, reason: str) -> None:
        pool = self.sandbox_pool
        if pool is not None:
            pool.shutdown(reason)

    def _close_mcp(self) -> None:
        manager = getattr(self, "mcp", None)
        if manager is not None:
            manager.close()

    def request_pause(self) -> None:
        self.control.request_pause()

    def request_cancel(self) -> None:
        """Request hard cancel of the model stream, cancellable tools, and the graph.

        Returns without waiting; the operation in progress reports the outcome.
        Also cancels a run reserved with ``reserve_run`` that has not started.
        Only the run's own exit path records its stop reason: the cancel may
        arrive after the run already finished, and a run that never reaches
        that path stays PENDING, which recovery already handles.
        """
        self.control.cancel()

    def reserve_run(self) -> int:
        """Mark that the caller is dispatching a run so an early cancel is not lost."""
        return self.control.reserve_run()

    def release_run_reservation(self, reservation: int) -> None:
        self.control.release_reservation(reservation)

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
        transcript = messages_to_transcript(
            messages,
            turn_timings=self.session_store.turn_timings(info.id) if self.session_store is not None else None,
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
            model_metrics_history=self.session_store.model_metrics_history(info.id) if self.session_store is not None else [],
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
        self._environment.invalidate(snapshot.info.id)
        self._restore_configured_sandbox_mode()
        return snapshot

    def _restore_configured_sandbox_mode(self) -> None:
        """Resume and new sessions use the configured filesystem mode, not a prior /sandbox choice."""
        self._pending_sandbox_mode = None
        if self._sandbox_mode == self._configured_sandbox_mode:
            return
        backend = self.prepared.backend
        if isinstance(backend, WorkspaceCompositeBackend):
            backend.apply_sandbox_mode(self._configured_sandbox_mode)
        self._sandbox_mode = self._configured_sandbox_mode

    def _apply_restore_plan(self, plan: RestorePlan) -> None:
        if self.settings is not None and plan.model_id and (
            plan.model_id != self._current_model_id or plan.reasoning_effort != self._reasoning_effort
        ):
            profile = self.settings.get_profile(plan.model_id)
            self._rebuild_prepared(
                model=build_chat_model(
                    profile, attachment_store=self.attachment_store,
                    trust_env=self._spec.trust_env,
                    reasoning_effort=None if plan.reasoning_effort == "default" else plan.reasoning_effort,
                    timeout=self.settings.request_timeout,
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
        self._restore_configured_sandbox_mode()
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
            # The active graph may still be unwinding an MCP call. Keep its
            # bridge and clients alive with the lease until close is retried.
            raise RuntimeError(
                "Runner close timed out with a run still active; the session "
                "lease is kept until the run drains and close is retried"
            )
        if not self.control.wait_model_streams_closed(MODEL_STREAM_CLOSE_SECONDS):
            raise RuntimeError(
                "Runner close could not confirm the model request stopped; the "
                "session lease is kept until it closes and close is retried"
            )
        self._close_mcp()
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
        validate_human_input_values(values, state.payload)
        envelope: dict[str, Any] = {"type": "human_input", "values": dict(values)}
        if state.payload.get("interactionId"):
            envelope["interactionId"] = state.payload["interactionId"]
        return self._resume(envelope, on_delta=on_delta, on_event=on_event)

    @_exclusive_operation
    def decide_tools(
        self,
        decisions: Sequence[dict[str, Any]],
        *,
        on_delta: DeltaHandler | None = None,
        on_event: RunEventHandler | None = None,
    ) -> RunResult:
        """Resume a complete approval batch with an explicit decision per call."""
        self._require_active_session()
        state = self._require_interrupt(InterruptKind.WAITING_CONFIRMATION, "decide_tools")
        pending_ids = [str(call.get("toolCallId", "")) for call in state.pending_tools]
        by_id: dict[str, dict[str, Any]] = {}
        for decision in decisions:
            call_id = decision.get("tool_call_id")
            kind = decision.get("decision")
            if call_id not in pending_ids or call_id in by_id or kind not in ("approve", "reject"):
                raise ValueError("Each pending tool must have exactly one approve/reject decision")
            item = {"type": kind}
            message = decision.get("message")
            if message is not None and not isinstance(message, str):
                raise ValueError("Decision message must be a string")
            if kind == "reject" and message:
                item["message"] = message
            by_id[call_id] = item
        if set(by_id) != set(pending_ids):
            raise ValueError("Decisions must cover every pending tool call")
        return self._resume(
            {"decisions": [by_id[call_id] for call_id in pending_ids]},
            on_delta=on_delta, on_event=on_event,
        )

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

        metrics_thread_id = self.thread_id

        def record_model_metrics(metrics: dict[str, Any]) -> None:
            if self.session_store is not None:
                self.session_store.append_model_metrics(metrics_thread_id, metrics)
            _emit(event_handler, RunEvent(type="model_metrics", result=metrics))

        callback = StreamDeltaCallback(
            handler or (lambda _kind, _text: None),
            on_usage=stream_usage,
            on_metrics=record_model_metrics,
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
        run_control = self.control.begin_run()
        set_output_emitter(self._emit_tool_output)
        attachment_token = set_attachment_store(self.attachment_store)
        recovery_token = set_recovery_context(self._resume_context)
        environment_token = set_environment_cache(self._environment)
        stream_owner_token = set_stream_owner(self.control)
        self._busy = True
        self._context_usage_invalidated = True
        _emit(event_handler, RunEvent(type="run_started"))

        result: RunResult | None = None
        turn_started = time.monotonic()

        def emit_turn_completed(output: str) -> None:
            timing = TurnTiming(
                elapsed_seconds=max(0.0, time.monotonic() - turn_started),
                finished_at=datetime.now(timezone.utc),
            )
            if self.session_store is not None:
                state = self.prepared.graph.get_state(self._thread_config())
                messages = list((state.values or {}).get("messages", []) or [])
                answer = next((item for item in reversed(messages) if isinstance(item, AIMessage)), None)
                if answer is not None and answer.id:
                    self.session_store.save_turn_timing(
                        self.thread_id, answer.id, timing.elapsed_seconds, timing.finished_at,
                    )
            _emit(event_handler, RunEvent(
                type="turn_completed",
                content=output,
                result=timing,
            ))

        try:
            result = self._stream_once(graph_input, config, run_control, event_handler)
            result = self._resume_config_boundaries(result, config, run_control, event_handler)
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
                turn_started = time.monotonic()
                self._apply_pending_runtime_config(event_handler)
                result = self._stream_once(
                    {"messages": [HumanMessage(content=text)]},
                    config,
                    run_control,
                    event_handler,
                )
                result = self._resume_config_boundaries(result, config, run_control, event_handler)
            if result.status == "completed":
                _emit(event_handler, RunEvent(type="run_completed", content=result.output))
                emit_turn_completed(result.output)
                self._touch_status(StopReason.STOP)
            return result
        finally:
            self._busy = False
            self.control.end_run()
            set_output_emitter(None)
            reset_stream_owner(stream_owner_token)
            reset_attachment_store(attachment_token)
            reset_recovery_context(recovery_token)
            reset_environment_cache(environment_token)
            if (result is not None and result.status == "completed") or (
                self._resume_context is not None and self._resume_context.text is None
            ):
                self._resume_context = None
            self._event_handler = None

    def _resume_config_boundaries(
        self,
        result: RunResult,
        config: dict[str, Any],
        run_control: Any,
        event_handler: RunEventHandler | None,
    ) -> RunResult:
        """Apply a queued runtime change and resume the same run. Not a new generation."""
        while result.status == "runtime_config_boundary":
            self._apply_pending_runtime_config(event_handler)
            result = self._stream_once(Command(resume=True), config, run_control, event_handler)
        return result

    def _refresh_environment_snapshot(self) -> None:
        """Reuse a matching sandbox fingerprint. Probe again only when it changed."""
        try:
            if self._runtime is None or self._runtime.thread_id is None:
                return
            backend = bubblewrap_backend(self.prepared.backend)
            if backend is None:
                self._environment.snapshot = None
                return
            self._environment.ensure(
                self._runtime.thread_id, backend.config, self._permission_mode, probe=lambda: probe_sandbox(backend),
            )
        except Exception:  # noqa: BLE001
            self._environment.snapshot = None

    def _stream_once(
        self,
        graph_input: Any,
        config: dict[str, Any],
        run_control: Any,
        event_handler: RunEventHandler | None,
    ) -> RunResult:
        self._refresh_environment_snapshot()
        interrupted = ""
        pending: list[dict[str, Any]] = []
        human_input: dict[str, Any] = {}
        state_values = self.prepared.graph.get_state(self._thread_config()).values or {}
        self._subagent_tool_ids = {
            str(call.get("id") or "") for message in state_values.get("messages", [])
            if isinstance(message, AIMessage) for call in message.tool_calls or []
            if call.get("name") == "task"
        }
        last_message_id = _last_message_id(state_values)
        try:
            for chunk in self.prepared.graph.stream(
                graph_input, config, stream_mode=["updates", "values"], control=run_control,
                durability="sync",
            ):
                mode, payload = chunk
                if mode == "values":
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
            if self._final_answer_committed(last_message_id):
                return completed_result(_final_output(self.prepared.graph, self._thread_config()))
            return self._cancelled_result(event_handler)
        except Exception as exc:  # noqa: BLE001
            # A ModelStreamCancelled without a cancel request of this run is
            # not a user cancel and stays a failure, like any network error.
            failure = normalize_stream_error(exc, cancel_requested=self.control.cancel_requested)
            if failure is None:
                return self._cancelled_result(event_handler)
            _emit(event_handler, RunEvent(type="run_failed", content=str(failure), is_error=True))
            self._touch_status(StopReason.ERROR)
            return failed_result(str(failure))

        # graph.stream returned without GraphDrained: the graph reached its end
        # and the final message is already checkpointed. A cancel that arrived
        # after the last step does not undo that, so the run stays completed.
        if interrupted:
            self.control.set_defer_steering(True)
            self._touch_status(StopReason.DEFERRED)
            result = interaction_result(interrupted, pending, human_input)
            _emit(event_handler, RunEvent(type="interaction_requested", result=result))
            return result

        return completed_result(_final_output(self.prepared.graph, self._thread_config()))

    def _final_answer_committed(self, last_message_id: str | None) -> bool:
        """Whether this run checkpointed a final answer before it was drained.

        After an answer without tool calls only no-op middleware hooks remain
        (or a steering continuation, which the cancel stops), so the turn is
        complete and the late cancel must not report it as cancelled.
        """
        values = self.prepared.graph.get_state(self._thread_config()).values or {}
        return final_answer_committed(values.get("messages") or [], last_message_id)

    def _cancelled_result(self, event_handler: RunEventHandler | None) -> RunResult:
        """Finish a cancelled run once its model request is confirmed closed.

        graph.stream has returned, so no node can still write a checkpoint.
        An unconfirmed close is reported on the result and makes close() keep
        the session lease.
        """
        closed = self.control.wait_model_streams_closed(MODEL_STREAM_CLOSE_SECONDS)
        self._touch_status(StopReason.ABORTED)
        error, result = cancelled_result(
            model_stream_closed=closed, unclosed_message=MODEL_STREAM_UNCLOSED,
        )
        _emit(event_handler, RunEvent(type="run_cancelled", content=error, is_error=not closed))
        return result

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
        events, saw_input_usage = update_tool_events(chunk, self._seen_tool_calls)
        # 图更新可能省略 Usage，不能覆盖同次调用已收到的流式输入用量。
        if saw_input_usage:
            self._context_usage_invalidated = False
        for event in events:
            if event.type == "tool_started" and event.name == "task":
                self._subagent_tool_ids.add(event.tool_call_id)
            elif event.type == "tool_completed" and event.tool_call_id in self._subagent_tool_ids:
                # Native task returns an unnamed ToolMessage. Preserve the
                # presentation identity even when resuming a saved tool step.
                event = replace(event, name="task")
            _emit(handler, event)
