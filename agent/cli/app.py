from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import os
import time
from pathlib import Path
from typing import Any

from prompt_toolkit import ANSI, Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText, to_formatted_text
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import BufferControl, ConditionalContainer, Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.data_structures import Point
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from agent.attachments import (
    MAX_IMAGES_PER_MESSAGE,
    ImageAttachmentRef,
    image_attachment_from_path,
)
from agent.cli.clipboard import (
    ClipboardAdapter,
    ClipboardError,
    ClipboardFiles,
    ClipboardImage,
    ClipboardText,
    ClipboardUnavailable,
    copy_to_clipboard,
    windows_path_to_wsl,
)
from agent.cli.commands import Command, command_table
from agent.cli.input import Keymap
from agent.cli.interactions import InteractionController
from agent.cli.rendering import render_interaction, render_transcript
from agent.cli.selection import SelectableFormattedTextControl
from agent.cli.state import CliState
from agent.permission import (
    PERMISSION_ALLOW_WARNING,
    PermissionMode,
    allow_mode_available,
    allow_mode_unavailable_reason,
    parse_permission_mode,
    permission_mode_label,
)
from agent.runner import AgentRunner, RunEvent, RunResult


class SlashCompleter(Completer):
    def __init__(self, commands: tuple[Command, ...]) -> None:
        self.commands = commands
        self.accepted_text: str | None = None

    @staticmethod
    def _rank(name: str, query: str) -> tuple[int, int, int] | None:
        if name == query:
            return (0, 0, len(name))
        if name.startswith(query):
            return (1, 0, len(name))
        offset = 0
        gaps = 0
        for char in query:
            found = name.find(char, offset)
            if found < 0:
                return None
            gaps += found - offset
            offset = found + 1
        return (2, gaps, len(name))

    def get_completions(self, document, complete_event):  # type: ignore[no-untyped-def]
        before = document.text_before_cursor
        if "\n" in before or not before.startswith("/") or " " in before:
            return
        if before == self.accepted_text:
            return
        prefix = before[1:].lower()
        candidates = []
        for index, command in enumerate(self.commands):
            rank = self._rank(command.name.lower(), prefix)
            if rank is not None:
                candidates.append((rank, index, command))
        for _, _, command in sorted(candidates):
            description = command.description
            if command.unavailable_reason:
                description += " (unavailable)"
            yield Completion(
                f"/{command.name}",
                start_position=-len(before),
                display_meta=description,
            )


class CliApplication:
    """pi-inspired terminal presentation over the existing synchronous runner."""

    def __init__(
        self,
        runner: AgentRunner,
        *,
        config_dir: Path | None = None,
        input: Any = None,
        output: Any = None,
    ) -> None:
        self.runner = runner
        _register_terminal_sequences()
        self.state = CliState()
        self.commands = command_table()
        self.command_by_name = {item.name: item for item in self.commands}
        keymap_path = (config_dir / "keybindings.json") if config_dir else None
        self.keymap = Keymap.load(keymap_path)
        self.interaction: InteractionController | None = None
        self.clipboard = ClipboardAdapter()
        self._io_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="deep-agent-io")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._last_ctrl_c = 0.0
        self._transcript_line_count = 1
        # None = stick to bottom (follow new output); int = pinned scroll row.
        self._transcript_anchor: int | None = None

        self.slash_completer = SlashCompleter(self.commands)
        self.buffer = Buffer(
            multiline=True,
            history=InMemoryHistory(),
            completer=self.slash_completer,
            complete_while_typing=True,
        )
        self.transcript_control = SelectableFormattedTextControl(
            text=self._transcript_text,
            on_copy=self._copy_selected_text,
            on_selection_start=self._begin_transcript_selection,
            get_cursor_position=self._transcript_cursor,
        )
        self.interaction_control = FormattedTextControl(text=self._interaction_text, focusable=False)
        self.attachment_control = FormattedTextControl(text=self._attachment_text, focusable=False)
        self.footer_control = FormattedTextControl(text=self._footer_text, focusable=False)
        self.editor_control = BufferControl(buffer=self.buffer, focusable=True)
        self.bindings = self._create_bindings()

        interaction_visible = Condition(lambda: self.interaction is not None)
        editor_visible = Condition(lambda: self.interaction is None or self.interaction.accepts_text)
        attachments_visible = Condition(lambda: bool(self.state.attachments) and self.interaction is None)

        def editor_height() -> Any:
            lines = max(1, self.buffer.document.line_count)
            try:
                rows = self.application.output.get_size().rows
            except Exception:  # noqa: BLE001
                rows = 30
            maximum = max(3, min(10, rows // 3))
            return Dimension(min=1, preferred=min(lines, maximum), max=maximum)

        body = HSplit([
            Window(
                self.transcript_control,
                wrap_lines=True,
                height=Dimension(min=3, weight=1),
                always_hide_cursor=True,
                right_margins=[ScrollbarMargin(display_arrows=False)],
            ),
            Window(height=1, char="─", style="class:editor-border"),
            ConditionalContainer(
                Window(
                    self.attachment_control,
                    wrap_lines=True,
                    height=lambda: len(self.state.attachments),
                    dont_extend_height=True,
                    style="class:attachments",
                ),
                filter=attachments_visible,
            ),
            ConditionalContainer(
                Window(
                    self.interaction_control,
                    wrap_lines=True,
                    height=Dimension(min=3, preferred=10, max=16),
                    dont_extend_height=True,
                    always_hide_cursor=True,
                    style="class:interaction",
                ),
                filter=interaction_visible,
            ),
            ConditionalContainer(
                Window(
                    self.editor_control,
                    wrap_lines=True,
                    height=editor_height,
                    dont_extend_height=True,
                ),
                filter=editor_visible,
            ),
            Window(height=1, char="─", style="class:editor-border"),
            Window(self.footer_control, height=2, style="class:footer", dont_extend_height=True),
        ])
        root = FloatContainer(
            content=body,
            floats=[
                Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=8, scroll_offset=1)),
            ],
        )
        self.application: Application[None] = Application(
            layout=Layout(root, focused_element=self.editor_control),
            key_bindings=self.bindings,
            full_screen=True,
            mouse_support=True,
            style=Style.from_dict({
                "editor-border": "#77a8bd",
                "footer": "#858585",
                "interaction": "bg:#15191d #d0d0d0",
                "attachments": "bg:#17242a #72d5e8",
                "selection": "bg:#3b5c73 #ffffff",
                "scrollbar.background": "#202020",
                "scrollbar.button": "#666666",
                "completion-menu": "bg:#15191d #d0d0d0",
                "completion-menu.completion.current": "bg:#3b5c73 #ffffff",
                "completion-menu.meta.completion.current": "bg:#3b5c73 #ffffff",
            }),
            input=input,
            output=output,
            refresh_interval=0.1,
        )
        if self.keymap.warning:
            self.state.add_system(self.keymap.warning, error=True)
        if self.runner.on_event is None:
            self.runner.on_event = self._on_event_thread

    def run(self) -> None:
        def capture_loop() -> None:
            self._loop = asyncio.get_running_loop()

        self.application.run(pre_run=capture_loop)

    async def run_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.application.run_async()

    def set_status(self, text: str) -> None:
        self.state.status = text
        self.application.invalidate()

    def show_help(self) -> None:
        commands = "\n".join(
            f"/{item.name:<9} {item.description}"
            + (f" — unavailable: {item.unavailable_reason}" if item.unavailable_reason else "")
            for item in self.commands
        )
        self.state.add_system(
            "Keyboard\n"
            "Enter submit · Ctrl+J newline · Alt+Enter follow-up · Esc cancel+restore · "
            "PgUp/PgDn scroll · Ctrl+P next model · Alt+P prev model · Alt+Up restore queue · "
            "Ctrl+O tools · Ctrl+T thinking · drag transcript to copy · "
            "Ctrl+V/Alt+V paste image/text · "
            "Ctrl+C clear/exit · Ctrl+D exit\n\n"
            f"Commands\n{commands}"
        )

    def show_status(self) -> None:
        prepared = self.runner.prepared
        mode = prepared.execution_mode.value
        tool_count = len(prepared.exposed_tool_names) + len(prepared.filesystem_tools) + 3
        model = self.runner.current_model()
        model_line = (
            f"Model: {model.model}\nInputs: {', '.join(model.input)}\n"
            if model is not None else "Model: (fixed)\n"
        )
        perm = permission_mode_label(self.runner.permission_mode())
        self.state.add_system(
            f"Status: {'running' if self.state.running else 'idle'}\n"
            f"{model_line}"
            f"Permission: {perm}\n"
            f"Thread: {self.runner.thread_id}\nSandbox: {mode}\nTools: {tool_count}"
        )

    def show_session(self) -> None:
        store = self.runner.session_store
        model = self.runner.current_model()
        model_line = (
            f"Model: {model.model}\n" if model is not None else ""
        )
        if store is None:
            self.state.add_system(
                f"Session: {self.runner.thread_id}\n"
                f"{model_line}"
                "Checkpointer: in-memory\nPersistent /resume is not configured."
            )
            return
        info = store.get(self.runner.thread_id)
        status = info.status if info else "unknown"
        stop_reason = info.last_run_status.value if info else "unknown"
        title = info.title if info else ""
        self.state.add_system(
            f"Session: {self.runner.thread_id}\n"
            f"Title: {title}\n"
            f"Status: {status}\n"
            f"Last run: {stop_reason}\n"
            f"{model_line}"
            f"Checkpointer: sqlite\n"
            f"Persistent store: {self.runner.state_path}"
        )

    def new_session(self) -> None:
        if self.state.running:
            self.set_status("Cancel the active run before starting a new session")
            return
        info = self.runner.new_session()
        self.state.clear()
        self.state.attachments.clear()
        self.transcript_control.clear_selection()
        self.interaction = None
        self.state.add_system(f"Started session {info.id}")
        self.set_status("Ready")

    async def resume_session(self, arg: str = "") -> None:
        if self.state.running:
            self.set_status("Cancel the active run before switching sessions")
            return
        if self.runner.session_store is None:
            self.state.add_system("/resume is unavailable: persistent session storage is not configured", error=True)
            return
        try:
            if arg.strip():
                snapshot = self.runner.switch_session(arg.strip())
            else:
                sessions = self.runner.list_sessions(limit=20)
                if not sessions:
                    self.state.add_system("No saved sessions in this workspace.", error=True)
                    return
                options = [
                    {
                        "value": item.id,
                        "label": f"{item.id[:8]} · {item.last_run_status.value} · {item.title[:40]}",
                    }
                    for item in sessions
                ]
                self.interaction = InteractionController(
                    kind="resume",
                    title="Resume session",
                    question="Select a session to restore",
                    fields=[{
                        "id": "session",
                        "type": "single_select",
                        "label": "Session",
                        "required": True,
                        "options": options,
                    }],
                )
                self.set_status("Select a session · Enter confirm · Esc cancel")
                self.application.invalidate()
                return
        except KeyError as exc:
            self.state.add_system(str(exc), error=True)
            return
        except RuntimeError as exc:
            self.state.add_system(str(exc), error=True)
            return
        self._apply_session_snapshot(snapshot)

    async def select_model(self, arg: str = "") -> None:
        if self.state.running:
            self.set_status("Cancel the active run before switching models")
            return
        if self.interaction is not None:
            self.set_status("Finish the current interaction before switching models")
            return
        if self.state.attachments:
            self.set_status("Clear pending images before switching models")
            return
        profiles = self.runner.list_models()
        if not profiles:
            self.state.add_system(
                "/model is unavailable: configure llm.models in config.yaml",
                error=True,
            )
            return
        try:
            if arg.strip():
                profile = self.runner.switch_model(arg.strip())
                self.state.add_system(f"Switched model to {profile.model}")
                self.set_status(f"Model: {profile.model}")
                return
            current = self.runner.current_model()
            current_id = current.id if current else ""
            options = [
                {
                    "value": item.id,
                    "label": item.model
                    + (" · current" if item.id == current_id else ""),
                }
                for item in profiles
            ]
            self.interaction = InteractionController(
                kind="model",
                title="Select model",
                question="Choose an OpenAI-compatible model profile",
                fields=[{
                    "id": "model",
                    "type": "single_select",
                    "label": "Model",
                    "required": True,
                    "options": options,
                }],
            )
            # Pre-select current model when possible.
            if current_id:
                for index, option in enumerate(options):
                    if option["value"] == current_id:
                        self.interaction.option_index = index
                        break
            self.set_status("Select a model · Enter confirm · Esc cancel")
            self.application.invalidate()
        except (KeyError, RuntimeError) as exc:
            self.state.add_system(str(exc), error=True)

    async def select_permission(self, arg: str = "") -> None:
        if self.state.running:
            self.set_status("Cancel the active run before changing permission mode")
            return
        if self.interaction is not None:
            self.set_status("Finish the current interaction before changing permission mode")
            return
        raw = arg.strip()
        if raw:
            mode = parse_permission_mode(raw)
            if mode is None:
                hint = "ask or allow" if self._allow_available() else "ask"
                self.state.add_system(
                    f"Unknown permission mode. Use /permission {hint}",
                    error=True,
                )
                return
            await self._apply_permission_mode(mode)
            return
        current = self.runner.permission_mode()
        options = [
            {
                "value": PermissionMode.ASK.value,
                "label": "ask · require approval for every execute and side-effect tool"
                + (" · current" if current is PermissionMode.ASK else ""),
            },
        ]
        if self._allow_available():
            options.append({
                "value": PermissionMode.ALLOW.value,
                "label": "allow · auto-approve all tools (SANDBOXED, HIGH RISK)"
                + (" · current" if current is PermissionMode.ALLOW else ""),
            })
        if len(options) == 1:
            self.state.add_system(
                "Permission mode is locked to ask because ALLOW requires SANDBOXED execution.",
            )
            self.set_status("Permission: ask")
            return
        self.interaction = InteractionController(
            kind="permission",
            title="Permission mode",
            question="Choose how tools are approved before they run",
            fields=[{
                "id": "mode",
                "type": "single_select",
                "label": "Mode",
                "required": True,
                "options": options,
            }],
        )
        self.interaction.option_index = 0 if current is PermissionMode.ASK else 1
        self.set_status("Select permission mode · Enter confirm · Esc cancel")
        self.application.invalidate()

    def _allow_available(self) -> bool:
        return allow_mode_available(self.runner.prepared.execution_mode)

    async def _apply_permission_mode(self, mode: PermissionMode) -> None:
        if mode is PermissionMode.ALLOW and not self._allow_available():
            self.state.add_system(
                allow_mode_unavailable_reason(self.runner.prepared.execution_mode),
                error=True,
            )
            return
        if mode is PermissionMode.ALLOW and self.runner.permission_mode() is not PermissionMode.ALLOW:
            self._begin_allow_permission_confirm()
            return
        try:
            applied = self.runner.set_permission_mode(mode)
        except (RuntimeError, ValueError) as exc:
            self.state.add_system(str(exc), error=True)
            return
        label = permission_mode_label(applied)
        self.state.add_system(f"Permission mode: {label}")
        self.set_status(f"Permission: {applied.value}")

    def _begin_allow_permission_confirm(self) -> None:
        self.interaction = InteractionController(
            kind="permission_confirm",
            title="Enable ALLOW? (HIGH RISK)",
            question=PERMISSION_ALLOW_WARNING,
            fields=[{
                "id": "confirm",
                "type": "text",
                "label": 'Type ALLOW to confirm auto-approve for all tools',
                "required": True,
                "options": [],
            }],
        )
        self.set_status("Type ALLOW to confirm · Esc cancel")
        self.application.invalidate()

    def cycle_model(self, *, delta: int = 1) -> None:
        if self.state.running:
            self.set_status("Cancel the active run before switching models")
            return
        if self.interaction is not None:
            self.set_status("Finish the current interaction before switching models")
            return
        if self.state.attachments:
            self.set_status("Clear pending images before switching models")
            return
        profiles = self.runner.list_models()
        if len(profiles) < 2:
            if not profiles:
                self.set_status("No models configured in config.yaml")
            else:
                current = self.runner.current_model()
                label = current.model if current else profiles[0].model
                self.set_status(f"Only one model configured: {label}")
            return
        current = self.runner.current_model()
        current_id = current.id if current else profiles[0].id
        index = next((i for i, item in enumerate(profiles) if item.id == current_id), 0)
        nxt = profiles[(index + delta) % len(profiles)]
        try:
            profile = self.runner.switch_model(nxt.id)
        except (KeyError, RuntimeError) as exc:
            self.state.add_system(str(exc), error=True)
            return
        self.state.add_system(f"Switched model to {profile.model}")
        self.set_status(f"Model: {profile.model}")
        self.application.invalidate()

    def _restore_queued_to_editor(self, buffer: Any) -> list[str]:
        """Pull unapplied steering/follow-up back into the editor (pi Esc / Alt+Up)."""
        from agent.cli.state import MessageBlock

        restored_parts = self.runner.take_unapplied_messages()
        self.state.blocks = [
            block for block in self.state.blocks
            if not (isinstance(block, MessageBlock) and block.pending)
        ]
        self.state.pending_user_ids.clear()
        if restored_parts:
            existing = buffer.text.strip()
            restored = "\n\n".join(restored_parts)
            text = f"{existing}\n\n{restored}" if existing else restored
            buffer.text = text
            buffer.cursor_position = len(text)
        return restored_parts

    def _apply_session_snapshot(self, snapshot: Any) -> None:
        self.state.load_transcript(snapshot.transcript)
        self.state.todos = list(snapshot.todos)
        self.transcript_control.clear_selection()
        self.interaction = None
        self.state.add_system(
            f"Resumed session {snapshot.info.id} (last run: {snapshot.info.last_run_status.value})"
        )
        for notice in snapshot.notices:
            self.state.add_system(notice)
        if snapshot.interrupt_kind == "waiting_confirmation":
            self.interaction = InteractionController.approval(snapshot.pending_tool_calls)
            self.state.status = "Waiting for input"
        elif snapshot.interrupt_kind == "waiting_human":
            self.interaction = InteractionController.human(snapshot.human_input)
            self.state.status = "Waiting for input"
        elif snapshot.interrupt_kind == "paused":
            self.interaction = InteractionController(
                kind="pause",
                title="Paused",
                question="Continue the agent run?",
                fields=[{
                    "id": "continue", "type": "single_select", "label": "Decision", "required": True,
                    "options": [{"value": "stay", "label": "No"}, {"value": "continue", "label": "Yes"}],
                }],
            )
            self.state.status = "Paused"
        else:
            self.set_status("Ready")
        self.application.invalidate()

    def exit(self) -> None:
        if self.state.running:
            self.runner.request_cancel()
        self._io_executor.shutdown(wait=False, cancel_futures=True)
        self.application.exit()

    async def _run_blocking(self, func: Any, /, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._io_executor, partial(func, *args, **kwargs))

    def _width(self) -> int:
        try:
            columns = self.application.output.get_size().columns
        except Exception:  # noqa: BLE001
            columns = 100
        return max(20, columns - 1)

    def _transcript_cursor(self) -> Point:
        last = max(0, self._transcript_line_count - 1)
        if self._transcript_anchor is None:
            return Point(x=0, y=last)
        return Point(x=0, y=min(last, max(0, self._transcript_anchor)))

    def _scroll_transcript(self, delta: int) -> None:
        last = max(0, self._transcript_line_count - 1)
        current = last if self._transcript_anchor is None else self._transcript_anchor
        nxt = min(last, max(0, current + delta))
        self._transcript_anchor = None if nxt >= last else nxt
        self.application.invalidate()

    def _transcript_text(self):  # type: ignore[no-untyped-def]
        rendered = render_transcript(self.state, self._width())
        self._transcript_line_count = rendered.count("\n") + 1
        return to_formatted_text(ANSI(rendered))

    def _interaction_text(self):  # type: ignore[no-untyped-def]
        return to_formatted_text(ANSI(render_interaction(self.interaction, self._width())))

    def _attachment_text(self):  # type: ignore[no-untyped-def]
        fragments = []
        for index, ref in enumerate(self.state.attachments, 1):
            size = f"{ref.size / (1024 * 1024):.1f} MiB" if ref.size >= 1024 * 1024 else f"{ref.size / 1024:.0f} KiB"
            fragments.append(("class:attachments", f" ▣ Image #{index}  {ref.filename} · {size}\n"))
        return FormattedText(fragments)

    def _footer_text(self):  # type: ignore[no-untyped-def]
        pending = self.runner.control.pending_steering_count() + self.runner.control.pending_follow_up_count()
        queue = f" · queued {pending}" if pending else ""
        mode = self.runner.prepared.execution_mode.value
        perm = self.runner.permission_mode().value
        spinner = " ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 10) % 11] if self.state.running else ""
        workspace = self.runner.settings.sandbox.workspace if self.runner.settings is not None else Path.cwd()
        first = _fit_footer_line(f" {workspace}", "", self._width())
        left = f" {spinner}{self.state.status}{queue} · {mode} · perm:{perm} · {self.runner.thread_id[:8]}"
        model = self.runner.current_model()
        right = model.model if model is not None else "fixed model"
        second = _fit_footer_line(left, right, self._width())
        return FormattedText([("class:footer", f"{first}\n{second}")])

    def _begin_transcript_selection(self) -> None:
        if self._transcript_anchor is None:
            self._transcript_anchor = max(0, self._transcript_line_count - 1)
        self.application.invalidate()

    def _copy_selected_text(self, value: str) -> None:
        previous = self.state.status
        try:
            backend = copy_to_clipboard(value, output=self.application.output)
            notice = f"Copied {len(value)} characters via {backend}"
        except ClipboardError as exc:
            notice = f"Copy failed: {exc}"
        self.state.status = notice
        self.application.invalidate()
        if self._loop is not None:
            def restore() -> None:
                if self.state.status == notice:
                    self.state.status = previous
                    self.application.invalidate()
            self._loop.call_later(2.0, restore)

    def _create_bindings(self) -> KeyBindings:
        kb = KeyBindings()
        interaction_active = Condition(lambda: self.interaction is not None)

        def bind(action: str):  # type: ignore[no-untyped-def]
            def register(handler):  # type: ignore[no-untyped-def]
                registered = False
                for sequence in self.keymap.sequences(action):
                    try:
                        kb.add(*sequence)(handler)
                        registered = True
                    except (KeyError, ValueError) as exc:
                        self.keymap.warning = f"Invalid keybinding for {action}: {' '.join(sequence)} ({exc})"
                if not registered:
                    for sequence in Keymap().sequences(action):
                        try:
                            kb.add(*sequence)(handler)
                        except (KeyError, ValueError):
                            continue
                return handler
            return register

        @bind("submit")
        def submit(event) -> None:  # type: ignore[no-untyped-def]
            if self._accept_command_completion(event.current_buffer):
                return
            self._submit_buffer("steer")

        completion_active = Condition(lambda: self.interaction is None and self.buffer.complete_state is not None)

        @kb.add("up", filter=completion_active)
        def completion_up(event) -> None:  # type: ignore[no-untyped-def]
            event.current_buffer.complete_previous()

        @kb.add("down", filter=completion_active)
        def completion_down(event) -> None:  # type: ignore[no-untyped-def]
            event.current_buffer.complete_next()

        @kb.add("tab", filter=completion_active)
        def completion_tab(event) -> None:  # type: ignore[no-untyped-def]
            self._accept_command_completion(event.current_buffer)

        @bind("newline")
        def newline(event) -> None:  # type: ignore[no-untyped-def]
            event.current_buffer.insert_text("\n")

        @bind("follow_up")
        def follow_up(event) -> None:  # type: ignore[no-untyped-def]
            self._submit_buffer("followUp")

        @bind("image_paste")
        def clipboard_paste(event) -> None:  # type: ignore[no-untyped-def]
            if self.interaction is not None:
                return
            self.set_status("Reading clipboard…")
            asyncio.create_task(self._paste_clipboard())

        @kb.add(Keys.BracketedPaste, eager=True)
        def bracketed_paste(event) -> None:  # type: ignore[no-untyped-def]
            self._handle_pasted_text(event.data.replace("\r\n", "\n").replace("\r", "\n"))

        @kb.add("backspace", filter=Condition(
            lambda: self.interaction is None and not self.buffer.text and bool(self.state.attachments)
        ))
        def remove_last_attachment(event) -> None:  # type: ignore[no-untyped-def]
            removed = self.state.attachments.pop()
            self.set_status(f"Removed image: {removed.filename}")

        @kb.add("up", filter=interaction_active)
        def interaction_up(event) -> None:  # type: ignore[no-untyped-def]
            assert self.interaction
            self.interaction.move(-1)
            self.application.invalidate()

        @kb.add("down", filter=interaction_active)
        def interaction_down(event) -> None:  # type: ignore[no-untyped-def]
            assert self.interaction
            self.interaction.move(1)
            self.application.invalidate()

        @kb.add("pageup", filter=~interaction_active)
        def page_up(event) -> None:  # type: ignore[no-untyped-def]
            self._scroll_transcript(-10)

        @kb.add("pagedown", filter=~interaction_active)
        def page_down(event) -> None:  # type: ignore[no-untyped-def]
            self._scroll_transcript(10)

        @kb.add("c-home", filter=~interaction_active)
        def scroll_top(event) -> None:  # type: ignore[no-untyped-def]
            self._transcript_anchor = 0
            self.application.invalidate()

        @kb.add("c-end", filter=~interaction_active)
        def scroll_bottom(event) -> None:  # type: ignore[no-untyped-def]
            self._transcript_anchor = None
            self.application.invalidate()

        @kb.add(" ", filter=interaction_active)
        def interaction_toggle(event) -> None:  # type: ignore[no-untyped-def]
            assert self.interaction
            if self.interaction.current.get("type") == "multi_select":
                self.interaction.toggle()
            else:
                event.current_buffer.insert_text(" ")

        @kb.add("tab", filter=interaction_active)
        def interaction_next(event) -> None:  # type: ignore[no-untyped-def]
            self._submit_buffer("steer")

        @kb.add("s-tab", filter=interaction_active)
        def interaction_previous(event) -> None:  # type: ignore[no-untyped-def]
            assert self.interaction
            if self.interaction.index > 0:
                self.interaction.index -= 1
                self.interaction.option_index = 0
                previous = self.interaction.values.get(str(self.interaction.current.get("id") or ""), "")
                if isinstance(previous, str):
                    event.current_buffer.text = previous
                    event.current_buffer.cursor_position = len(previous)

        @bind("interrupt")
        def escape(event) -> None:  # type: ignore[no-untyped-def]
            if event.current_buffer.complete_state is not None:
                event.current_buffer.cancel_completion()
            elif self.interaction is not None:
                self._finish_interaction(cancelled=True)
            elif self.state.running:
                restored = self._restore_queued_to_editor(event.current_buffer)
                self.runner.request_cancel()
                if restored:
                    self.set_status("Cancelling… · queued messages restored")
                else:
                    self.set_status("Cancelling…")
                self.application.invalidate()

        @bind("clear_or_exit")
        def ctrl_c(event) -> None:  # type: ignore[no-untyped-def]
            if self.interaction is not None:
                self._finish_interaction(cancelled=True)
                return
            now = time.monotonic()
            if now - self._last_ctrl_c < 0.5:
                self.exit()
                return
            event.current_buffer.reset()
            self.state.attachments.clear()
            self._last_ctrl_c = now
            self.set_status("Input cleared · press Ctrl+C again to exit")

        @bind("exit")
        def ctrl_d(event) -> None:  # type: ignore[no-untyped-def]
            if not event.current_buffer.text:
                self.exit()
            else:
                event.current_buffer.delete()

        @bind("tools_expand")
        def tools_expand(event) -> None:  # type: ignore[no-untyped-def]
            self.state.tools_expanded = not self.state.tools_expanded
            self.set_status(f"Tool output: {'expanded' if self.state.tools_expanded else 'collapsed'}")

        @bind("thinking_toggle")
        def thinking_toggle(event) -> None:  # type: ignore[no-untyped-def]
            self.state.thinking_collapsed = not self.state.thinking_collapsed
            self.set_status(f"Thinking: {'collapsed' if self.state.thinking_collapsed else 'expanded'}")

        @bind("dequeue")
        def dequeue(event) -> None:  # type: ignore[no-untyped-def]
            restored = self._restore_queued_to_editor(event.current_buffer)
            if not restored:
                self.set_status("No queued messages")
                return
            self.set_status("Queued messages restored")
            self.application.invalidate()

        @bind("model_cycle_forward")
        def model_forward(event) -> None:  # type: ignore[no-untyped-def]
            self.cycle_model(delta=1)

        @bind("model_cycle_backward")
        def model_backward(event) -> None:  # type: ignore[no-untyped-def]
            self.cycle_model(delta=-1)

        return kb

    def _accept_command_completion(self, buffer: Buffer) -> bool:
        state = buffer.complete_state
        if self.interaction is not None or state is None or not state.completions:
            return False
        completion = state.current_completion or state.completions[0]
        buffer.apply_completion(completion)
        self.slash_completer.accepted_text = buffer.document.text_before_cursor
        self.application.invalidate()
        return True

    def _submit_buffer(self, queue_mode: str) -> None:
        self.slash_completer.accepted_text = None
        text = self.buffer.text.strip()
        if self.interaction is not None:
            was_text = self.interaction.accepts_text
            complete = self.interaction.accept(text)
            if was_text or complete:
                self.buffer.reset(append_to_history=bool(text))
            if complete:
                self._finish_interaction()
            self.application.invalidate()
            return
        if not text and not self.state.attachments:
            return
        if text.startswith("/") and "\n" not in text:
            self.buffer.reset(append_to_history=True)
            asyncio.create_task(self._dispatch_command(text))
            return
        if self.state.running:
            if self.state.attachments:
                self.set_status("Wait for the active run before sending images")
                return
            self.buffer.reset(append_to_history=bool(text))
            if queue_mode == "followUp":
                self.runner.follow_up(text)
                self.set_status("Follow-up queued")
            else:
                self.runner.steer(text)
                self.set_status("Steering queued")
            self.application.invalidate()
            return
        self.buffer.reset(append_to_history=bool(text))
        self._start_run(text, image_refs=tuple(self.state.attachments))

    async def _dispatch_command(self, text: str) -> None:
        name, _, arg = text[1:].partition(" ")
        command = self.command_by_name.get(name)
        if command is None:
            self.state.add_system(f"Unknown command: /{name}", error=True)
        elif command.unavailable_reason:
            self.state.add_system(f"/{name} is unavailable: {command.unavailable_reason}", error=True)
        elif command.handler:
            await command.handler(self, arg.strip())
        self.application.invalidate()

    def _start_run(
        self,
        text: str,
        *,
        resume: dict | None = None,
        image_refs: tuple[ImageAttachmentRef, ...] = (),
    ) -> None:
        if resume is None:
            self.state.add_user(text, attachments=image_refs)
            self.state.attachments.clear()
        self.state.running = True
        self.set_status("Working…  Esc to cancel")

        async def work() -> None:
            if resume is None:
                try:
                    result = await self._run_blocking(
                        self.runner.invoke_with_attachment_refs,
                        text,
                        image_refs=image_refs,
                        on_event=self._on_event_thread,
                    )
                except Exception as exc:  # noqa: BLE001
                    self.state.attachments[:] = image_refs
                    self._apply_event(RunEvent(type="run_failed", content=str(exc)))
                    return
                if result.status == "failed" and image_refs:
                    self.state.attachments[:] = image_refs
            else:
                result = await self._run_blocking(
                    self.runner.resume, resume, on_event=self._on_event_thread,
                )
            self._handle_result(result)

        self._run_task = asyncio.create_task(work())

    async def image_command(self, arg: str) -> None:
        value = arg.strip()
        if not value:
            if not self.state.attachments:
                self.state.add_system("No images attached. Use /image <path>.")
                return
            lines = [f"Image #{index}: {ref.filename} ({ref.size} bytes)" for index, ref in enumerate(self.state.attachments, 1)]
            self.state.add_system("Pending images\n" + "\n".join(lines))
            return
        if value.lower() == "clear":
            count = len(self.state.attachments)
            self.state.attachments.clear()
            self.set_status(f"Cleared {count} pending image(s)")
            return
        if value.lower() == "clipboard":
            self.set_status("Reading clipboard…")
            await self._paste_clipboard(images_only=True)
            return
        await self._attach_path(value)

    async def attachments_command(self, arg: str) -> None:
        if arg.strip().lower() != "cleanup":
            self.state.add_system("Usage: /attachments cleanup", error=True)
            return
        try:
            result = await self._run_blocking(
                self.runner.cleanup_attachments,
                protected=tuple(self.state.attachments),
            )
        except Exception as exc:  # noqa: BLE001
            self.state.add_system(f"Attachment cleanup failed: {exc}", error=True)
            return
        if result.skipped_busy:
            self.state.add_system("Attachment cleanup skipped: this workspace is in use by another process.", error=True)
            return
        self.state.add_system(
            f"Attachment cleanup: scanned {result.scanned}, kept {result.kept}, "
            f"deleted {result.deleted}, freed {result.freed_bytes} bytes."
        )

    async def _attach_path(self, value: str) -> bool:
        if not self.runner.supports_input("image"):
            self.set_status("Current model does not support image input")
            return False
        if len(self.state.attachments) >= MAX_IMAGES_PER_MESSAGE:
            self.set_status(f"A message can contain at most {MAX_IMAGES_PER_MESSAGE} images")
            return False
        try:
            path = windows_path_to_wsl(value)
            attachment = await self._run_blocking(image_attachment_from_path, path)
            ref = await self._run_blocking(self.runner.store_image, attachment)
        except (OSError, ValueError, ClipboardError) as exc:
            self.state.add_system(str(exc), error=True)
            return False
        self.state.attachments.append(ref)
        self.set_status(f"Attached image: {ref.filename}")
        return True

    def _handle_pasted_text(self, payload: str) -> None:
        trimmed = payload.strip()
        if trimmed and "\n" not in trimmed:
            try:
                path = windows_path_to_wsl(trimmed)
                looks_like_image = path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
                if path.is_file() and looks_like_image:
                    if self.runner.supports_input("image"):
                        asyncio.create_task(self._attach_pasted_path(trimmed, payload))
                        return
                    self.set_status("Image path pasted as text: current model does not support images")
            except (OSError, ClipboardError):
                pass
        self.buffer.insert_text(payload)

    async def _attach_pasted_path(self, value: str, original_payload: str) -> None:
        if not await self._attach_path(value):
            self.buffer.insert_text(original_payload)
            self.application.invalidate()

    async def _paste_clipboard(self, *, images_only: bool = False) -> None:
        content = await self._run_blocking(self.clipboard.inspect)
        if isinstance(content, ClipboardText):
            if images_only:
                self.set_status("Clipboard does not contain an image")
                return
            self.buffer.insert_text(content.text)
            self.application.invalidate()
            return
        if isinstance(content, ClipboardUnavailable):
            self.set_status(content.reason)
            return
        if not self.runner.supports_input("image"):
            self.set_status("Current model does not support image input")
            return
        if isinstance(content, ClipboardFiles):
            attached = 0
            for value in content.paths:
                if len(self.state.attachments) >= MAX_IMAGES_PER_MESSAGE:
                    break
                if Path(value).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                    continue
                if await self._attach_path(value):
                    attached += 1
            if not attached:
                self.set_status("Clipboard contains no supported image files")
            return
        if isinstance(content, ClipboardImage):
            if len(self.state.attachments) >= MAX_IMAGES_PER_MESSAGE:
                self.set_status(f"A message can contain at most {MAX_IMAGES_PER_MESSAGE} images")
                return
            temp: Path | None = None
            try:
                temp = await self._run_blocking(self.clipboard.export_image)
                await self._attach_path(str(temp))
            except (OSError, ClipboardError, ValueError) as exc:
                self.state.add_system(str(exc), error=True)
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)

    def _on_event_thread(self, event: RunEvent) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._apply_event, event)
        else:
            self._apply_event(event)

    def _apply_event(self, event: RunEvent) -> None:
        follow = self._transcript_anchor is None
        self.state.apply(event)
        if follow:
            self._transcript_anchor = None
        self.application.invalidate()

    def _handle_result(self, result: RunResult) -> None:
        if result.status == "waiting_confirmation":
            self.interaction = InteractionController.approval(result.pending_tool_calls)
        elif result.status == "waiting_human":
            self.interaction = InteractionController.human(result.human_input)
        elif result.status == "paused":
            self.state.running = False
            self.state.add_system("Paused at a checkpoint. Submit /pause again is unnecessary; press Enter to resume.")
            self.interaction = InteractionController(
                kind="pause",
                title="Paused",
                question="Continue the agent run?",
                fields=[{
                    "id": "continue", "type": "single_select", "label": "Decision", "required": True,
                    "options": [{"value": "stay", "label": "No"}, {"value": "continue", "label": "Yes"}],
                }],
            )
        self.application.invalidate()

    def _finish_interaction(self, *, cancelled: bool = False) -> None:
        interaction = self.interaction
        if interaction is None:
            return
        if interaction.kind == "resume":
            if cancelled:
                self.interaction = None
                self.set_status("Resume cancelled")
                return
            session_id = str(interaction.values.get("session") or "")
            self.interaction = None
            if not session_id:
                self.state.add_system("No session selected", error=True)
                return
            try:
                snapshot = self.runner.switch_session(session_id)
            except (KeyError, RuntimeError) as exc:
                self.state.add_system(str(exc), error=True)
                return
            self._apply_session_snapshot(snapshot)
            return
        if interaction.kind == "model":
            if cancelled:
                self.interaction = None
                self.set_status("Model switch cancelled")
                return
            model_id = str(interaction.values.get("model") or "")
            self.interaction = None
            if not model_id:
                self.state.add_system("No model selected", error=True)
                return
            try:
                profile = self.runner.switch_model(model_id)
            except (KeyError, RuntimeError) as exc:
                self.state.add_system(str(exc), error=True)
                return
            self.state.add_system(f"Switched model to {profile.model}")
            self.set_status(f"Model: {profile.model}")
            return
        if interaction.kind == "permission":
            if cancelled:
                self.interaction = None
                self.set_status("Permission change cancelled")
                return
            mode_raw = str(interaction.values.get("mode") or "")
            self.interaction = None
            mode = parse_permission_mode(mode_raw)
            if mode is None:
                self.state.add_system("No permission mode selected", error=True)
                return
            if mode is PermissionMode.ALLOW and not allow_mode_available(self.runner.prepared.execution_mode):
                self.state.add_system(
                    allow_mode_unavailable_reason(self.runner.prepared.execution_mode),
                    error=True,
                )
                return
            if mode is PermissionMode.ALLOW and self.runner.permission_mode() is not PermissionMode.ALLOW:
                self._begin_allow_permission_confirm()
                return
            try:
                applied = self.runner.set_permission_mode(mode)
            except (RuntimeError, ValueError) as exc:
                self.state.add_system(str(exc), error=True)
                return
            self.state.add_system(f"Permission mode: {permission_mode_label(applied)}")
            self.set_status(f"Permission: {applied.value}")
            return
        if interaction.kind == "permission_confirm":
            if cancelled:
                self.interaction = None
                self.set_status("ALLOW mode cancelled")
                return
            typed = str(interaction.values.get("confirm") or "").strip()
            self.interaction = None
            if typed != "ALLOW":
                self.state.add_system(
                    'ALLOW not enabled. Type ALLOW exactly to confirm, or use /permission ask.',
                    error=True,
                )
                self.set_status("Permission: ask")
                return
            try:
                applied = self.runner.set_permission_mode(PermissionMode.ALLOW)
            except (RuntimeError, ValueError) as exc:
                self.state.add_system(str(exc), error=True)
                return
            self.state.add_system(f"Permission mode: {permission_mode_label(applied)}")
            self.set_status(f"Permission: {applied.value}")
            return
        if interaction.kind == "pause":
            choice = interaction.values.get("continue")
            if not cancelled and choice == "continue":
                self.interaction = None
                self._start_run("", resume={"type": "continue"})
            else:
                interaction.values.clear()
                interaction.option_index = 0
                interaction.error = "The run remains paused; select Yes to continue."
                self.interaction = interaction
                self.set_status("Paused")
            return
        decision = interaction.decision(cancelled=cancelled)
        self.interaction = None
        self._start_run("", resume=decision)


def default_config_dir() -> Path:
    configured = os.environ.get("DEEP_AGENT_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".deep-agent"


def _fit_footer_line(left: str, right: str, width: int) -> str:
    available = max(1, width)
    if not right:
        left = _truncate_cells(left, available)
        return left + (" " * max(0, available - get_cwidth(left)))
    right = _truncate_cells(right, available)
    remaining = max(0, available - get_cwidth(right) - 1)
    left = _truncate_cells(left, remaining)
    gap = max(1, available - get_cwidth(left) - get_cwidth(right))
    return f"{left}{' ' * gap}{right}"


def _truncate_cells(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if get_cwidth(value) <= width:
        return value
    if width == 1:
        return "…"
    result: list[str] = []
    used = 0
    for char in value:
        char_width = get_cwidth(char)
        if used + char_width > width - 1:
            break
        result.append(char)
        used += char_width
    return "".join(result) + "…"


def _register_terminal_sequences() -> None:
    # Kitty/CSI-u and xterm modified-key encodings used for Shift+Enter.
    # Ctrl+J remains the portable fallback when a terminal does not distinguish it.
    ANSI_SEQUENCES.setdefault("\x1b[13;2u", Keys.ControlJ)
    ANSI_SEQUENCES.setdefault("\x1b[13;2~", Keys.ControlJ)
