# Stream the compiled graph, classify LangGraph interrupts, resume with Command.
# Token-level reasoning/assistant deltas use LangChain callbacks (ChatOpenAI streaming=True).
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
import tempfile
from typing import Any, Callable, Sequence
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphDrained
from langgraph.types import Command

from deepagents.backends.protocol import BackendProtocol
from agent.cancel import set_output_emitter, set_run_controller
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
from agent.factory import PreparedAgent, create_agent, state_file
from agent.llm import build_chat_model
from agent.permission import (
    PermissionMode,
    interrupt_on_for_mode,
    parse_permission_mode,
)
from agent.session import (
    SessionInfo,
    SessionStore,
    TranscriptBlock,
    messages_to_transcript,
    workspace_state_path,
)
from agent.stream import DeltaHandler, StreamDeltaCallback, merge_stream_callbacks, visible_text

HUMAN_TOOLS = frozenset({"request_human_input", "handoff_to_human"})


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


RunEventHandler = Callable[[RunEvent], None]


@dataclass
class RunResult:
    status: str
    output: str = ""
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    human_input: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class SessionSnapshot:
    info: SessionInfo
    transcript: list[TranscriptBlock]
    interrupt_kind: str = ""
    human_input: dict[str, Any] = field(default_factory=dict)
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)


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
        workspace: Path | None = None,
        settings: Settings | None = None,
        model_id: str | None = None,
    ) -> None:
        self._pause_requested = False
        self.control = RunController(on_control_event=self._on_control_event)
        self.on_delta = on_delta
        self.on_event = on_event
        self._event_handler: RunEventHandler | None = None
        self._seen_tool_calls: set[str] = set()
        self.settings = settings
        self._sandbox_config = sandbox_config or (settings.sandbox if settings else None)
        self._busy = False
        self._permission_mode = PermissionMode.ASK

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
        if saver is None and self.session_store is not None:
            saver = self.session_store.checkpointer
        self._checkpointer = saver

        initial_model = model
        self._current_model_id = "default"
        if settings is not None:
            profile = settings.get_profile(model_id) if model_id else settings.active_profile
            self._current_model_id = profile.id
            if initial_model is None:
                initial_model = build_chat_model(profile, attachment_store=self.attachment_store)

        self.prepared = prepared or create_agent(
            model=initial_model,
            checkpointer=saver,
            backend=backend,
            sandbox_config=self._sandbox_config,
            settings=settings,
            should_pause=lambda: self._pause_requested,
            run_controller=self.control,
            interrupt_on=interrupt_on_for_mode(self._permission_mode),
        )
        self._chat_model = initial_model
        self._backend = backend if backend is not None else self.prepared.backend
        if prepared is not None:
            # Preserve whatever HITL map the caller already compiled.
            self._permission_mode = (
                PermissionMode.ALLOW if not prepared.interrupt_on else PermissionMode.ASK
            )

        if thread_id is None:
            if self.session_store is not None:
                info = self.session_store.create_session()
                self.thread_id = info.id
            else:
                self.thread_id = f"cli-{uuid4()}"
        else:
            self.thread_id = thread_id
            if self.session_store is not None and self.session_store.get(thread_id) is None:
                self.session_store.create_session(session_id=thread_id)

        self.state_path = (
            self.session_store.path if self.session_store is not None
            else workspace_state_path(workspace or Path.cwd())
        )

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

    def supports_input(self, kind: InputKind) -> bool:
        profile = self.current_model()
        return kind == "text" if profile is None else profile.supports_input(kind)

    def switch_model(self, id_or_prefix: str) -> ModelProfile:
        if self.settings is None:
            raise RuntimeError("Model switching requires Settings with llm.models")
        if self._busy:
            raise RuntimeError("Cannot switch model while a run is in progress")
        profile = self.settings.get_profile(id_or_prefix)
        if profile.id == self._current_model_id:
            return profile
        self._rebuild_prepared(model=build_chat_model(
            profile, attachment_store=self.attachment_store,
        ))
        self._current_model_id = profile.id
        return profile

    def permission_mode(self) -> PermissionMode:
        return self._permission_mode

    def set_permission_mode(self, mode: PermissionMode | str) -> PermissionMode:
        if self._busy:
            raise RuntimeError("Cannot change permission mode while a run is in progress")
        if isinstance(mode, str):
            parsed = parse_permission_mode(mode)
            if parsed is None:
                raise ValueError(f"Unknown permission mode: {mode}")
            mode = parsed
        if mode is self._permission_mode:
            return mode
        model = self._chat_model
        if self.settings is not None:
            model = build_chat_model(
                self.settings.get_profile(self._current_model_id),
                attachment_store=self.attachment_store,
            )
        self._rebuild_prepared(model=model, permission_mode=mode)
        self._permission_mode = mode
        return mode

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
                attachment_store=self.attachment_store,
            )
        self.prepared = create_agent(
            model=chat,
            checkpointer=self._checkpointer,
            backend=self._backend,
            sandbox_config=self._sandbox_config,
            settings=self.settings,
            should_pause=lambda: self._pause_requested,
            run_controller=self.control,
            system_prompt=self.prepared.system_prompt,
            interrupt_on=interrupt_on_for_mode(mode),
            files=self.prepared.files,
        )
        self._chat_model = chat

    def request_pause(self) -> None:
        self._pause_requested = True

    def request_cancel(self) -> None:
        """Request hard cancel for execute / cancellable tools and drain the graph."""
        self.control.cancel()

    def steer(self, text: str) -> None:
        self.control.steer(text)

    def follow_up(self, text: str) -> None:
        self.control.follow_up(text)

    def take_unapplied_messages(self) -> list[str]:
        return [item.text for item in self.control.take_unapplied()]

    def list_sessions(self, *, limit: int = 50) -> list[SessionInfo]:
        if self.session_store is None:
            return []
        return self.session_store.list_sessions(limit=limit)

    def load_session(self, session_id: str) -> SessionSnapshot | None:
        if self.session_store is None:
            return None
        info = self.session_store.resolve_prefix(session_id) or self.session_store.get(session_id)
        if info is None:
            return None
        config = {"configurable": {"thread_id": info.id}}
        try:
            state = self.prepared.graph.get_state(config)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed to load session {info.id}: {exc}") from exc
        messages = list((state.values or {}).get("messages", []) or [])
        kind, payload, pending = interrupt_kind_from_state(self.prepared.graph, config)
        return SessionSnapshot(
            info=info,
            transcript=messages_to_transcript(messages),
            interrupt_kind=kind,
            human_input=payload if kind == "waiting_human" else {},
            pending_tool_calls=pending,
        )

    def switch_session(self, session_id: str) -> SessionSnapshot:
        snapshot = self.load_session(session_id)
        if snapshot is None:
            raise KeyError(f"Unknown session: {session_id}")
        self.thread_id = snapshot.info.id
        return snapshot

    def new_session(self, *, title: str = "") -> SessionInfo:
        if self.session_store is None:
            self.thread_id = f"cli-{uuid4()}"
            now = datetime.now(timezone.utc)
            return SessionInfo(
                id=self.thread_id,
                title=title or "New session",
                created_at=now,
                updated_at=now,
                status="running",
            )
        info = self.session_store.create_session(title=title)
        self.thread_id = info.id
        return info

    def invoke(
        self,
        text: str,
        *,
        images: Sequence[ImageAttachment] = (),
        on_delta: DeltaHandler | None = None,
        on_event: RunEventHandler | None = None,
    ) -> RunResult:
        if images and not self.supports_input("image"):
            raise ValueError("The current model does not declare image input support")
        if len(images) > MAX_IMAGES_PER_MESSAGE:
            raise ValueError(f"A message can contain at most {MAX_IMAGES_PER_MESSAGE} images")
        if images and not getattr(self._chat_model, "materializes_attachment_refs", False):
            raise RuntimeError(
                "The current custom model does not implement Deep-Agent image attachment resolution"
            )
        refs = tuple(self.attachment_store.put(image) for image in images)
        return self.invoke_with_attachment_refs(
            text,
            image_refs=refs,
            on_delta=on_delta,
            on_event=on_event,
        )

    def invoke_with_attachment_refs(
        self,
        text: str,
        *,
        image_refs: Sequence[ImageAttachmentRef] = (),
        on_delta: DeltaHandler | None = None,
        on_event: RunEventHandler | None = None,
    ) -> RunResult:
        refs = tuple(image_refs)
        if refs and not self.supports_input("image"):
            raise ValueError("The current model does not declare image input support")
        if len(refs) > MAX_IMAGES_PER_MESSAGE:
            raise ValueError(f"A message can contain at most {MAX_IMAGES_PER_MESSAGE} images")
        if refs and not getattr(self._chat_model, "materializes_attachment_refs", False):
            raise RuntimeError(
                "The current custom model does not implement Deep-Agent image attachment resolution"
            )
        additional = {ATTACHMENT_META_KEY: refs_to_dicts(refs)} if refs else {}
        graph_input: dict[str, Any] = {
            "messages": [HumanMessage(content=text, additional_kwargs=additional)],
        }
        files = self.prepared.files_for_state()
        if files:
            graph_input["files"] = {path: state_file(content) for path, content in files.items()}
        if self.session_store is not None:
            title = text.strip().splitlines()[0][:80] if text.strip() else None
            self.session_store.touch(self.thread_id, status="running", title=title)
        return self._stream(graph_input, on_delta=on_delta, on_event=on_event)

    def store_image(self, image: ImageAttachment) -> ImageAttachmentRef:
        if not self.supports_input("image"):
            raise ValueError("The current model does not declare image input support")
        return self.attachment_store.put(image)

    def cleanup_attachments(
        self,
        *,
        protected: Sequence[ImageAttachmentRef] = (),
    ) -> AttachmentCleanupResult:
        if self._busy:
            raise RuntimeError("Cannot clean attachments while a run is in progress")
        if self.session_store is None:
            return self.attachment_store.cleanup(
                set(), protected_storage_keys=(ref.storage_key for ref in protected),
            )
        return self.session_store.cleanup_attachments(
            protected=tuple(protected), release_runtime_lease=True,
        )

    def resume(
        self,
        decision: dict[str, Any] | str | bool | None = None,
        *,
        on_delta: DeltaHandler | None = None,
        on_event: RunEventHandler | None = None,
    ) -> RunResult:
        kind, payload, pending = interrupt_kind_from_state(
            self.prepared.graph, self._thread_config(),
        )
        graph_input = self._resume_command(decision, kind, pending, payload)
        if self.session_store is not None:
            self.session_store.touch(self.thread_id, status="running")
        return self._stream(graph_input, on_delta=on_delta, on_event=on_event)

    def current_interrupt(self) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
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
        if handler is None and event_handler is None:
            return config
        callback = StreamDeltaCallback(
            handler or (lambda _kind, _text: None),
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
        set_run_controller(self.control)
        set_output_emitter(self._emit_tool_output)
        self._busy = True
        _emit(event_handler, RunEvent(type="run_started"))

        try:
            result = self._stream_once(graph_input, config, run_control, event_handler)
            # Late steering that arrived after the final after_agent check stays in-run.
            while (
                result.status == "completed"
                and self.control.has_pending_steering()
                and not self.control.cancel_requested
            ):
                text = self.control.pop_steering()
                if text is None:
                    break
                run_control = self.control.begin_run()
                result = self._stream_once(
                    {"messages": [HumanMessage(content=text)]},
                    config,
                    run_control,
                    event_handler,
                )
            return result
        finally:
            self._busy = False
            self.control.end_run()
            set_output_emitter(None)
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
        try:
            for chunk in self.prepared.graph.stream(
                graph_input, config, stream_mode="updates", control=run_control,
            ):
                self._emit_update_events(chunk, event_handler)
                if not isinstance(chunk, dict):
                    continue
                for node, update in chunk.items():
                    if node != "__interrupt__":
                        continue
                    interrupted, payload, pending = classify_interrupt(
                        update, self.prepared.graph, self._thread_config(),
                    )
                    if interrupted == "waiting_human":
                        human_input = payload
        except GraphDrained:
            _emit(event_handler, RunEvent(type="run_cancelled"))
            self._touch_status("cancelled")
            return RunResult(status="cancelled")
        except Exception as exc:  # noqa: BLE001
            if self.control.cancel_requested:
                _emit(event_handler, RunEvent(type="run_cancelled"))
                self._touch_status("cancelled")
                return RunResult(status="cancelled")
            _emit(event_handler, RunEvent(type="run_failed", content=str(exc), is_error=True))
            self._touch_status("failed")
            return RunResult(status="failed", error=str(exc))

        if self.control.cancel_requested and not interrupted:
            _emit(event_handler, RunEvent(type="run_cancelled"))
            self._touch_status("cancelled")
            return RunResult(status="cancelled")

        if interrupted:
            status = "waiting" if interrupted.startswith("waiting") else interrupted
            mapped = "interrupted" if interrupted == "paused" else status
            if interrupted in {"waiting_human", "waiting_confirmation"}:
                mapped = "waiting"
            self._touch_status(mapped if mapped in {"waiting", "interrupted"} else "waiting")
            result = RunResult(
                status=interrupted,
                pending_tool_calls=pending,
                human_input=human_input,
            )
            _emit(event_handler, RunEvent(type="interaction_requested", result=result))
            return result

        output = _final_output(self.prepared.graph, self._thread_config())
        if self.control.has_pending_steering():
            # Caller may start a supplemental invocation before completing.
            return RunResult(status="completed", output=output)
        _emit(event_handler, RunEvent(type="run_completed", content=output))
        self._touch_status("completed")
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

    def _touch_status(self, status: str) -> None:
        if self.session_store is not None:
            self.session_store.touch(self.thread_id, status=status)  # type: ignore[arg-type]

    def _emit_update_events(self, chunk: Any, handler: RunEventHandler | None) -> None:
        if handler is None:
            return
        for message in _messages_in_update(chunk):
            if isinstance(message, AIMessage):
                message_id = str(getattr(message, "id", "") or "")
                for call in message.tool_calls or []:
                    tool_call_id = str(call.get("id") or "")
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
                content = _message_text(message)
                status = str(getattr(message, "status", "") or "")
                _emit(handler, RunEvent(
                    type="tool_completed",
                    tool_call_id=str(getattr(message, "tool_call_id", "") or ""),
                    name=str(getattr(message, "name", "") or "tool"),
                    content=content,
                    result=getattr(message, "content", content),
                    is_error=status == "error" or content.lower().startswith("error")
                    or "cancelled by user" in content.lower(),
                ))

    def _resume_command(
        self,
        decision: dict[str, Any] | str | bool | None,
        kind: str,
        pending: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> Command:
        if isinstance(decision, bool):
            self._pause_requested = False
            return Command(resume=decision)
        if kind == "paused" or _decision_type(decision) in ("continue", "resume"):
            self._pause_requested = False
            return Command(resume=True)
        if kind == "waiting_human" or _decision_type(decision) in ("human_input", "input"):
            return Command(resume=_human_resume_value(decision, payload))
        return Command(resume=_hitl_resume_value(decision, pending))


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


def classify_interrupt(
    interrupts: Any, graph: Any, config: dict[str, Any],
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    payloads = interrupt_payloads(interrupts)
    for payload in payloads:
        kind = str(payload.get("type", "")).lower()
        if kind == "human_input":
            return "waiting_human", payload, []
        if kind == "pause":
            return "paused", payload, []
    pending = resolve_pending_tool_calls(interrupts, graph, config)
    if pending:
        return "waiting_confirmation", {}, pending
    if payloads:
        return "waiting_human", payloads[0], []
    return "waiting_confirmation", {}, []


def interrupt_kind_from_state(graph: Any, config: dict[str, Any]) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    try:
        state = graph.get_state(config)
    except Exception:  # noqa: BLE001
        return "", {}, []
    interrupts = getattr(state, "interrupts", ()) or ()
    if not interrupts:
        return "", {}, []
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


def _emit(handler: RunEventHandler | None, event: RunEvent) -> None:
    if handler is not None:
        try:
            handler(event)
        except Exception:  # noqa: BLE001
            # Presentation observers must never change Agent/Tool execution semantics.
            pass


def _decision_type(decision: dict[str, Any] | str | bool | None) -> str:
    if isinstance(decision, dict):
        return str(decision.get("type", "")).lower()
    return ""


def _human_resume_value(decision: dict[str, Any] | str | bool | None, payload: dict[str, Any]) -> Any:
    if isinstance(decision, str):
        return decision
    if not isinstance(decision, dict):
        return ""
    values = decision.get("values")
    if isinstance(values, dict) and values:
        return dict(decision)
    text = str(decision.get("text") or "")
    option_id = str(decision.get("optionId") or decision.get("option_id") or "")
    if option_id or decision.get("interactionId") or payload.get("interactionId"):
        resume: dict[str, Any] = {}
        if text.strip():
            resume["text"] = text
        if option_id:
            resume["optionId"] = option_id
        if decision.get("interactionId"):
            resume["interactionId"] = decision["interactionId"]
        leftover = {
            key: value for key, value in decision.items()
            if key not in {"type", "text", "optionId", "option_id", "interactionId", "values"}
        }
        resume.update(leftover)
        return resume
    return text


def _hitl_resume_value(
    decision: dict[str, Any] | str | bool | None, pending: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = decision if isinstance(decision, dict) else {}
    decision_type = str(payload.get("type", "approve")).lower()
    if decision_type not in ("approve", "reject"):
        decision_type = "reject"
    target = str(payload.get("toolCallId") or "")
    item: dict[str, Any] = {"type": decision_type}
    if decision_type == "reject" and payload.get("message"):
        item["message"] = str(payload["message"])
    pending_ids = [str(call.get("toolCallId", "")) for call in pending]
    if len(pending_ids) <= 1 or not target or target not in pending_ids:
        return {"decisions": [dict(item) for _ in range(max(len(pending_ids), 1))]}
    return {"decisions": [
        dict(item) if tool_call_id == target
        else {"type": "reject", "message": "另一个并发的待确认调用未包含在本次人工决策中，按拒绝处理"}
        for tool_call_id in pending_ids
    ]}
