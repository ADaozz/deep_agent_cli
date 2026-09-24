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
from prompt_toolkit.layout import BufferControl, ConditionalContainer, Float, FloatContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.data_structures import Point
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
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
    windows_path_to_wsl,
)
from agent.cli.commands import Command, command_table
from agent.cli.gitinfo import REFRESH_SECONDS, GitProbe, GitSummary
from agent.cli.input import Keymap
from agent.cli.interactions import InteractionController
from agent.cli.rendering import (
    MUTATION_TOOLS,
    TranscriptRenderer,
    render_interaction,
    render_review,
)
from agent.cli.state import CliState, ToolBlock
from agent.config import ModelProfile, require_keybindings_outside_workspace
from agent.permission import (
    PERMISSION_ALLOW_WARNING,
    PermissionMode,
    allow_mode_available,
    allow_mode_unavailable_reason,
    parse_permission_mode,
    permission_mode_label,
)
from agent.runner import AgentRunner, InterruptKind, RunEvent, RunResult, UnknownInterruptError

# Path / status / workspace-git + context-usage.
FOOTER_LINES = 3


def format_context_window(window: int) -> str:
    """1000000 -> "1.0m", 200000 -> "200k"."""
    if window >= 1_000_000:
        return f"{window / 1_000_000:.1f}m"
    if window >= 1_000:
        return f"{round(window / 1_000)}k"
    return str(window)


def format_context_usage(usage: dict[str, int], window: int) -> str:
    """Right-hand footer label, empty when the profile declares no window."""
    if window <= 0:
        return ""
    label = f"{format_context_window(window)} Context"
    used = usage.get("total_tokens") or usage.get("input_tokens") or 0
    if used <= 0:
        return label
    percent = min(100.0, used / window * 100)
    return f"{label} · {percent:.1f}% used"


def model_display_name(profile: ModelProfile) -> str:
    """Include the configured source when two providers expose the same model."""
    return f"{profile.model} · {profile.source}" if profile.source else profile.model


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
            yield Completion(
                f"/{command.name}",
                start_position=-len(before),
                display_meta=command.description,
            )


class _ScrollableTextControl(FormattedTextControl):
    """Formatted text that consumes clicks and routes wheel events to the transcript."""

    def __init__(self, *args: Any, on_scroll: Any = None, **kwargs: Any) -> None:
        self._on_scroll = on_scroll
        super().__init__(*args, **kwargs)

    def mouse_handler(self, mouse_event):  # type: ignore[no-untyped-def]
        if self._on_scroll is not None:
            if mouse_event.event_type is MouseEventType.SCROLL_UP:
                self._on_scroll(-3)
                return None
            if mouse_event.event_type is MouseEventType.SCROLL_DOWN:
                self._on_scroll(3)
                return None
        return None


class _EditorScrollControl(BufferControl):
    """Input box: wheel scrolls the transcript instead of the empty editor."""

    def __init__(self, *args: Any, on_scroll: Any = None, **kwargs: Any) -> None:
        self._on_scroll = on_scroll
        super().__init__(*args, **kwargs)

    def mouse_handler(self, mouse_event):  # type: ignore[no-untyped-def]
        if self._on_scroll is not None and mouse_event.event_type in {
            MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN,
        }:
            self._on_scroll(-3 if mouse_event.event_type is MouseEventType.SCROLL_UP else 3)
            return None
        return super().mouse_handler(mouse_event)


class _TranscriptWindow(Window):
    """Transcript viewport whose scroll offset is authoritative, not cursor-derived.

    prompt_toolkit's scrollers only ever move ``vertical_scroll`` far enough to keep
    a cursor visible. Disguising the scroll anchor as a cursor therefore left the
    viewport stuck: ``max(previous_scroll, ...)`` blocked downward movement and
    ``min(..., get_max_vertical_scroll())`` blocked upward movement, so the
    scrollbar barely responded in either direction. This window computes the offset
    outright and skips the cursor-chasing arithmetic entirely.
    """

    def __init__(self, *args: Any, viewport: Any = None, **kwargs: Any) -> None:
        self._viewport = viewport
        self._scrollbar_drag: tuple[int, int] | None = None
        self._scrollbar_ypos = 0
        super().__init__(*args, **kwargs)

    def _scroll(self, ui_content: Any, width: int, height: int) -> None:
        self.horizontal_scroll = 0
        self.vertical_scroll_2 = 0
        anchor = self._viewport.transcript_anchor() if self._viewport is not None else None
        maximum = max(0, ui_content.line_count - height)
        self.vertical_scroll = maximum if anchor is None else min(maximum, max(0, anchor))

    def write_to_screen(self, screen: Any, mouse_handlers: Any, write_position: Any, *args: Any, **kwargs: Any) -> Any:
        self._scrollbar_ypos = write_position.ypos
        super().write_to_screen(screen, mouse_handlers, write_position, *args, **kwargs)
        if self._viewport is None:
            return
        margin_width = sum(self._get_margin_width(margin) for margin in self.right_margins)
        if margin_width <= 0:
            return
        # prompt_toolkit registers body handlers only up to `width - margin`, so
        # without this the scrollbar column swallows clicks and does nothing.
        mouse_handlers.set_mouse_handler_for_range(
            x_min=write_position.xpos + write_position.width - margin_width,
            x_max=write_position.xpos + write_position.width,
            y_min=write_position.ypos,
            y_max=write_position.ypos + write_position.height,
            handler=self._scrollbar_mouse_handler,
        )

    def _scrollbar_mouse_handler(self, mouse_event: Any) -> Any:
        info = self.render_info
        if info is None:
            return NotImplemented
        if mouse_event.event_type is MouseEventType.SCROLL_UP:
            self._viewport.scroll_transcript(-3)
        elif mouse_event.event_type is MouseEventType.SCROLL_DOWN:
            self._viewport.scroll_transcript(3)
        elif mouse_event.event_type is MouseEventType.MOUSE_UP:
            self._scrollbar_drag = None
        elif mouse_event.event_type is MouseEventType.MOUSE_DOWN and mouse_event.button is MouseButton.LEFT:
            rows = max(1, info.window_height)
            maximum = max(0, info.content_height - rows)
            y = min(rows - 1, max(0, mouse_event.position.y - self._scrollbar_ypos))
            thumb_height = min(rows, max(1, int(rows * len(info.displayed_lines) / max(1, info.content_height)) + 1))
            current_scroll = self._viewport.transcript_top()
            thumb_top = int(rows * current_scroll / max(1, info.content_height))
            if thumb_top <= y < thumb_top + thumb_height:
                # Holding the existing thumb must not move the viewport.
                self._scrollbar_drag = (y, current_scroll)
            else:
                # A track click jumps across the whole range, including both ends.
                target = round(y / max(1, rows - 1) * maximum)
                self._viewport.scroll_transcript_to(target)
                self._scrollbar_drag = (y, target)
        elif mouse_event.event_type is MouseEventType.MOUSE_MOVE and mouse_event.button is MouseButton.LEFT:
            if self._scrollbar_drag is None:
                return NotImplemented
            rows = max(1, info.window_height)
            maximum = max(0, info.content_height - rows)
            thumb_height = min(rows, max(1, int(rows * len(info.displayed_lines) / max(1, info.content_height)) + 1))
            travel = max(1, rows - thumb_height)
            start_y, start_scroll = self._scrollbar_drag
            y = min(rows - 1, max(0, mouse_event.position.y - self._scrollbar_ypos))
            self._viewport.scroll_transcript_to(start_scroll + round((y - start_y) * maximum / travel))
        else:
            return NotImplemented
        return None


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
        if config_dir is not None:
            sandbox = runner.settings.sandbox if runner.settings is not None else runner._sandbox_config
            workspace = sandbox.workspace if sandbox is not None else Path.cwd()
            require_keybindings_outside_workspace(config_dir, workspace)
        _register_terminal_sequences()
        self.state = CliState()
        self.commands = command_table()
        self.command_by_name = {item.name: item for item in self.commands}
        keymap_path = (config_dir / "keybindings.json") if config_dir else None
        self.keymap = Keymap.load(keymap_path)
        self.interaction: InteractionController | None = None
        self._reviewing = False
        self.clipboard = ClipboardAdapter()
        self._io_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="deep-agent-io")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._compacting = False
        self._last_ctrl_c = 0.0
        self._transcript_line_count = 1
        # None = stick to bottom (follow new output); int = pinned scroll row.
        self._transcript_anchor: int | None = None
        self._resume_picker_on_start = False
        self._renderer = TranscriptRenderer()
        self._git = GitProbe(self._workspace())
        self._git_summary = GitSummary()
        self._git_task: asyncio.Task[None] | None = None

        self.slash_completer = SlashCompleter(self.commands)
        self.buffer = Buffer(
            multiline=True,
            history=InMemoryHistory(),
            completer=self.slash_completer,
            complete_while_typing=True,
        )
        self.transcript_control = _ScrollableTextControl(
            text=self._transcript_text,
            focusable=False,
            show_cursor=False,
            get_cursor_position=self._transcript_cursor,
            on_scroll=self.scroll_transcript,
        )
        self.interaction_control = _ScrollableTextControl(
            text=self._interaction_text, focusable=False, on_scroll=self.scroll_transcript,
        )
        self.attachment_control = _ScrollableTextControl(
            text=self._attachment_text, focusable=False, on_scroll=self.scroll_transcript,
        )
        self.footer_control = _ScrollableTextControl(
            text=self._footer_text, focusable=False, on_scroll=self.scroll_transcript,
        )
        self.editor_control = _EditorScrollControl(
            buffer=self.buffer, focusable=True, on_scroll=self.scroll_transcript,
        )
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

        def interaction_height() -> Any:
            if self.interaction is None:
                return Dimension(min=3)
            text = render_interaction(self.interaction, self._width())
            lines = text.count("\n") + 1 if text else 3
            try:
                rows = self.application.output.get_size().rows
            except Exception:  # noqa: BLE001
                rows = 30
            maximum = max(8, rows - 8)
            return Dimension(min=3, preferred=min(max(lines, 3), maximum), max=maximum)

        self.transcript_window = _TranscriptWindow(
            self.transcript_control,
            wrap_lines=True,
            height=Dimension(min=3, weight=1),
            always_hide_cursor=True,
            right_margins=[ScrollbarMargin(display_arrows=False)],
            viewport=self,
        )
        body = HSplit([
            self.transcript_window,
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
                    height=interaction_height,
                    dont_extend_height=True,
                    always_hide_cursor=True,
                    style="class:interaction",
                ),
                filter=interaction_visible,
            ),
            ConditionalContainer(
                HSplit([
                    Window(height=1, char=" ", style="class:editor"),
                    VSplit([
                        Window(FormattedTextControl("› "), width=2, style="class:editor"),
                        Window(
                            self.editor_control,
                            wrap_lines=True,
                            height=editor_height,
                            dont_extend_height=True,
                            style="class:editor",
                        ),
                    ]),
                    Window(height=1, char=" ", style="class:editor"),
                ]),
                filter=editor_visible,
            ),
            Window(height=1, char="─", style="class:editor-border"),
            Window(
                self.footer_control,
                height=lambda: FOOTER_LINES if self._git_summary.label() else FOOTER_LINES - 1,
                style="class:footer",
                dont_extend_height=True,
            ),
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
                "editor": "bg:#303030 #ffffff",
                "footer": "#858585",
                "interaction": "#d0d0d0",
                "attachments": "#72d5e8",
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
            self._start_git_watch()
            if self._resume_picker_on_start:
                self._loop.create_task(self.resume_session())

        self.application.run(pre_run=capture_loop)

    async def run_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._start_git_watch()
        if self._resume_picker_on_start:
            await self.resume_session()
        await self.application.run_async()

    def _start_git_watch(self) -> None:
        if self._git_task is not None:
            return
        self._git_task = asyncio.get_running_loop().create_task(self._watch_git())

    async def _watch_git(self) -> None:
        """Keep the footer's git summary fresh without ever blocking a render pass."""
        while True:
            summary = await self._git.refresh()
            if summary != self._git_summary:
                self._git_summary = summary
                self.application.invalidate()
            await asyncio.sleep(REFRESH_SECONDS)

    def continue_session_message(self) -> str | None:
        if not self.runner.thread_has_content():
            return None
        model = self.runner.current_model()
        model_name = model_display_name(model) if model is not None else "fixed model"
        return (
            "To continue this session, run:\n"
            "\n"
            f"  deep-agent resume {self.runner.thread_id}\n"
            "\n"
            f"Or run deep-agent resume and select. Model: {model_name}"
        )

    def set_status(self, text: str) -> None:
        self.state.status = text
        self.application.invalidate()

    def _switch_model(self, id_or_prefix: str) -> ModelProfile:
        previous = self.runner.current_model()
        profile = self.runner.switch_model(id_or_prefix)
        if previous is None or previous.id != profile.id:
            self.state.usage.clear()
        return profile

    def show_help(self) -> None:
        commands = "\n".join(
            f"/{item.name:<9} {item.description}"
            for item in self.commands
        )
        self.state.add_system(
            "Keyboard\n"
            "Enter submit · Ctrl+J newline · Alt+Enter follow-up · Esc cancel+restore · "
            "PgUp/PgDn scroll · Ctrl+P next model · Alt+P prev model · Alt+Up restore queue · "
            "Ctrl+O tools · Ctrl+R review · F2 pending · Ctrl+T thinking · "
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
            f"Model: {model_display_name(model)}\nInputs: {', '.join(model.input)}\n"
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
            f"Model: {model_display_name(model)}\n" if model is not None else ""
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
        created_at = info.created_at.astimezone().isoformat(sep=" ", timespec="seconds") if info else "unknown"
        updated_at = info.updated_at.astimezone().isoformat(sep=" ", timespec="seconds") if info else "unknown"
        self.state.add_system(
            f"Session: {self.runner.thread_id}\n"
            f"Title: {title}\n"
            f"Created: {created_at}\n"
            f"Updated: {updated_at}\n"
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
        self._restore_queued_to_editor(self.buffer)
        try:
            info = self.runner.new_session()
        except RuntimeError as exc:
            self.state.add_system(str(exc), error=True)
            return
        self.state.clear()
        self.state.attachments.clear()
        self.interaction = None
        self._reviewing = False
        self._transcript_anchor = None
        self._renderer.clear()
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
                self._restore_queued_to_editor(self.buffer)
                snapshot = self.runner.switch_session(arg.strip())
            else:
                sessions = self._sessions_with_content(limit=50)
                if not sessions:
                    self.state.add_system("No sessions with conversation content.", error=True)
                    return
                options = [
                    {
                        "value": item.id,
                        "label": f"{item.id[:8]} · {item.last_run_status.value} · {item.title[:40]}",
                        "right_label": item.updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
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

    def _sessions_with_content(self, *, limit: int = 50) -> list[Any]:
        store = self.runner.session_store
        if store is None:
            return []
        sessions = []
        for info in self.runner.list_sessions(limit=limit):
            if not self.runner.thread_has_content(info.id):
                continue
            sessions.append(info)
        return sessions

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
                "/model is unavailable: configure llm.models in the agent config",
                error=True,
            )
            return
        try:
            if arg.strip():
                profile = self._switch_model(arg.strip())
                self.state.add_system(f"Switched model to {model_display_name(profile)}")
                self.set_status(f"Model: {model_display_name(profile)}")
                return
            current = self.runner.current_model()
            current_id = current.id if current else ""
            options = [
                {
                    "value": item.id,
                    "label": model_display_name(item)
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

    async def compact_command(self, arg: str = "") -> None:
        if arg.strip():
            self.state.add_system("Usage: /compact", error=True)
            return
        if self.state.running or self.interaction is not None:
            self.set_status("Finish the current run or interaction before compacting")
            return
        self._compacting = True
        self.state.running = True
        self.set_status("Compacting context…")
        try:
            result = await self._run_blocking(self.runner.compact_context)
        except Exception as exc:  # noqa: BLE001
            self.state.add_system(f"Context compaction failed: {exc}", error=True)
            self.set_status("Compaction failed")
            return
        finally:
            self._compacting = False
            self.state.running = False
            self.application.invalidate()
        if result.status == "ineligible":
            if result.percent is not None:
                notice = (
                    "Manual compaction is allowed at about 42.5% of the context window; "
                    f"current usage: {result.percent:.1f}%."
                )
                if result.percent >= 42.5:
                    notice += " Deep Agents has not accepted the latest model usage as eligible."
                self.state.add_system(notice)
            elif result.window_tokens > 0:
                self.state.add_system(
                    "Manual compaction is allowed at about 42.5% of the context window; "
                    "current usage is unknown because the model has not reported token usage."
                )
            else:
                self.state.add_system(
                    "Manual compaction requires about 85,000 tokens with the current unknown "
                    "context window; current usage percentage is unavailable. Configure context_window to show it."
                )
            self.set_status("Manual compaction unavailable")
        elif result.status == "nothing_to_compact":
            self.state.add_system("There are no older messages to compact yet.")
            self.set_status("No older context to compact")
        elif result.status == "compacted":
            self.state.usage.clear()
            self.state.add_system(result.message)
            self.set_status("Context compacted")
        else:
            self.state.add_system(result.message or "Context compaction failed", error=True)
            self.set_status("Compaction failed")

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
                self.set_status("No models configured in the agent config")
            else:
                current = self.runner.current_model()
                label = model_display_name(current or profiles[0])
                self.set_status(f"Only one model configured: {label}")
            return
        current = self.runner.current_model()
        current_id = current.id if current else profiles[0].id
        index = next((i for i, item in enumerate(profiles) if item.id == current_id), 0)
        nxt = profiles[(index + delta) % len(profiles)]
        try:
            profile = self._switch_model(nxt.id)
        except (KeyError, RuntimeError) as exc:
            self.state.add_system(str(exc), error=True)
            return
        self.state.add_system(f"Switched model to {model_display_name(profile)}")
        self.set_status(f"Model: {model_display_name(profile)}")
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
        self.interaction = None
        self._reviewing = False
        self.state.add_system(
            f"Resumed session {snapshot.info.id} (last run: {snapshot.info.last_run_status.value})"
        )
        for notice in snapshot.notices:
            self.state.add_system(notice)
        if not self._reopen_pending_interaction(notify_missing=False):
            self.set_status("Ready")
        self._transcript_anchor = None
        self._renderer.clear()
        self.state.usage = dict(self.runner.latest_usage())
        self.application.invalidate()

    def exit(self) -> None:
        if self.state.running:
            self.runner.request_cancel()
        if self._git_task is not None:
            self._git_task.cancel()
            self._git_task = None
        self._io_executor.shutdown(wait=False, cancel_futures=True)
        self.application.exit()

    async def _run_blocking(self, func: Any, /, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._io_executor, partial(func, *args, **kwargs))

    def _workspace(self) -> Path:
        sandbox = (
            self.runner.settings.sandbox
            if self.runner.settings is not None else self.runner._sandbox_config
        )
        return Path(sandbox.workspace) if sandbox is not None else Path.cwd()

    def _width(self) -> int:
        try:
            columns = self.application.output.get_size().columns
        except Exception:  # noqa: BLE001
            columns = 100
        return max(20, columns - 1)

    def _transcript_cursor(self) -> Point:
        last = max(0, self._transcript_line_count - 1)
        return Point(x=0, y=min(last, self.transcript_top()))

    def transcript_anchor(self) -> int | None:
        """Top row to pin the viewport to, or None while following new output."""
        return self._transcript_anchor

    def transcript_rows(self) -> int:
        """Total transcript rows as of the most recent paint."""
        return self._transcript_line_count

    def transcript_viewport_rows(self) -> int:
        """Visible transcript rows; from the viewport once it has painted."""
        info = self.transcript_window.render_info
        if info is not None:
            return max(1, info.window_height)
        try:
            rows = self.application.output.get_size().rows
        except Exception:  # noqa: BLE001
            rows = 24
        return max(1, rows)

    def transcript_max_scroll(self) -> int:
        return max(0, self.transcript_rows() - self.transcript_viewport_rows())

    def transcript_top(self) -> int:
        """Row currently sitting at the top of the viewport."""
        maximum = self.transcript_max_scroll()
        if self._transcript_anchor is None:
            return maximum
        return min(maximum, max(0, self._transcript_anchor))

    def scroll_transcript(self, delta: int) -> None:
        self.scroll_transcript_to(self.transcript_top() + delta)

    def scroll_transcript_to(self, row: int) -> None:
        maximum = self.transcript_max_scroll()
        nxt = min(maximum, max(0, row))
        self._transcript_anchor = None if nxt >= maximum else nxt
        self.application.invalidate()

    def _transcript_text(self):  # type: ignore[no-untyped-def]
        if self._reviewing:
            rendered = render_review(self._review_calls(), self._width())
            self._transcript_line_count = rendered.count("\n") + 1
            return to_formatted_text(ANSI(rendered))
        fragments, lines = self._renderer.render(self.state, self._width())
        self._transcript_line_count = lines
        return fragments

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
        width = self._width()
        first = _fit_footer_line(f" {self._workspace()}", "", width)
        left = f" {spinner}{self.state.status}{queue} · {mode} · perm:{perm} · {self.runner.thread_id[:8]}"
        model = self.runner.current_model()
        right = model_display_name(model) if model is not None else "fixed model"
        context = format_context_usage(self.state.usage, self.runner.context_window())
        git = self._git_summary.label()
        if not git:
            right = f"{right} · {context}" if context else right
            second = _fit_footer_line(left, right, width)
            return FormattedText([("class:footer", f"{first}\n{second}")])
        second = _fit_footer_line(left, right, width)
        third = _fit_footer_line(f" {git}", context, width)
        return FormattedText([("class:footer", f"{first}\n{second}\n{third}")])

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

        @kb.add("pageup")
        def page_up(event) -> None:  # type: ignore[no-untyped-def]
            self.scroll_transcript(-10)

        @kb.add("pagedown")
        def page_down(event) -> None:  # type: ignore[no-untyped-def]
            self.scroll_transcript(10)

        @kb.add("c-home")
        def scroll_top(event) -> None:  # type: ignore[no-untyped-def]
            self.scroll_transcript_to(0)

        @kb.add("c-end")
        def scroll_bottom(event) -> None:  # type: ignore[no-untyped-def]
            self.scroll_transcript_to(self.transcript_rows())

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
            elif self._reviewing:
                self._close_review()
            elif self.interaction is not None:
                self._finish_interaction(cancelled=True)
            elif self._compacting:
                self.set_status("Wait for compaction to finish")
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

        @bind("review_diff")
        def review_diff(event) -> None:  # type: ignore[no-untyped-def]
            self._toggle_review()

        @bind("reopen_interaction")
        def reopen_interaction(event) -> None:  # type: ignore[no-untyped-def]
            self._reopen_pending_interaction()

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
        if self._compacting:
            self.set_status("Wait for compaction to finish")
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
        else:
            await command.handler(self, arg.strip())
        self.application.invalidate()

    def _start_run(
        self,
        text: str,
        *,
        resume_call: Any | None = None,
        image_refs: tuple[ImageAttachmentRef, ...] = (),
    ) -> None:
        if resume_call is None:
            self.state.add_user(text, attachments=image_refs)
            self.state.attachments.clear()
        self.state.running = True
        self.set_status("Working…  Esc to cancel")

        async def work() -> None:
            if resume_call is None:
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
                try:
                    result = await self._run_blocking(resume_call)
                except Exception as exc:  # noqa: BLE001
                    self._apply_event(RunEvent(type="run_failed", content=str(exc)))
                    return
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
        if result.status == "paused":
            self.state.running = False
            self.state.add_system("Paused at a checkpoint. Submit /pause again is unnecessary; press Enter to resume.")
        if result.status in {"waiting_confirmation", "waiting_human", "paused"}:
            self._reopen_pending_interaction()
        self.application.invalidate()

    def _finish_interaction(self, *, cancelled: bool = False) -> None:
        interaction = self.interaction
        if interaction is None:
            return
        self._reviewing = False
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
                self._restore_queued_to_editor(self.buffer)
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
                profile = self._switch_model(model_id)
            except (KeyError, RuntimeError) as exc:
                self.state.add_system(str(exc), error=True)
                return
            self.state.add_system(f"Switched model to {model_display_name(profile)}")
            self.set_status(f"Model: {model_display_name(profile)}")
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
        if cancelled and interaction.kind in {"pause", "approval", "human"}:
            self._dismiss_pending_interrupt(interaction.kind)
            return
        if interaction.kind == "pause":
            if interaction.values.get("continue") == "continue":
                self.interaction = None
                self._start_run("", resume_call=partial(
                    self.runner.continue_run, on_event=self._on_event_thread,
                ))
                return
            self._dismiss_pending_interrupt("pause")
            return
        if interaction.kind == "approval":
            ids = [item for item in interaction.tool_call_ids if item]
            approved = str(interaction.values.get("approved") or "") == "approve"
            self.interaction = None
            if approved and len(ids) == 1:
                resume_call = partial(
                    self.runner.approve_tool, ids[0], on_event=self._on_event_thread,
                )
            elif approved:
                resume_call = partial(
                    self.runner._decide_listed_tools, ids, approved=True,
                    on_event=self._on_event_thread,
                )
            elif len(ids) == 1:
                resume_call = partial(
                    self.runner.reject_tool, ids[0], on_event=self._on_event_thread,
                )
            else:
                resume_call = partial(
                    self.runner._decide_listed_tools, ids, approved=False,
                    on_event=self._on_event_thread,
                )
            self._start_run("", resume_call=resume_call)
            return
        values = dict(interaction.values)
        self.interaction = None
        self._start_run("", resume_call=partial(
            self.runner.submit_human_input, values, on_event=self._on_event_thread,
        ))

    def _dismiss_pending_interrupt(self, kind: str) -> None:
        self.interaction = None
        self._reviewing = False
        label = {"approval": "Approval", "human": "Input", "pause": "Pause"}.get(kind, "Input")
        self.state.add_system(f"{label} still pending · F2 to decide")
        self.set_status("Ready")

    def _reopen_pending_interaction(self, *, notify_missing: bool = True) -> bool:
        try:
            interrupt = self.runner.current_interrupt()
        except UnknownInterruptError as exc:
            self.state.add_system(str(exc), error=True)
            self.set_status("Unknown interrupt")
            return False
        if interrupt is None:
            if notify_missing:
                self.state.add_system("No pending interaction")
                self.set_status("No pending interaction")
            return False
        if interrupt.kind is InterruptKind.WAITING_CONFIRMATION:
            self.interaction = InteractionController.approval(list(interrupt.pending_tools))
            self.set_status("Waiting for input")
        elif interrupt.kind is InterruptKind.WAITING_HUMAN:
            self.interaction = InteractionController.human(interrupt.payload)
            self.set_status("Waiting for input")
        elif interrupt.kind is InterruptKind.PAUSED:
            self.interaction = InteractionController.pause()
            self.set_status("Paused")
        else:
            self.state.add_system(f"Unsupported interrupt: {interrupt.kind}", error=True)
            return False
        self.application.invalidate()
        return True

    def _review_calls(self) -> list[tuple[str, dict[str, Any]]]:
        if self.interaction is not None and self.interaction.kind == "approval":
            found = [
                (str(call.get("name") or "tool"), call.get("args") if isinstance(call.get("args"), dict) else {})
                for call in self.interaction.calls
                if str(call.get("name") or "") in MUTATION_TOOLS
            ]
            if found:
                return found
        for block in reversed(self.state.blocks):
            if isinstance(block, ToolBlock) and block.name in MUTATION_TOOLS:
                return [(block.name, block.arguments)]
        return []

    def _toggle_review(self) -> None:
        if self._reviewing:
            self._close_review()
            return
        if not self._review_calls():
            self.set_status("No pending review")
            return
        self._reviewing = True
        self._transcript_anchor = 0
        self.set_status("Reviewing diff · Esc or Ctrl+R to close")

    def _close_review(self) -> None:
        self._reviewing = False
        self._transcript_anchor = None
        self.set_status("Waiting for input" if self.interaction is not None else "Ready")


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
