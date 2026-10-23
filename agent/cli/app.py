from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import partial
import logging
import os
import time
import warnings
from time import monotonic
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from prompt_toolkit import ANSI, Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText, to_formatted_text
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.named_commands import get_by_name
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import BufferControl, ConditionalContainer, Float, FloatContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import WindowAlign
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput
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
    copy_to_clipboard,
    windows_path_to_wsl,
)
from agent.cli.commands import Command, command_table
from agent.cli.gitinfo import REFRESH_SECONDS, GitProbe
from agent.cli.input import Keymap
from agent.cli.interactions import InteractionController
from agent.cli.pasted_content import PastedContentDraft
from agent.cli.skills import SkillDraft, load_skill_catalog
from agent.cli.previews import is_mutation_tool, normalize_file_mutation
from agent.cli.rendering import (
    RenderedUnit,
    TranscriptDocument,
    TranscriptRenderer,
    render_interaction,
    render_queued_messages,
    render_review,
)
from agent.cli.session_controller import SessionController
from agent.cli.state import CliState, ToolBlock
from agent.cli.working_messages import WorkingMessageRotation, load_working_messages
from agent.config import (
    DEFAULT_UI_DISPLAY_LIMITS, DEFAULT_UI_TIMEZONE, ModelProfile, parse_sandbox_mode,
    require_keybindings_outside_workspace,
)
from agent.permission import (
    PERMISSION_ALLOW_WARNING,
    PermissionMode,
    allow_mode_available,
    allow_mode_unavailable_reason,
    parse_permission_mode,
)
from agent.cli.lifecycle import AppLifecycle, TaskStop
from agent.cli.input_actions import (
    EXIT_WINDOW_SECONDS as CTRL_C_EXIT_WINDOW_SECONDS,
    InputAction,
    InputActionRouter,
    InputView,
)
from agent.performance import model_metric_groups
from agent.mcp.logging import MCPLogHandler
from agent.runner import AgentRunner, InterruptKind, RunEvent, RunResult, UnknownInterruptError

# Workspace/model and execution settings on the left; git/context on the right.
FOOTER_LINES = 2
INPUT_CLEARED_HINT = "Input cleared · press Ctrl+C again to exit"
STATUS_NOTICE_SECONDS = 1.0

_RUN_EPOCH: ContextVar[int | None] = ContextVar("deep_agent_tui_run_epoch", default=None)


def _call_in_run_epoch(epoch: int, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run a Runner operation in a context tagged with its run epoch.

    LangGraph copies the context into node and tool threads, so every event
    the operation emits carries the epoch back to the UI loop.
    """
    def call() -> Any:
        _RUN_EPOCH.set(epoch)
        return func(*args, **kwargs)
    return copy_context().run(call)


def format_workspace_path(path: Path) -> str:
    """Abbreviate the current user's home directory for display only."""
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        return str(path)
    return "~/" if relative == Path(".") else f"~/{relative}"


def format_context_window(window: int) -> str:
    """1000000 -> "1.0m", 200000 -> "200k"."""
    if window >= 1_000_000:
        return f"{window / 1_000_000:.1f}m"
    if window >= 1_000:
        return f"{round(window / 1_000)}k"
    return str(window)


def format_context_usage(usage: dict[str, int], window: int, *, empty_session: bool = False) -> str:
    """Right-hand footer label, empty when the profile declares no window."""
    if window <= 0:
        return ""
    label = f"{format_context_window(window)} Context"
    used = usage.get("input_tokens")
    if empty_session and not usage:
        used = 0
    if type(used) is not int or used < 0:
        return f"{label} · Unknown"
    percent = used / window * 100
    return f"{label} · {percent:.1f}% used"


def model_display_name(profile: ModelProfile) -> str:
    """Include the configured source when two providers expose the same model."""
    return f"{profile.source} · {profile.model}" if profile.source else profile.model


class SlashCompleter(Completer):
    def __init__(
        self,
        commands: tuple[Command, ...],
        *,
        model_profiles: Callable[[], list[ModelProfile]] | None = None,
        permission_modes: Callable[[], list[str]] | None = None,
        sandbox_modes: Callable[[], list[str]] | None = None,
    ) -> None:
        self.commands = commands
        self.model_profiles = model_profiles or (lambda: [])
        self.permission_modes = permission_modes or (lambda: [])
        self.sandbox_modes = sandbox_modes or (lambda: [])
        self.accepted_text: str | None = None

    @staticmethod
    def _rank(name: str, query: str) -> tuple[int, int, int] | None:
        if name == query:
            return (0, 0, len(name))
        if name.startswith(query):
            return (1, 0, len(name))
        offset = name.find(query)
        return (2, offset, len(name)) if offset >= 0 else None

    def get_completions(self, document, complete_event):  # type: ignore[no-untyped-def]
        before = document.text_before_cursor
        if "\n" in before or not before.startswith("/"):
            return
        if before == self.accepted_text:
            return
        if " " in before:
            name, arg = before[1:].split(" ", 1)
            if " " in arg:
                return
            yield from self._argument_completions(name, arg)
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

    def _argument_completions(self, name: str, arg: str) -> Iterator[Completion]:
        candidates: list[tuple[str, str]] = []
        if name == "model":
            profiles = self.model_profiles()
            sources = list(dict.fromkeys(profile.id.split("/", 1)[0]
                                         for profile in profiles if "/" in profile.id))
            candidates.extend((source, "Model source") for source in sources)
            candidates.extend((profile.id, f"Model: {model_display_name(profile)}") for profile in profiles)
        elif name == "permission":
            descriptions = {"ask": "Require approval for tools", "allow": "Auto-approve tools (SANDBOXED, HIGH RISK)"}
            candidates.extend((mode, descriptions[mode]) for mode in self.permission_modes())
        elif name == "sandbox":
            descriptions = {
                "restricted": "Workspace, skills, and declared mounts; system directories stay read-only",
                "full-access": "Write the real filesystem except the agent install and configuration (HIGH RISK)",
            }
            candidates.extend((mode, descriptions[mode]) for mode in self.sandbox_modes())
        elif name == "mcp":
            candidates.extend((
                ("enable", "Enable MCP and connect configured servers"),
                ("disable", "Disable MCP without deleting servers or tokens"),
                ("add", "Add a host MCP server"),
                ("remove", "Remove a configured server"),
                ("test", "Connect and list tools"),
                ("tools", "Show one server's tools"),
            ))
        ranked = []
        for index, (value, description) in enumerate(candidates):
            rank = self._rank(value.lower(), arg.lower())
            if rank is not None:
                ranked.append((rank, index, value, description))
        for _, _, value, description in sorted(ranked):
            yield Completion(value, start_position=-len(arg), display_meta=description)


def _iter_selection_marks(fragments: Any, left: float, right: float) -> Iterator[tuple[Any, str, bool]]:
    """Yield ``(fragment, char, marked)`` for cells overlapping ``[left, right)``.

    Zero-width characters (combining marks, ZWJ, variation selectors) share the
    cell of the preceding character, so they follow its marked state instead of
    being judged on their own empty span.
    """
    column = 0
    previous = False
    for fragment in fragments:
        if "[ZeroWidthEscape]" in fragment[0]:
            yield fragment, fragment[1], False
            continue
        for char in fragment[1]:
            width = max(0, get_cwidth(char))
            marked = previous if width == 0 else column < right and column + width > left
            yield fragment, char, marked
            previous = marked
            column += width


class _ScrollableTextControl(FormattedTextControl):
    """Formatted text that consumes clicks and routes wheel events to the transcript."""

    def __init__(self, *args: Any, on_scroll: Any = None, on_select_outside: Any = None, **kwargs: Any) -> None:
        self._on_scroll = on_scroll
        self._on_select_outside = on_select_outside
        super().__init__(*args, **kwargs)

    def mouse_handler(self, mouse_event):  # type: ignore[no-untyped-def]
        if self._on_select_outside is not None and self._on_select_outside(mouse_event):
            return None
        if self._on_scroll is not None:
            if mouse_event.event_type is MouseEventType.SCROLL_UP:
                self._on_scroll(-3)
                return None
            if mouse_event.event_type is MouseEventType.SCROLL_DOWN:
                self._on_scroll(3)
                return None
        return None


class _BackToBottomControl(_ScrollableTextControl):
    """Make the whole return-to-bottom hint clickable without stealing wheel events."""

    def __init__(self, *args: Any, on_click: Any, **kwargs: Any) -> None:
        self._on_click = on_click
        super().__init__(*args, **kwargs)

    def mouse_handler(self, mouse_event):  # type: ignore[no-untyped-def]
        if mouse_event.event_type is MouseEventType.MOUSE_DOWN and mouse_event.button is MouseButton.LEFT:
            self._on_click()
            return None
        return super().mouse_handler(mouse_event)


class _TranscriptControl(UIControl):
    """Expose cached transcript lines to prompt_toolkit without splitting them again."""

    def __init__(self, document: Any, cursor: Any, on_scroll: Any, on_select: Any, selected_line: Any) -> None:
        self._document = document
        self._cursor = cursor
        self._on_scroll = on_scroll
        self._on_select = on_select
        self._selected_line = selected_line

    def create_content(self, width: int, height: int) -> UIContent:
        document = self._document()
        return UIContent(
            get_line=lambda row: self._selected_line(row, document.get_line(row)),
            line_count=document.line_count,
            cursor_position=self._cursor(),
            show_cursor=False,
        )

    def preferred_height(self, width: int, max_available_height: int, wrap_lines: bool, get_line_prefix: Any) -> int:
        return min(max_available_height, self._document().line_count)

    def mouse_handler(self, mouse_event):  # type: ignore[no-untyped-def]
        if mouse_event.event_type is MouseEventType.SCROLL_UP:
            self._on_scroll(-3)
        elif mouse_event.event_type is MouseEventType.SCROLL_DOWN:
            self._on_scroll(3)
        elif mouse_event.event_type in {MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_MOVE, MouseEventType.MOUSE_UP}:
            self._on_select(mouse_event)
        return None


class _EditorScrollControl(BufferControl):
    """Input box: wheel scrolls the transcript instead of the empty editor."""

    def __init__(self, *args: Any, on_scroll: Any = None, on_select_outside: Any = None, **kwargs: Any) -> None:
        self._on_scroll = on_scroll
        self._on_select_outside = on_select_outside
        super().__init__(*args, **kwargs)

    def mouse_handler(self, mouse_event):  # type: ignore[no-untyped-def]
        if self._on_select_outside is not None and self._on_select_outside(mouse_event):
            return None
        if self._on_scroll is not None and mouse_event.event_type in {
            MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN,
        }:
            self._on_scroll(-3 if mouse_event.event_type is MouseEventType.SCROLL_UP else 3)
            return None
        return super().mouse_handler(mouse_event)


class _PastedContentProcessor(Processor):
    """为输入块标记添加样式，保持编辑文本和光标位置不变。"""

    def __init__(self, draft: PastedContentDraft | SkillDraft, style: str = "pasted-content") -> None:
        self._draft = draft
        self._style = style

    def apply_transformation(self, transformation_input: TransformationInput) -> Transformation:
        fragments = transformation_input.fragments
        plain = "".join(fragment[1] for fragment in fragments)
        ranges = [
            (index, index + len(label))
            for label in self._draft.labels
            if (index := plain.find(label)) >= 0
        ]
        if not ranges:
            return Transformation(fragments)
        styled = []
        index = 0
        for fragment in fragments:
            style, text, *rest = fragment
            for char in text:
                char_style = style + " class:" + self._style if any(
                    start <= index < end for start, end in ranges
                ) else style
                styled.append((char_style, char, *rest))
                index += 1
        return Transformation(styled)


class _TranscriptWindow(Window):
    """Transcript viewport whose scroll offset is authoritative, not cursor-derived.

    prompt_toolkit's scrollers only ever move ``vertical_scroll`` far enough to keep
    a cursor visible. Disguising the scroll anchor as a cursor therefore left the
    viewport stuck: ``max(previous_scroll, ...)`` blocked downward movement and
    ``min(..., get_max_vertical_scroll())`` blocked upward movement, so the
    viewport barely responded in either direction. This window computes the offset
    outright and skips the cursor-chasing arithmetic entirely.
    """

    def __init__(self, *args: Any, viewport: Any = None, scroll_offset: Any = None, **kwargs: Any) -> None:
        self._viewport = viewport
        self._scroll_offset = scroll_offset
        super().__init__(*args, **kwargs)

    def _scroll(self, ui_content: Any, width: int, height: int) -> None:
        self.horizontal_scroll = 0
        self.vertical_scroll_2 = 0
        maximum = max(0, ui_content.line_count - height)
        if self._scroll_offset is not None:
            self.vertical_scroll = self._scroll_offset(maximum)
        else:
            self.vertical_scroll = self._viewport.transcript_offset(maximum) if self._viewport is not None else maximum

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
        manager = getattr(runner, "mcp", None)
        mcp_config_dir = config_dir or (manager.path.parent if manager is not None else None)
        self._mcp_log_path = (
            mcp_config_dir / "logs" / f"mcp-{os.getpid()}.log" if mcp_config_dir is not None else None
        )
        if config_dir is not None:
            sandbox = runner.settings.sandbox if runner.settings is not None else runner._sandbox_config
            workspace = sandbox.workspace if sandbox is not None else Path.cwd()
            require_keybindings_outside_workspace(config_dir, workspace)
        _register_terminal_sequences()
        self.state = CliState()
        self.state.load_model_metrics(runner.model_metrics_history())
        self.state.context_pristine = not runner.thread_has_content()
        self.commands = command_table()
        self.command_by_name = {item.name: item for item in self.commands}
        keymap_path = (config_dir / "keybindings.json") if config_dir else None
        self.keymap = Keymap.load(keymap_path)
        self.interaction: InteractionController | None = None
        self._deferred_config_interaction: InteractionController | None = None
        self.sessions = SessionController(self)
        self._reviewing = False
        self.clipboard = ClipboardAdapter()
        self._io_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="deep-agent-io")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._run_task: asyncio.Task[None] | None = None
        # Each dispatched run gets an epoch; its Runner events carry it so a
        # late event of an earlier run cannot change the current run's UI.
        self._run_epoch = 0
        self._cancel_sent = False
        self._compacting = False
        self._input_actions = InputActionRouter()
        self._status_notice_handle: asyncio.TimerHandle | None = None
        self._status_before_notice = "Ready"
        self._status_notice: str | None = None
        self.lifecycle = AppLifecycle(self)
        self._transcript_line_count = 1
        # None = stick to bottom (follow new output); int = pinned scroll row.
        self._transcript_anchor: int | None = None
        self._queue_scroll = 0
        self._queue_preview_cache: tuple[Any, FormattedText, int] | None = None
        self._selection_start: tuple[int, int] | None = None
        self._selection_end: tuple[int, int] | None = None
        self._selection_dragging = False
        self._selection_edge = 0
        self._selection_edge_x = 0
        self._selection_scroll_step = 0
        self._selection_scroll_handle: asyncio.TimerHandle | None = None
        timezone_name = runner.settings.ui_timezone if runner.settings is not None else DEFAULT_UI_TIMEZONE
        display_limits = runner.settings.ui_display_limits if runner.settings is not None else DEFAULT_UI_DISPLAY_LIMITS
        self._renderer = TranscriptRenderer(ZoneInfo(timezone_name), limits=display_limits)
        self._git = GitProbe(self._workspace())
        self._git_summary = self._git.summary
        self._git_task: asyncio.Task[None] | None = None

        self.slash_completer = SlashCompleter(
            self.commands,
            model_profiles=self.runner.list_models,
            permission_modes=lambda: ["ask", "allow"] if self._allow_available() else ["ask"],
            sandbox_modes=lambda: ["restricted", "full-access"],
        )
        self.buffer = Buffer(
            multiline=True,
            history=InMemoryHistory(),
            completer=self.slash_completer,
            complete_while_typing=True,
        )
        self._pasted_content = PastedContentDraft()
        self._skill_draft = SkillDraft()
        self._approval_draft: tuple[Document, PastedContentDraft, SkillDraft] | None = None
        self._skill_picker_draft: tuple[str, int] | None = None
        self.transcript_control = _TranscriptControl(
            document=self._transcript_document,
            cursor=self._transcript_cursor,
            on_scroll=self.scroll_transcript,
            on_select=self._select_transcript,
            selected_line=self._selected_transcript_line,
        )
        self.interaction_control = _ScrollableTextControl(
            text=self._interaction_text, focusable=False, on_scroll=self.scroll_transcript,
            on_select_outside=self._select_below_transcript,
        )
        self.attachment_control = _ScrollableTextControl(
            text=self._attachment_text, focusable=False, on_scroll=self.scroll_transcript,
            on_select_outside=self._select_below_transcript,
        )
        self.footer_control = _ScrollableTextControl(
            text=self._footer_text, focusable=False, on_scroll=self.scroll_transcript,
            on_select_outside=self._select_below_transcript,
        )
        messages_path = config_dir / "working_messages.yaml" if config_dir else None
        self._working_messages = WorkingMessageRotation(load_working_messages(messages_path))
        self.status_control = _ScrollableTextControl(
            text=self._status_text, focusable=False, on_scroll=self.scroll_transcript,
            on_select_outside=self._select_below_transcript,
        )
        self.queue_control = _ScrollableTextControl(
            text=self._queue_text, focusable=False, on_scroll=self._scroll_queue,
        )
        self.queue_window = _TranscriptWindow(
            self.queue_control, scroll_offset=self._queue_offset,
            height=self._queue_height, wrap_lines=False, always_hide_cursor=True,
            dont_extend_height=True, style="class:status",
        )
        self.back_to_bottom_control = _BackToBottomControl(
            text=lambda: FormattedText([("class:back-to-bottom", "↓ Back to bottom · esc")])
            if self.interaction is None and self.transcript_away_from_bottom() else FormattedText([]),
            focusable=False,
            on_scroll=self.scroll_transcript,
            on_click=self.follow_transcript,
            on_select_outside=self._select_below_transcript,
        )
        self.editor_control = _EditorScrollControl(
            buffer=self.buffer, focusable=True, on_scroll=self.scroll_transcript,
            on_select_outside=self._select_below_transcript,
            input_processors=[
                _PastedContentProcessor(self._pasted_content),
                _PastedContentProcessor(self._skill_draft, "skill-block"),
            ],
        )
        self.bindings = self._create_bindings()

        interaction_visible = Condition(lambda: self.interaction is not None)
        editor_visible = Condition(lambda: self.interaction is None or self.interaction.accepts_text)
        attachments_visible = Condition(lambda: bool(self.state.attachments) and self.interaction is None)
        back_to_bottom_visible = Condition(self.transcript_away_from_bottom)

        def editor_height() -> Any:
            lines = max(1, self.buffer.document.line_count)
            try:
                rows = self.application.output.get_size().rows
            except Exception:  # noqa: BLE001
                rows = 30
            # Keep at least one input row even in an extremely short terminal.
            terminal_limit = max(1, rows // 3)
            maximum = min(display_limits.editor_max_lines, terminal_limit)
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
            # Rich already wraps each captured transcript line to the viewport.
            # Re-wrapping those padded lines here makes physical rows diverge
            # from _transcript_line_count, shifting the viewport and editor.
            wrap_lines=False,
            height=Dimension(min=3, weight=1),
            always_hide_cursor=True,
            viewport=self,
        )
        self.interaction_divider_window = Window(height=1, char="─", style="class:interaction-divider")
        self.interaction_bottom_divider_window = Window(height=1, char="─", style="class:interaction-divider")
        self.interaction_hint_window = Window(
            _BackToBottomControl(
                text=FormattedText([("class:back-to-bottom", "↓ Back to bottom · esc")]),
                focusable=False, on_scroll=self.scroll_transcript,
                on_click=self.follow_transcript, on_select_outside=self._select_below_transcript,
            ),
            height=1, dont_extend_height=True,
            align=WindowAlign.CENTER,
        )
        body = HSplit([
            self.transcript_window,
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
                self.interaction_hint_window,
                filter=back_to_bottom_visible & interaction_visible,
            ),
            ConditionalContainer(
                self.interaction_divider_window,
                filter=interaction_visible,
            ),
            ConditionalContainer(
                Window(
                    self.interaction_control,
                    # Rich already wraps this content at the viewport width.
                    # A second wrap invalidates interaction_height's row count.
                    wrap_lines=False,
                    height=interaction_height,
                    dont_extend_height=True,
                    always_hide_cursor=True,
                    style="class:interaction",
                ),
                filter=interaction_visible,
            ),
            ConditionalContainer(
                Window(
                    self.status_control,
                    height=1,
                    wrap_lines=False,
                    always_hide_cursor=True,
                    dont_extend_height=True,
                    style="class:status",
                ),
                filter=Condition(self._status_visible),
            ),
            ConditionalContainer(self.queue_window, filter=Condition(self._queue_expanded)),
            ConditionalContainer(
                self.interaction_bottom_divider_window,
                filter=interaction_visible | Condition(lambda: self.state.status == "Waiting for input"),
            ),
            ConditionalContainer(
                Window(
                    self.back_to_bottom_control,
                    height=1, dont_extend_height=True, align=WindowAlign.CENTER,
                ),
                filter=back_to_bottom_visible & ~interaction_visible,
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
            Window(
                self.footer_control,
                height=FOOTER_LINES,
                style="class:footer",
                dont_extend_height=True,
            ),
        ])
        self.application: Application[None] = Application(
            layout=Layout(FloatContainer(
                content=body,
                floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=display_limits.completion_menu_lines, scroll_offset=1))],
            ), focused_element=self.editor_control),
            key_bindings=self.bindings,
            full_screen=True,
            mouse_support=Condition(lambda: not self._exiting),
            style=Style.from_dict({
                "editor": "bg:#303030 #ffffff",
                "pasted-content": "bg:#245c38 #e8ffe8",
                "skill-block": "bg:#254b70 #e8f4ff bold",
                "transcript-selection": "bg:#264f78 #ffffff noreverse",
                "footer": "#858585",
                "footer-workspace": "ansigreen",
                "footer-resume-id": "ansicyan",
                "footer-model": "ansiyellow",
                "status": "ansicyan bold",
                "status-message": "#aaaaaa nobold",
                "interaction": "#d0d0d0",
                "attachments": "#72d5e8",
                "back-to-bottom": "bg:#163a5f #d7edff",
                "interaction-divider": "#3b82f6",
                "completion-menu": "bg:#15191d #d0d0d0",
                "completion-menu.completion.current": "bg:#3b5c73 #ffffff",
                "completion-menu.meta.completion.current": "bg:#3b5c73 #ffffff",
            }),
            input=input,
            output=output,
            refresh_interval=0.1,
        )
        self.application.ttimeoutlen = 0.05
        self.application.timeoutlen = 0.10
        if self.keymap.warning:
            self.state.add_system(self.keymap.warning, error=True)
        if self._working_messages.config.warning:
            self.state.add_system(self._working_messages.config.warning, error=True)
        if self.runner.on_event is None:
            self.runner.on_event = self._on_event_thread

    def run(self) -> None:
        asyncio.run(self.run_async())

    async def run_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._start_git_watch()
        with self._capture_runtime_logs():
            try:
                await self.sessions.start()
                await self.application.run_async(pre_run=self._start_mcp_connections)
            finally:
                await self.lifecycle.shutdown()

    def _start_mcp_connections(self) -> None:
        manager = getattr(self.runner, "mcp", None)
        if manager is None or not manager.enabled or not manager.list_servers():
            return
        from agent.cli.mcp_ui import _start_operation
        _start_operation(self, manager.connect_pending, quiet_success=True)

    @contextmanager
    def _capture_runtime_logs(self) -> Iterator[None]:
        """Keep MCP logs in a private background file, away from terminal/UI output."""
        if getattr(self, "_runtime_logs_active", False):
            yield
            return
        class TranscriptHandler(logging.Handler):
            def emit(handler_self, record: logging.LogRecord) -> None:
                if record.levelno < logging.WARNING:
                    return
                message = record.getMessage()
                if self._loop is not None and self._loop.is_running():
                    self._loop.call_soon_threadsafe(self._show_runtime_log, message)
                else:
                    self._show_runtime_log(message)

        handler = TranscriptHandler()
        mcp_handler: logging.Handler = logging.NullHandler()
        if self._mcp_log_path is not None:
            self._mcp_log_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            self._mcp_log_path.parent.chmod(0o700)
            mcp_handler = MCPLogHandler(
                self._mcp_log_path, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8",
            )
            mcp_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        previous_showwarning = warnings.showwarning

        def show_warning(message, category, filename, lineno, file=None, line=None):
            if file is not None:
                previous_showwarning(message, category, filename, lineno, file=file, line=line)
                return
            text = f"{category.__name__}: {str(message)[:2000]}"
            if any(part in str(filename).replace("\\", "/").split("/") for part in ("fastmcp", "mcp", "key_value")):
                logging.getLogger("mcp").warning(text)
                return
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._show_runtime_log, text)
            else:
                self._show_runtime_log(text)

        previous = []
        for name in ("deepagents.backends.filesystem", "fastmcp", "mcp"):
            logger = logging.getLogger(name)
            previous.append((logger, logger.handlers[:], logger.propagate))
            logger.handlers = [handler if name == "deepagents.backends.filesystem" else mcp_handler]
            logger.propagate = False
        try:
            self._runtime_logs_active = True
            with warnings.catch_warnings():
                warnings.showwarning = show_warning
                yield
        finally:
            self._runtime_logs_active = False
            for logger, handlers, propagate in previous:
                logger.handlers = handlers
                logger.propagate = propagate
            handler.close()
            mcp_handler.close()

    def _show_runtime_log(self, message: str) -> None:
        self.state.add_system(message)
        self.application.invalidate()

    def _start_git_watch(self) -> None:
        if self._git_task is not None:
            return
        self._git_task = self.lifecycle.spawn(self._watch_git(), stop=TaskStop.CANCEL, name="git-watch")

    async def _watch_git(self) -> None:
        """Keep the footer's git summary fresh without ever blocking a render pass."""
        while True:
            summary = await self._git.refresh()
            if summary != self._git_summary:
                self._git_summary = summary
                self.application.invalidate()
            await asyncio.sleep(REFRESH_SECONDS)

    async def _stop_git_watch(self) -> None:
        task = self._git_task
        if task is None:
            return
        try:
            if not task.done():
                task.cancel()
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._git_task = None

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
            f"Or run deep-agent resume and select. Model: {model_name} · {self.runner.reasoning_effort()}"
        )

    def set_status(self, text: str, *, transient: bool = True) -> None:
        """临时提示显示一秒后恢复原状态，持续状态由调用方显式指定。"""
        previous = self.state.status
        if self._status_notice == previous:
            previous = self._status_before_notice
        if previous == "Reading clipboard…" or (
            self.interaction is None and previous.startswith(("Select ", "Type ALLOW", "Reviewing diff"))
        ):
            previous = "Working…  Esc to cancel" if self.state.running else "Ready"
        if self._status_notice_handle is not None:
            self._status_notice_handle.cancel()
            self._status_notice_handle = None
        self._status_notice = None
        self.state.status = text
        if transient:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = self._loop
            if loop is not None and loop.is_running():
                self._status_before_notice = previous
                self._status_notice = text
                self._status_notice_handle = loop.call_later(
                    CTRL_C_EXIT_WINDOW_SECONDS if text == INPUT_CLEARED_HINT else STATUS_NOTICE_SECONDS,
                    self._hide_status_notice,
                )
        self.application.invalidate()

    def _show_input_cleared_hint(self) -> None:
        """空闲时短暂提示输入已清空，执行和待处理状态保持原样。"""
        if (self.state.running or self._compacting or self.state.compacting
                or self.state.status == "Waiting for input"
                or self.state.status.endswith("still pending · F2 to decide")):
            self.application.invalidate()
            return
        self.set_status(INPUT_CLEARED_HINT)

    def _hide_status_notice(self) -> None:
        """仅恢复仍有效的提示，避免覆盖后来出现的运行或审批状态。"""
        self._status_notice_handle = None
        notice = self._status_notice
        self._status_notice = None
        if self.state.status == notice:
            previous = self._status_before_notice
            if previous == "Compacting context" and not (self._compacting or self.state.compacting):
                previous = "Ready"
            self.set_status(previous, transient=False)

    def _switch_model(self, id_or_prefix: str, *, reasoning_effort: str | None = None) -> ModelProfile:
        profile = self.runner.request_model_change(id_or_prefix, reasoning_effort=reasoning_effort)
        interrupt = self.runner.current_interrupt()
        if not self.state.running and interrupt is None:
            events: list[RunEvent] = []
            self.runner._apply_pending_runtime_config(events.append)
            for event in events:
                self._apply_event(event, announce=False)
            if any(event.type == "runtime_config_failed" for event in events):
                self.set_status("Model switch failed")
                return profile
        if self.runner.pending_model() is None and self.runner.pending_reasoning_effort() is None:
            notice = f"Model: {model_display_name(profile)} · {self.runner.reasoning_effort()}"
        else:
            notice = f"Model queued: {model_display_name(profile)} · {self.runner.pending_reasoning_effort() or self.runner.reasoning_effort()}"
        if interrupt is not None and interrupt.kind is InterruptKind.WAITING_CONFIRMATION:
            notice += " · F2: pending approval"
        self.set_status(notice)
        return profile

    def _request_permission_mode(self, mode: PermissionMode) -> None:
        before = self.runner.permission_mode()
        self.runner.request_permission_change(mode)
        interrupt = self.runner.current_interrupt()
        if not self.state.running and interrupt is None and self.runner._operation_lock.acquire(blocking=False):
            events: list[RunEvent] = []
            try:
                self.runner._apply_pending_runtime_config(events.append)
            finally:
                self.runner._operation_lock.release()
            for event in events:
                self._apply_event(event, announce=False)
            if any(event.type == "runtime_config_failed" for event in events):
                self.set_status(f"Permission switch failed; permission remains {self.runner.permission_mode().value}.")
                return
        if self.runner.pending_permission_mode() is None:
            notice = f"Permission: {self.runner.permission_mode().value}."
        else:
            notice = f"Permission switch queued: {before.value} → {mode.value}. Current work uses {before.value}."
        if interrupt is not None and interrupt.kind is InterruptKind.WAITING_CONFIRMATION:
            notice += " Existing approval remains under ask; press F2 to approve or reject."
        self.state.add_system(notice)
        self.set_status(notice)

    def _request_sandbox_mode(self, mode: str) -> None:
        before = self.runner.sandbox_mode()
        try:
            self.runner.request_sandbox_mode(mode)
        except ValueError as exc:
            self.state.add_system(str(exc), error=True)
            return
        interrupt = self.runner.current_interrupt()
        if not self.state.running and interrupt is None and self.runner._operation_lock.acquire(blocking=False):
            events: list[RunEvent] = []
            try:
                self.runner._apply_pending_runtime_config(events.append)
            finally:
                self.runner._operation_lock.release()
            for event in events:
                self._apply_event(event, announce=False)
            if any(event.type == "runtime_config_failed" for event in events):
                self.set_status(f"Sandbox switch failed; filesystem mode remains {self.runner.sandbox_mode()}.")
                return
        if self.runner.pending_sandbox_mode() is None:
            notice = f"Sandbox filesystem: {self.runner.sandbox_mode()}."
        else:
            notice = f"Sandbox switch queued: {before} → {mode}. Current work uses {before}."
        self.state.add_system(notice)
        self.set_status(notice)

    def show_help(self) -> None:
        commands = "\n".join(
            f"/{item.name:<9} {item.description}"
            for item in self.commands
        )
        self.state.add_system(
            "Keyboard\n"
            "Enter submit · Ctrl+J newline · Alt+Enter follow-up · Esc cancel+restore · "
            "PgUp/PgDn scroll · Ctrl+P next model · Alt+P prev model · Alt+Up restore queue · "
            "Ctrl+O tools · Ctrl+R review · F2 pending · Ctrl+T thinking · Ctrl+B messages · "
            "Mouse drag selects and copies across history · "
            "Ctrl+V/Alt+V paste image/text · "
            "Ctrl+C clear/exit · Ctrl+D exit\n\n"
            f"Commands\n{commands}"
        )

    def show_status(self) -> None:
        prepared = self.runner.prepared
        tool_names = ", ".join(prepared.model_tool_names)
        tool_count = len(prepared.model_tool_names)
        tool_line = f"Tools: {tool_count}" + (f" · {tool_names}" if tool_names else "")
        model = self.runner.current_model()
        pending_model = self.runner.pending_model()
        model_line = (
            f"Model: {model_display_name(model)} · reasoning: {self.runner.reasoning_effort()}"
            + (f" → {model_display_name(pending_model)} (pending)" if pending_model else "")
            + f"\nInputs: {', '.join(model.input)}\n"
            if model is not None else "Model: (fixed)\n"
        )
        pending_effort = self.runner.pending_reasoning_effort()
        if pending_effort is not None:
            model_line += f"Reasoning: {self.runner.reasoning_effort()} → {pending_effort} (pending)\n"
        self.state.add_system(
            f"Status: {'running' if self.state.running else 'idle'}\n"
            f"{model_line}"
            f"{tool_line}\n"
            f"{self._model_metrics_lines()}"
        )

    def _model_metrics_lines(self) -> str:
        groups = model_metric_groups(self.state.model_metrics_history)
        if not groups:
            profile = self.runner.current_model()
            groups = model_metric_groups([{
                "model": profile.model if profile is not None else "unknown",
                "reasoning_effort": self.runner.reasoning_effort(),
            }])
        sections = []
        for group in groups:
            model = group["model"]
            effort = group["reasoning_effort"]
            label = f"{model}-{effort}" if effort != "default" else model
            lines = [label]
            for name, value in (
                ("5% low TPS", group["tps_low"]),
                ("5% top TPS", group["tps_top"]),
                ("Average TPS", group["tps_average"]),
            ):
                lines.append(f"  {name}: " + (f"{value:.1f}" if value is not None else "--"))
            sections.append("\n".join(lines))
        return "\n\n".join(sections)

    def show_session(self) -> None:
        store = self.runner.session_store
        model = self.runner.current_model()
        model_line = (
            f"Model: {model_display_name(model)} · reasoning: {self.runner.reasoning_effort()}\n" if model is not None else ""
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
        self.sessions.new_session()

    async def resume_session(self, arg: str = "") -> None:
        await self.sessions.resume(arg)

    def _apply_session_snapshot(self, snapshot: Any) -> None:
        self.sessions.apply_snapshot(snapshot)

    async def select_skill(self, arg: str = "") -> None:
        if self.sessions.waiting or self.interaction is not None or self._compacting:
            self.set_status("Finish the current interaction before selecting a skill")
            return
        if arg:
            self.state.add_system("Usage: /skill", error=True)
            return
        prepared = self.runner.prepared
        try:
            catalog = await self._run_blocking(load_skill_catalog, prepared)
        except (OSError, ValueError, RuntimeError) as exc:
            self.state.add_system(f"Cannot load skills: {exc}", error=True)
            return
        if (self.sessions.waiting or self.interaction is not None or self._compacting
                or self._exiting or self.runner.prepared is not prepared):
            return
        for warning in catalog.get("skills_load_errors", []):
            self.state.add_system(str(warning), error=True)
        skills = sorted(catalog.get("skills_metadata", []), key=lambda item: item["name"])
        if not skills:
            self.state.add_system("No valid skills in ~/.deep-agent/skills/ (SKILL.md requires name and description)")
            self.set_status("No skills available")
            return
        self._skill_picker_draft = (self.buffer.text, self.buffer.cursor_position)
        self.buffer.reset()
        self.interaction = InteractionController(
            kind="skill", title="Select skill", question="Choose a skill for the next message",
            fields=[{"id": "skill", "type": "single_select", "label": "Skill", "required": True,
                     "options": [{"value": item["path"], "label": item["name"],
                                  "description": item["description"]} for item in skills]}],
        )
        self.set_status("Select a skill · Enter confirm · Esc cancel", transient=False)
        self.application.invalidate()

    def _clear_skill_draft(self) -> None:
        self.buffer.text = self._skill_draft.expand(self.buffer.text)
        self._skill_draft.clear()
        self._skill_picker_draft = None

    async def select_model(self, arg: str = "") -> None:
        if self.sessions.waiting:
            self.set_status(self.sessions.wait_status(), transient=False)
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
            if self.state.running:
                self.set_status("Finish the current run before selecting a model")
                return
            if arg.strip():
                groups = {item.id.split("/", 1)[0] for item in profiles if "/" in item.id}
                if arg.strip() in groups:
                    self._show_model_choices(arg.strip(), profiles)
                    return
                profile = self.runner.settings.get_profile(arg.strip())
                self._show_reasoning_choices(profile, profile.source)
                return
            if any("/" in item.id for item in profiles):
                self._show_model_sources(profiles)
                return
            self._show_model_choices("", profiles)
        except (KeyError, RuntimeError, ValueError) as exc:
            self.state.add_system(str(exc), error=True)

    def _show_model_sources(self, profiles: list[ModelProfile]) -> None:
        current = self.runner.current_model()
        current_source = current.id.split("/", 1)[0] if current and "/" in current.id else ""
        sources = list(dict.fromkeys(item.id.split("/", 1)[0] for item in profiles))
        options = [
            {"value": source, "label": source + (" · current" if source == current_source else "")}
            for source in sources
        ]
        self.interaction = InteractionController(
            kind="model_source", title="Select model source", question="Choose a model source",
            fields=[{"id": "source", "type": "single_select", "label": "Source", "required": True, "options": options}],
        )
        if current_source in sources:
            self.interaction.option_index = sources.index(current_source)
        self.set_status("Select a source · Enter confirm · Esc cancel", transient=False)
        self.application.invalidate()

    def _show_model_choices(self, source: str, profiles: list[ModelProfile]) -> None:
        if source:
            profiles = [item for item in profiles if item.id.startswith(f"{source}/")]
        if not profiles:
            raise KeyError(f"Unknown model source: {source}")
        current = self.runner.current_model()
        current_id = current.id if current else ""
        options = []
        for item in profiles:
            name = item.model if source else model_display_name(item)
            options.append({
                "value": item.id,
                "label": name + (" · current" if item.id == current_id else ""),
                "description": "",
            })
        self.interaction = InteractionController(
            kind="model", title=f"Select model · {source}" if source else "Select model",
            question="Choose a model", values={"source": source} if source else {},
            fields=[{"id": "model", "type": "single_select", "label": "Model", "required": True, "options": options}],
        )
        for index, option in enumerate(options):
            if option["value"] == current_id:
                self.interaction.option_index = index
                break
        self.set_status("Select a model · Enter confirm · Esc back" if source else "Select a model · Enter confirm · Esc cancel", transient=False)
        self.application.invalidate()

    def _show_reasoning_choices(self, profile: ModelProfile, source: str = "") -> None:
        current = self.runner.current_model()
        current_effort = self.runner.reasoning_effort() if current and current.id == profile.id else None
        selected = current_effort or "default"
        pending = self.runner.pending_model() or current
        if pending and pending.id == profile.id and self.runner.pending_reasoning_effort() is not None:
            selected = self.runner.pending_reasoning_effort()
        efforts = list(profile.reasoning_efforts) or ["default"]
        self.interaction = InteractionController(
            kind="model_reasoning", title=f"Reasoning effort · {model_display_name(profile)}",
            question="Choose reasoning effort",
            values={"model": profile.id, "source": source},
            fields=[{"id": "effort", "type": "single_select", "label": "Reasoning effort",
                     "required": True, "options": [
                         {"value": effort, "label": effort + (" · current" if effort == current_effort else ""),
                          "description": "Use model default" if effort == "default" else ""}
                         for effort in efforts
                     ]}],
        )
        self.interaction.option_index = efforts.index(selected) if selected in efforts else 0
        self.set_status("Select reasoning effort · Enter confirm · Esc back", transient=False)
        self.application.invalidate()

    async def compact_command(self, arg: str = "") -> None:
        if arg.strip():
            self.state.add_system("Usage: /compact", error=True)
            return
        if self.state.running or self.interaction is not None:
            self.set_status("Finish the current run or interaction before compacting")
            return
        if self.sessions.waiting:
            self.set_status(self.sessions.wait_status(), transient=False)
            return
        self._compacting = True
        self.state.running = True
        self.set_status("Compacting context", transient=False)
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
            self.state.add_system(result.message)
            self.set_status("Context compacted")
        else:
            self.state.add_system(result.message or "Context compaction failed", error=True)
            self.set_status("Compaction failed")

    async def select_permission(self, arg: str = "") -> None:
        if self.sessions.waiting:
            self.set_status(self.sessions.wait_status(), transient=False)
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
        if self.state.running:
            self.set_status("Use /permission ask|allow while a run is active")
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
                "label": "allow · auto-approve all tools; sandbox network open (SANDBOXED, HIGH RISK)"
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
        self.set_status("Select permission mode · Enter confirm · Esc cancel", transient=False)
        self.application.invalidate()

    async def select_sandbox(self, arg: str = "") -> None:
        if self.sessions.waiting:
            self.set_status(self.sessions.wait_status(), transient=False)
            return
        if self.interaction is not None:
            self.set_status("Finish the current interaction before changing sandbox mode")
            return
        raw = arg.strip()
        if raw:
            mode = parse_sandbox_mode(raw)
            if mode is None:
                self.state.add_system(
                    "Unknown sandbox mode. Use /sandbox restricted or /sandbox full-access",
                    error=True,
                )
                return
            self._request_sandbox_mode(mode)
            return
        if self.state.running:
            self.set_status("Use /sandbox restricted|full-access while a run is active")
            return
        current = self.runner.sandbox_mode()
        options = [
            {
                "value": "restricted",
                "label": "restricted · workspace, skills, and declared mounts; system directories stay read-only"
                + (" · current" if current == "restricted" else ""),
            },
            {
                "value": "full-access",
                "label": "full-access · write the real filesystem except the agent install and configuration (HIGH RISK)"
                + (" · current" if current == "full-access" else ""),
            },
        ]
        self.interaction = InteractionController(
            kind="sandbox",
            title="Sandbox filesystem",
            question="Choose which files execute and file tools can change. ask/allow is unchanged.",
            fields=[{
                "id": "mode",
                "type": "single_select",
                "label": "Mode",
                "required": True,
                "options": options,
            }],
        )
        self.interaction.option_index = 0 if current == "restricted" else 1
        self.set_status("Select sandbox mode · Enter confirm · Esc cancel", transient=False)
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
        if (mode is PermissionMode.ALLOW and self.runner.permission_mode() is not PermissionMode.ALLOW
                and self.runner.pending_permission_mode() is not PermissionMode.ALLOW):
            self._begin_allow_permission_confirm()
            return
        try:
            self._request_permission_mode(mode)
        except (RuntimeError, ValueError) as exc:
            self.state.add_system(str(exc), error=True)
            return

    def _begin_allow_permission_confirm(self) -> None:
        self.interaction = InteractionController(
            kind="permission_confirm",
            title="Enable ALLOW? (HIGH RISK)",
            question=PERMISSION_ALLOW_WARNING,
            fields=[{
                "id": "confirm",
                "type": "text",
                "label": "Type ALLOW to confirm",
                "required": True,
                "options": [],
            }],
        )
        self._show_allow_confirm_status()
        self.application.invalidate()

    def _allow_confirm_open(self) -> bool:
        return self.interaction is not None and self.interaction.kind == "permission_confirm"

    def _current_interrupt_or_none(self) -> Any:
        try:
            return self.runner.current_interrupt()
        except UnknownInterruptError:
            return None

    def _show_allow_confirm_status(self) -> None:
        status = "Type ALLOW to confirm · Esc cancel"
        interrupt = self._current_interrupt_or_none()
        kind = interrupt.kind if interrupt is not None else None
        if kind is InterruptKind.WAITING_CONFIRMATION:
            status += " · approval pending · F2"
        elif kind is InterruptKind.WAITING_HUMAN:
            status += " · question pending · F2"
        elif kind is InterruptKind.PAUSED:
            status += " · pause pending · F2"
        self.set_status(status, transient=False)

    def _note_interrupt_behind_allow_confirm(self, status: str) -> None:
        if status == "waiting_confirmation":
            notice = "Tool approval is waiting. This ALLOW prompt stays open; F2 opens the approval and Esc returns here."
        elif status == "waiting_human":
            notice = "A question is waiting. This ALLOW prompt stays open; F2 opens it and Esc returns here."
        else:
            notice = "A pause is waiting. This ALLOW prompt stays open; F2 opens it and Esc returns here."
        self.state.add_system(notice)
        self._show_allow_confirm_status()

    def cycle_model(self, *, delta: int = 1) -> None:
        if self.sessions.waiting:
            self.set_status(self.sessions.wait_status(), transient=False)
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
        current = self.runner.pending_model() or self.runner.current_model()
        current_id = current.id if current else profiles[0].id
        index = next((i for i, item in enumerate(profiles) if item.id == current_id), 0)
        nxt = profiles[(index + delta) % len(profiles)]
        try:
            self._switch_model(nxt.id)
        except (KeyError, RuntimeError) as exc:
            self.state.add_system(str(exc), error=True)
            return
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
        self._clear_skill_draft()
        self.clear_transcript_selection()
        self.state.load_transcript(snapshot.transcript)
        self.state.load_model_metrics(snapshot.model_metrics_history)
        self.interaction = None
        self._reviewing = False
        self.state.add_system(
            f"Resumed session {snapshot.info.id} (last run: {snapshot.info.last_run_status.value})"
        )
        for notice in snapshot.notices:
            self.state.add_system(notice)
        if not self._reopen_pending_interaction(notify_missing=False):
            self.set_status("Ready", transient=False)
        self._transcript_anchor = None
        self._renderer.clear()
        self.state.usage = dict(self.runner.latest_usage())
        self.application.invalidate()

    @property
    def _exiting(self) -> bool:
        return self.lifecycle.exiting

    @property
    def _last_ctrl_c(self) -> float | None:
        """Double-press clock. Owned by InputActionRouter; exposed for existing checks."""
        return self._input_actions.last_ctrl_c

    def _input_view(self, buffer: Buffer) -> InputView:
        text = buffer.text
        interaction = self.interaction
        return InputView(
            has_interaction=interaction is not None,
            custom_entry=bool(interaction is not None and interaction.custom_entry),
            completion_open=buffer.complete_state is not None,
            slash_line=text.startswith("/") and "\n" not in text,
            transcript_away=self.transcript_away_from_bottom(),
            session_waiting=self.sessions.waiting,
            reviewing=self._reviewing,
            compacting=self._compacting,
            running=self.state.running,
            cancel_sent=self._cancel_sent,
            mcp_busy=getattr(self, "_mcp_busy", False),
            buffer_empty=not text,
            exiting=self._exiting,
        )

    def apply_input_action(self, action: InputAction, buffer: Buffer) -> None:
        """Perform one decision from InputActionRouter. Does not choose another."""
        if action is InputAction.NONE:
            return
        if action is InputAction.DISMISS_COMPLETION:
            # Dismiss the menu without cancel_completion restoring its pre-selection document.
            buffer.complete_state = None
            self.application.invalidate()
            return
        if action is InputAction.CLEAR_SLASH_LINE:
            self.slash_completer.accepted_text = None
            buffer.reset()
            self._pasted_content.clear()
            self._skill_draft.clear()
            self.application.invalidate()
            return
        if action is InputAction.FOLLOW_TRANSCRIPT:
            self.follow_transcript()
            return
        if action is InputAction.CANCEL_COMPLETION:
            buffer.cancel_completion()
            return
        if action is InputAction.CANCEL_SESSION_WAIT:
            self.sessions.cancel_wait()
            return
        if action is InputAction.CLOSE_REVIEW:
            self._close_review()
            return
        if action is InputAction.EXIT_CUSTOM_ENTRY:
            assert self.interaction is not None
            self.interaction.custom_entry = False
            self.interaction.error = ""
            buffer.reset()
            self._pasted_content.clear()
            self._skill_draft.clear()
            self.application.invalidate()
            return
        if action is InputAction.CANCEL_INTERACTION:
            self._finish_interaction(cancelled=True)
            return
        if action is InputAction.WAIT_FOR_COMPACTION:
            self.set_status("Wait for compaction to finish")
            return
        if action is InputAction.CANCEL_ALREADY_SENT:
            self.set_status("Cancelling… waiting for the run to stop", transient=False)
            self.application.invalidate()
            return
        if action is InputAction.CANCEL_RUN:
            restored = self._restore_queued_to_editor(buffer)
            self._cancel_sent = True
            self.runner.request_cancel()
            if restored:
                self.set_status("Cancelling… · queued messages restored", transient=False)
            else:
                self.set_status("Cancelling…", transient=False)
            self.application.invalidate()
            return
        if action is InputAction.MCP_STILL_RUNNING:
            self.set_status(
                "MCP is still running in the background; wait for completion or timeout",
                transient=False,
            )
            self.application.invalidate()
            return
        if action is InputAction.CLEAR_INPUT:
            buffer.reset()
            self._pasted_content.clear()
            self._skill_draft.clear()
            self.state.attachments.clear()
            self._show_input_cleared_hint()
            return
        if action is InputAction.REQUEST_EXIT:
            self.exit()
            return
        if action is InputAction.DELETE_AFTER_CURSOR:
            buffer.delete()
            return
        raise AssertionError(f"Unhandled input action: {action}")

    def request_command_exit(self) -> None:
        """`/exit` uses the same exit action as an empty Ctrl+D."""
        self.apply_input_action(self._input_actions.decide_exit_command(), self.buffer)

    def exit(self) -> None:
        """Ask the lifecycle to leave. Resource shutdown happens after the prompt loop."""
        self.lifecycle.request_exit()

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
        return max(20, columns)

    def _transcript_cursor(self) -> Point:
        last = max(0, self._transcript_line_count - 1)
        return Point(x=0, y=min(last, self.transcript_top()))

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

    def transcript_offset(self, maximum: int) -> int:
        """Resolve a painted viewport's offset and restore following at its tail."""
        anchor = self._transcript_anchor
        if anchor is None or anchor >= maximum:
            self._transcript_anchor = None
            return maximum
        return max(0, anchor)

    def transcript_away_from_bottom(self) -> bool:
        """Only offer the return action while the transcript tail is off screen."""
        return self._transcript_anchor is not None and self.transcript_top() < self.transcript_max_scroll()

    def follow_transcript(self) -> None:
        self._transcript_anchor = None
        self.application.invalidate()

    def scroll_transcript(self, delta: int) -> None:
        self.scroll_transcript_to(self.transcript_top() + delta)

    def scroll_transcript_to(self, row: int) -> None:
        maximum = self.transcript_max_scroll()
        nxt = min(maximum, max(0, row))
        self._transcript_anchor = None if nxt >= maximum else nxt
        self.application.invalidate()

    def _select_transcript(self, event: Any) -> None:
        # prompt_toolkit's Window already maps the click to (document row,
        # character index); only the index still needs converting to cells.
        row = event.position.y
        self._update_selection(row, self._selection_column(row, event.position.x), event.event_type, event.button)

    def _selection_column(self, row: int, index: int) -> int:
        document = self._transcript_document()
        if not 0 <= row < document.line_count:
            return index
        column = 0
        remaining = index
        for fragment in document.get_line(row):
            if "[ZeroWidthEscape]" in fragment[0]:
                continue
            for char in fragment[1]:
                if remaining <= 0:
                    return column
                column += max(0, get_cwidth(char))
                remaining -= 1
        return column

    def _update_selection(self, row: int, column: int, event_type: Any, button: Any) -> None:
        position = (row, column)
        if event_type is MouseEventType.MOUSE_DOWN and button is MouseButton.LEFT:
            self._stop_selection_scroll()
            self._selection_start = self._selection_end = position
            self._selection_dragging = True
            # Reinforce mouse reporting so IDE terminals drop their native
            # selection overlay instead of stacking it on ours.
            try:
                self.application.output.enable_mouse_support()
            except Exception:  # noqa: BLE001
                pass
        elif event_type is MouseEventType.MOUSE_MOVE and self._selection_dragging:
            if button is MouseButton.NONE:
                # The MOUSE_UP never arrived (released outside the terminal window
                # or over a control without a selection handler). Plain motion must
                # not keep dragging, or the 1003 any-motion events spin the UI.
                self._selection_dragging = False
                self._stop_selection_scroll()
            else:
                self._selection_end = position
                top = self.transcript_top()
                bottom = top + self.transcript_viewport_rows() - 1
                edge = -1 if position[0] <= top else (1 if position[0] >= bottom else 0)
                self._selection_edge_x = position[1]
                self._set_selection_edge(edge)
        elif event_type is MouseEventType.MOUSE_UP and self._selection_dragging:
            self._selection_end = position
            self._selection_dragging = False
            self._stop_selection_scroll()
            selected = self._selected_transcript_text()
            if selected:
                self.lifecycle.spawn(self._copy_selection(selected), stop=TaskStop.CANCEL, name="clipboard-copy")
        else:
            return
        self.application.invalidate()

    def _select_below_transcript(self, event: Any) -> bool:
        if not self._selection_dragging or event.event_type not in {
            MouseEventType.MOUSE_MOVE, MouseEventType.MOUSE_UP,
        }:
            return False
        bottom = self.transcript_top() + self.transcript_viewport_rows() - 1
        self._update_selection(bottom, event.position.x, event.event_type, event.button)
        return True

    def _set_selection_edge(self, edge: int) -> None:
        if edge != self._selection_edge:
            self._selection_scroll_step = 0
        self._selection_edge = edge
        if edge == 0:
            self._stop_selection_scroll()
        elif self._selection_scroll_handle is None:
            # Scroll immediately, then keep accelerating while the pointer stays
            # pinned to the viewport edge.
            self._scroll_selection_edge()

    def _scroll_selection_edge(self) -> None:
        self._selection_scroll_handle = None
        if not self._selection_dragging or not self._selection_edge:
            return
        previous = self.transcript_top()
        self._selection_scroll_step = min(12, max(3, self._selection_scroll_step + 1))
        self.scroll_transcript(self._selection_edge * self._selection_scroll_step)
        current = self.transcript_top()
        if current == previous:
            return
        row = current if self._selection_edge < 0 else current + self.transcript_viewport_rows() - 1
        self._selection_end = (row, self._selection_edge_x)
        self._selection_scroll_handle = asyncio.get_running_loop().call_later(0.04, self._scroll_selection_edge)

    def _stop_selection_scroll(self) -> None:
        self._selection_edge = 0
        self._selection_scroll_step = 0
        if self._selection_scroll_handle is not None:
            self._selection_scroll_handle.cancel()
            self._selection_scroll_handle = None

    def clear_transcript_selection(self) -> None:
        self._stop_selection_scroll()
        self._selection_dragging = False
        self._selection_start = self._selection_end = None
        self.application.invalidate()

    def _selection_bounds(self) -> tuple[tuple[int, int], tuple[int, int]] | None:
        if self._selection_start is None or self._selection_end is None:
            return None
        start, end = sorted((self._selection_start, self._selection_end))
        return (start, end) if start != end else None

    def _selection_span(self, row: int, bounds: tuple[tuple[int, int], tuple[int, int]]) -> tuple[float, float]:
        # The cell under the pointer is part of the selection: clicks right of a
        # line's text resolve to its last character, which must still be copied.
        left = bounds[0][1] if row == bounds[0][0] else 0
        right = bounds[1][1] + 1 if row == bounds[1][0] else float("inf")
        return left, right

    def _selected_transcript_line(self, row: int, fragments: Any) -> Any:
        bounds = self._selection_bounds()
        if bounds is None or not bounds[0][0] <= row <= bounds[1][0]:
            return fragments
        left, right = self._selection_span(row, bounds)
        return [
            (f"{style} class:transcript-selection" if marked else style, char, *rest)
            for (style, _text, *rest), char, marked in _iter_selection_marks(fragments, left, right)
        ]

    def _selected_transcript_text(self) -> str:
        bounds = self._selection_bounds()
        if bounds is None:
            return ""
        document = self._transcript_document()
        lines = []
        for row in range(bounds[0][0], min(bounds[1][0], document.line_count - 1) + 1):
            left, right = self._selection_span(row, bounds)
            chars = [char for _, char, marked in _iter_selection_marks(document.get_line(row), left, right) if marked]
            lines.append("".join(chars).rstrip())
        return "\n".join(lines).strip("\n")

    async def _copy_selection(self, content: str) -> None:
        try:
            await self._run_blocking(copy_to_clipboard, content, output=self.application.output)
        except ClipboardError as exc:
            self.set_status(f"Could not copy selection: {exc}")
        else:
            self.set_status("Selection copied to clipboard")

    def _transcript_document(self) -> TranscriptDocument:
        if self._reviewing:
            rendered = render_review(self._review_calls(), self._width())
            unit = RenderedUnit(to_formatted_text(ANSI(rendered)), rendered.count("\n") + 1)
            document = TranscriptDocument((unit,), (0,), unit.line_count)
        else:
            document = self._renderer.render_document(self.state, self._width())
        self._transcript_line_count = document.line_count
        return document

    def _interaction_text(self):  # type: ignore[no-untyped-def]
        return to_formatted_text(ANSI(render_interaction(self.interaction, self._width())))

    def _attachment_text(self):  # type: ignore[no-untyped-def]
        fragments = []
        for index, ref in enumerate(self.state.attachments, 1):
            size = f"{ref.size / (1024 * 1024):.1f} MiB" if ref.size >= 1024 * 1024 else f"{ref.size / 1024:.0f} KiB"
            fragments.append(("class:attachments", f" ▣ Image #{index}  {ref.filename} · {size}\n"))
        return FormattedText(fragments)

    def _status_visible(self) -> bool:
        return bool(
            self.state.running or self.state.status != "Ready"
            or self.runner.control.pending_steering_count()
            or self.runner.control.pending_follow_up_count()
        )

    def _queue_expanded(self) -> bool:
        return self.state.messages_expanded and bool(self.runner.control.pending_message_texts())

    def _queue_text(self) -> FormattedText:
        key = (tuple(self.runner.control.pending_message_texts()), self._width())
        if self._queue_preview_cache is None or self._queue_preview_cache[0] != key:
            text = render_queued_messages(list(key[0]), key[1]) if key[0] else ""
            self._queue_preview_cache = (key, to_formatted_text(ANSI(text)), text.count("\n") + 1)
            self._queue_scroll = 0
        return self._queue_preview_cache[1]

    def _queue_height(self) -> Dimension:
        self._queue_text()
        rows = self.application.output.get_size().rows
        height = min(self._queue_preview_cache[2], max(1, rows // 3))
        return Dimension.exact(height)

    def _queue_offset(self, maximum: int) -> int:
        self._queue_scroll = max(0, min(self._queue_scroll, maximum))
        return self._queue_scroll

    def _scroll_queue(self, delta: int) -> None:
        self._queue_text()
        maximum = max(0, self._queue_preview_cache[2] - self._queue_height().preferred)
        self._queue_offset(maximum)
        self._queue_scroll = max(0, min(maximum, self._queue_scroll + delta))
        self.application.invalidate()

    def _status_text(self) -> FormattedText:
        if not self._status_visible():
            self._working_messages.reset()
            return FormattedText([])
        queued = self.runner.control.pending_message_texts()
        pending = len(queued)
        active = self.state.running and self.interaction is None and not self.state.status.startswith("Cancelling")
        now = monotonic()
        message = self._working_messages.current(active, now)
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(now * 10) % 10] + " " if self.state.running else ""
        width = self._width()
        label = self.state.status if self.state.running or self.state.status != "Ready" else ""
        if (self._compacting or self.state.compacting) and not label.startswith("Cancelling"):
            label = "Compacting context"
        if pending:
            preview = " | ".join(" ".join(text.split()) for text in queued)
            queue_label = f"queued {pending} · Ctrl+B collapse" if self.state.messages_expanded else f"queued {pending}: {preview}"
            label = f"{label} · {queue_label}" if label else queue_label
        raw_status = f" {spinner}{label}"
        if pending and not self.state.messages_expanded and get_cwidth(raw_status) > width and width >= 20:
            hint = " · Ctrl+B expand" if width >= 32 else " · Ctrl+B"
            status = _truncate_cells(raw_status, max(1, width - get_cwidth(hint))) + hint
            status = _truncate_cells(status, width)
        else:
            status = _truncate_cells(raw_status, width)
        fragments = [("class:status", status)]
        remaining = width - get_cwidth(status)
        if message is not None and remaining > 3:
            fragments.append(("class:status-message", " · " + _truncate_cells(message.display, remaining - 3)))
        return FormattedText(fragments)

    def _footer_text(self):  # type: ignore[no-untyped-def]
        mode = self.runner.prepared.execution_mode.value
        perm = self.runner.permission_mode().value
        pending_permission = self.runner.pending_permission_mode()
        if pending_permission is not None:
            perm += f"→{pending_permission.value}(pending)"
        width = self._width()
        model = self.runner.current_model()
        model_label = model_display_name(model) if model is not None else "fixed model"
        model_label += f" · {self.runner.reasoning_effort()}"
        pending_model = self.runner.pending_model()
        pending_effort = self.runner.pending_reasoning_effort()
        if pending_model is not None:
            model_label += f" → {model_display_name(pending_model)} · {pending_effort or 'default'} (pending)"
        elif pending_effort is not None:
            model_label += f" → {pending_effort} (pending)"
        resume_id = self.runner.thread_id[:8]
        context = format_context_usage(
            self.state.usage, self.runner.context_window(),
            empty_session=self.state.context_pristine and not self.state.running,
        )
        # Row 1: workspace · model · resume on the left, git on the right.
        # Row 2: sandbox/permission on the left, context on the right.
        # The left side always truncates before the right side.
        git = self._git_summary.label() or "⎇ no git"
        filesystem = self.runner.sandbox_mode()
        pending_filesystem = self.runner.pending_sandbox_mode()
        if pending_filesystem is not None:
            filesystem += f"→{pending_filesystem}(pending)"
        status = f" {mode}:{filesystem} · perm:{perm}"
        git_fit = _truncate_cells(git, width)
        available = max(1, width - get_cwidth(git_fit) - 1)
        fragments: list[tuple[str, str]] = []
        if available < 6 + get_cwidth(resume_id) + 2:
            # Extremely narrow: keep the session handle and the git label only.
            resume_fit = _truncate_cells(resume_id, available)
            gap = " " * max(1, width - get_cwidth(resume_fit) - get_cwidth(git_fit))
            fragments.append(("class:footer-resume-id", resume_fit))
            fragments.append(("class:footer", gap + git_fit + "\n"))
        else:
            model_width = max(1, available - get_cwidth(resume_id) - 6)
            model_fit = _truncate_cells(model_label, model_width)
            workspace_width = max(1, available - get_cwidth(model_fit) - get_cwidth(resume_id) - 6)
            workspace_fit = _truncate_cells(f" {format_workspace_path(self._workspace())}", workspace_width)
            left_width = get_cwidth(workspace_fit) + get_cwidth(model_fit) + get_cwidth(resume_id) + 6
            first_padding = " " * max(1, width - left_width - get_cwidth(git_fit))
            if workspace_fit.startswith(" "):
                fragments.append(("class:footer", " "))
                fragments.append(("class:footer-workspace", workspace_fit[1:]))
            else:
                fragments.append(("class:footer-workspace", workspace_fit))
            fragments.append(("class:footer", " · "))
            fragments.append(("class:footer-model", model_fit))
            fragments.append(("class:footer", " · "))
            fragments.append(("class:footer-resume-id", resume_id))
            fragments.append(("class:footer", first_padding + git_fit + "\n"))
        fragments.append(("class:footer", _fit_footer_line(status, context, width)))
        return FormattedText(fragments)

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
            if self.interaction is None and event.current_buffer.text.strip() == "/skill":
                event.current_buffer.cancel_completion()
                self._submit_buffer("steer")
                return
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
            self.set_status("Reading clipboard…", transient=False)
            self.lifecycle.spawn(self._paste_clipboard(), stop=TaskStop.CANCEL, name="clipboard-paste")

        @kb.add(Keys.BracketedPaste, eager=True)
        def bracketed_paste(event) -> None:  # type: ignore[no-untyped-def]
            self._handle_pasted_text(event.data.replace("\r\n", "\n").replace("\r", "\n"))

        @kb.add("backspace", filter=Condition(
            lambda: self.interaction is None and self.buffer.text.startswith("/")
            and "\n" not in self.buffer.text
        ))
        def command_backspace(event) -> None:  # type: ignore[no-untyped-def]
            """退格修改命令后重新补全，同时解除已确认候选的抑制状态。"""
            get_by_name("backward-delete-char").handler(event)
            self.slash_completer.accepted_text = None
            if event.current_buffer.text.startswith("/"):
                event.current_buffer.start_completion()

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
            self._scroll_queue(-10) if self._queue_expanded() else self.scroll_transcript(-10)

        @kb.add("pagedown")
        def page_down(event) -> None:  # type: ignore[no-untyped-def]
            self._scroll_queue(10) if self._queue_expanded() else self.scroll_transcript(10)

        @kb.add("c-home")
        def scroll_top(event) -> None:  # type: ignore[no-untyped-def]
            self.scroll_transcript_to(0)

        @kb.add("c-end")
        def scroll_bottom(event) -> None:  # type: ignore[no-untyped-def]
            self.scroll_transcript_to(self.transcript_rows())

        @kb.add(" ", filter=interaction_active)
        def interaction_toggle(event) -> None:  # type: ignore[no-untyped-def]
            assert self.interaction
            if self.interaction.current.get("type") == "multi_select" and not self.interaction.accepts_text:
                self.interaction.toggle()
            else:
                event.current_buffer.insert_text(" ")

        @kb.add("tab", filter=interaction_active)
        def interaction_next(event) -> None:  # type: ignore[no-untyped-def]
            self._submit_buffer("steer")

        @kb.add("s-tab", filter=interaction_active)
        def interaction_previous(event) -> None:  # type: ignore[no-untyped-def]
            assert self.interaction
            if self.interaction.custom_entry:
                self.interaction.custom_entry = False
                self.interaction.error = ""
                event.current_buffer.reset()
                self._pasted_content.clear()
                self._skill_draft.clear()
                return
            if self.interaction.index > 0:
                self.interaction.index -= 1
                self.interaction.option_index = 0
                previous = self.interaction.values.get(str(self.interaction.current.get("id") or ""), "")
                if self.interaction.accepts_text and isinstance(previous, str):
                    event.current_buffer.text = previous
                    event.current_buffer.cursor_position = len(previous)
                else:
                    event.current_buffer.reset()
                    self._pasted_content.clear()
                    self._skill_draft.clear()

        @bind("interrupt")
        def escape(event) -> None:  # type: ignore[no-untyped-def]
            buffer = event.current_buffer
            action = self._input_actions.decide_interrupt(self._input_view(buffer))
            self.apply_input_action(action, buffer)

        @bind("clear_or_exit")
        def ctrl_c(event) -> None:  # type: ignore[no-untyped-def]
            buffer = event.current_buffer
            now = time.monotonic()
            action = self._input_actions.decide_clear_or_exit(self._input_view(buffer), now)
            self.apply_input_action(action, buffer)
            if action is InputAction.CLEAR_INPUT:
                self._input_actions.arm_clear(now)

        @bind("exit")
        def ctrl_d(event) -> None:  # type: ignore[no-untyped-def]
            buffer = event.current_buffer
            action = self._input_actions.decide_ctrl_d(self._input_view(buffer))
            self.apply_input_action(action, buffer)

        @bind("tools_expand")
        def tools_expand(event) -> None:  # type: ignore[no-untyped-def]
            self.state.tools_expanded = not self.state.tools_expanded
            self.set_status(f"Tool output: {'expanded' if self.state.tools_expanded else 'collapsed'}")

        @bind("review_diff")
        def review_diff(event) -> None:  # type: ignore[no-untyped-def]
            self._toggle_review()

        @bind("reopen_interaction")
        def reopen_interaction(event) -> None:  # type: ignore[no-untyped-def]
            self._reopen_pending_interaction_preserving_allow()

        @bind("thinking_toggle")
        def thinking_toggle(event) -> None:  # type: ignore[no-untyped-def]
            self.state.thinking_collapsed = not self.state.thinking_collapsed
            self.set_status(f"Thinking: {'collapsed' if self.state.thinking_collapsed else 'expanded'}")

        @bind("messages_toggle")
        def messages_toggle(event) -> None:  # type: ignore[no-untyped-def]
            self.state.messages_expanded = not self.state.messages_expanded
            self._queue_scroll = 0
            self.clear_transcript_selection()
            self.application.invalidate()

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
        if self.sessions.waiting:
            self.set_status(self.sessions.wait_status(), transient=False)
            return
        displayed = self.buffer.text.strip()
        if self._skill_draft.has_invalid_marker(displayed):
            self.state.add_system("A skill marker was edited. Restore it or remove it before submitting.", error=True)
            self.set_status("Edited skill marker")
            return
        if self.interaction is None and self._skill_draft.is_present(displayed):
            try:
                responses = self.runner.prepared.backend.download_files([self._skill_draft.path])
            except (OSError, ValueError, RuntimeError) as exc:
                self.state.add_system(f"Cannot read selected skill: {exc}", error=True)
                return
            if not responses or responses[0].error or responses[0].content is None:
                self.state.add_system("Selected skill is no longer readable. Remove it or select another skill.", error=True)
                return
        text = self._pasted_content.expand(displayed)
        text = self._skill_draft.expand(text)
        if self._pasted_content.has_invalid_marker(displayed):
            self.state.add_system(
                "A pasted-content marker was edited or truncated. "
                "Restore the marker or re-paste the content before submitting.",
                error=True,
            )
            self.set_status("Edited pasted-content marker")
            return
        if self.interaction is not None:
            was_text = self.interaction.accepts_text
            complete = self.interaction.accept(text)
            if self.interaction.kind == "skill":
                self.buffer.reset()
            elif was_text or complete:
                self._reset_submitted_buffer(text)
            if complete:
                self._finish_interaction()
            self.application.invalidate()
            return
        if not text and not self.state.attachments:
            return
        if self._compacting:
            self.set_status("Wait for compaction to finish")
            return
        if (not self._pasted_content.has_blocks and not self._skill_draft.is_present(displayed)
                and text.startswith("/") and "\n" not in text):
            self.buffer.reset(append_to_history=True)
            self.lifecycle.spawn(self._dispatch_command(text), stop=TaskStop.CANCEL, name="slash-command")
            return
        pending_interrupt = self.runner.current_interrupt() if not self.state.running else None
        if self.state.running or self.runner.control.pending_message_texts() or pending_interrupt is not None:
            if self.state.attachments:
                self.set_status("Wait for the active run before sending images")
                return
            self._reset_submitted_buffer(text)
            if queue_mode == "followUp":
                self.runner.follow_up(text)
                self.set_status("Follow-up queued")
            else:
                self.runner.steer(text)
                self.set_status("Steering queued")
            if pending_interrupt is not None:
                self.set_status(f"{self.state.status} · F2 pending", transient=False)
            if not self.state.running and (self._run_task is None or self._run_task.done()):
                self._start_queued_run()
            self.application.invalidate()
            return
        self._reset_submitted_buffer(text)
        self._start_run(text, image_refs=tuple(self.state.attachments))

    def _reset_submitted_buffer(self, expanded_text: str) -> None:
        if self._pasted_content.has_blocks or self._skill_draft.has_blocks:
            if expanded_text:
                self.buffer.history.append_string(expanded_text)
            self.buffer.reset(append_to_history=False)
        else:
            self.buffer.reset(append_to_history=bool(expanded_text))
        self._pasted_content.clear()
        self._skill_draft.clear()

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
        self._working_messages.reset()
        self.set_status("Working…  Esc to cancel", transient=False)
        self._run_epoch += 1
        epoch = self._run_epoch
        self._cancel_sent = False
        # Esc may arrive before the worker reaches begin_run; the reservation
        # keeps that cancel instead of letting begin_run clear it.
        reservation = self.runner.reserve_run()

        async def work() -> None:
            try:
                if resume_call is None:
                    try:
                        result = await self._run_blocking(
                            _call_in_run_epoch, epoch,
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
                        result = await self._run_blocking(_call_in_run_epoch, epoch, resume_call)
                    except Exception as exc:  # noqa: BLE001
                        self._apply_event(RunEvent(type="run_failed", content=str(exc)))
                        return
                self._handle_result(result)
            finally:
                self.runner.release_run_reservation(reservation)
                if epoch == self._run_epoch:
                    self._cancel_sent = False

        self._run_task = self.lifecycle.spawn(work(), stop=TaskStop.OBSERVE, name="agent-run")

    def _start_queued_run(self) -> None:
        if self.interaction is not None or self.runner.current_interrupt() is not None:
            return
        text = self.runner.control.pop_steering()
        if text is None:
            text = self.runner.control.pop_follow_up()
        if text is not None:
            # The applied event records the user message in the transcript.
            self._start_run("", resume_call=partial(
                self.runner.invoke, text, on_event=self._on_event_thread,
            ))

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
            self.set_status("Reading clipboard…", transient=False)
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
        if not self.runner.supports_next_input("image"):
            self.set_status("Current or pending model does not support image input")
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
                    if self.runner.supports_next_input("image"):
                        self.lifecycle.spawn(
                            self._attach_pasted_path(trimmed, payload),
                            stop=TaskStop.CANCEL,
                            name="attach-image",
                        )
                        return
                    self.set_status("Image path pasted as text: current or pending model does not support images")
            except (OSError, ClipboardError):
                pass
        self.buffer.insert_text(self._pasted_content.display(payload))

    async def _attach_pasted_path(self, value: str, original_payload: str) -> None:
        if not await self._attach_path(value):
            self.buffer.insert_text(self._pasted_content.display(original_payload))
            self.application.invalidate()

    async def _paste_clipboard(self, *, images_only: bool = False) -> None:
        content = await self._run_blocking(self.clipboard.inspect)
        if isinstance(content, ClipboardText):
            if images_only:
                self.set_status("Clipboard does not contain an image")
                return
            self._handle_pasted_text(content.text)
            self.application.invalidate()
            return
        if isinstance(content, ClipboardUnavailable):
            self.set_status(content.reason)
            return
        if not self.runner.supports_next_input("image"):
            self.set_status("Current or pending model does not support image input")
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
        epoch = _RUN_EPOCH.get()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._apply_run_event, event, epoch)
        else:
            self._apply_run_event(event, epoch)

    def _apply_run_event(self, event: RunEvent, epoch: int | None) -> None:
        # Events without an epoch come from the UI thread (steering, control).
        if epoch is not None and epoch != self._run_epoch:
            return
        self._apply_event(event)

    def _apply_event(self, event: RunEvent, *, announce: bool = True) -> None:
        previous_status = self.state.status
        self.state.apply(event)
        if self.state.status != previous_status:
            if event.type == "steering_queued":
                notice = self.state.status
                self.state.status = previous_status
                self.set_status(notice)
            elif self._allow_confirm_open() and event.type == "interaction_requested":
                self._show_allow_confirm_status()
            else:
                self.set_status(self.state.status, transient=False)
        if event.type in {"run_started", "run_cancelling"} or not self.state.running:
            self._working_messages.reset()
        if event.type == "runtime_config_applied" and isinstance(event.result, dict):
            changes = []
            if event.result.get("model_changed"):
                self.state.usage.clear()
                model = self.runner.current_model()
                changes.append(f"Model: {model_display_name(model) if model else event.result['model_id']} · {event.result['reasoning_effort']}")
            elif event.result.get("reasoning_changed"):
                model = self.runner.current_model()
                changes.append(f"Model: {model_display_name(model) if model else event.result['model_id']} · {event.result['reasoning_effort']}")
            if event.result.get("permission_changed"):
                changes.append(f"permission: {event.result['permission_mode']}")
            if event.result.get("sandbox_changed"):
                changes.append(f"sandbox: {event.result['sandbox_mode']}")
            if event.result.get("mcp_changed"):
                self._announce_mcp_switch()
            if announce:
                if event.result.get("model_changed") or event.result.get("reasoning_changed"):
                    self.set_status("; ".join(changes))
                if event.result.get("permission_changed"):
                    self.state.add_system(f"permission: {event.result['permission_mode']}")
                if event.result.get("sandbox_changed"):
                    self.state.add_system(f"sandbox: {event.result['sandbox_mode']}")
        elif event.type == "runtime_config_failed":
            self.state.add_system(f"Pending runtime config failed: {event.content}", error=True)
        self.application.invalidate()

    def _announce_mcp_switch(self) -> None:
        """Show the global MCP state only after the rebuilt graph has published it."""
        from agent.cli.mcp_ui import _announce_published_switch

        if getattr(self.runner, "mcp", None) is None:
            return
        _announce_published_switch(self)

    def _handle_result(self, result: RunResult) -> None:
        if result.status == "cancelled":
            self.state.running = False
            if result.error:
                self.state.add_system(result.error, error=True)
                self.set_status("Cancel cleanup not confirmed", transient=False)
            elif self.state.status != "Cancelled":
                self.set_status("Cancelled", transient=False)
        if result.status == "paused":
            self.state.running = False
            self.state.add_system("Paused at a checkpoint. Submit /pause again is unnecessary; press Enter to resume.")
        if result.status in {"waiting_confirmation", "waiting_human", "paused"}:
            if self._allow_confirm_open():
                self._note_interrupt_behind_allow_confirm(result.status)
            elif result.status == "waiting_confirmation" and (
                self.buffer.text or self.state.attachments or self.interaction is not None
            ):
                # An asynchronous approval must not take over an editor or picker.
                self.state.running = False
                self.set_status("Approval still pending · F2 to decide", transient=False)
            else:
                self._reopen_pending_interaction()
        elif self._deferred_config_interaction is not None:
            self.interaction = self._deferred_config_interaction
            self._deferred_config_interaction = None
            self._show_allow_confirm_status()
        elif result.status == "completed":
            # Input can arrive after the worker's final queue check, while
            # the UI still considers that worker to be running.
            self._start_queued_run()
        self.application.invalidate()

    def _finish_interaction(self, *, cancelled: bool = False) -> None:
        interaction = self.interaction
        if interaction is None:
            return
        self._reviewing = False
        if interaction.kind.startswith("mcp_"):
            from agent.cli.mcp_ui import finish_mcp_interaction

            finish_mcp_interaction(self, interaction, cancelled=cancelled)
            return
        if interaction.kind == "approval":
            self._restore_approval_draft()
        if interaction.kind == "skill":
            self.interaction = None
            draft, cursor = self._skill_picker_draft or ("", 0)
            self._skill_picker_draft = None
            self.buffer.text = draft
            self.buffer.cursor_position = cursor
            if cancelled:
                self.set_status("Skill selection cancelled")
                return
            path = str(interaction.values.get("skill") or "")
            option = next((item for item in interaction.fields[0]["options"] if item["value"] == path), None)
            if option is None:
                return
            for label in self._skill_draft.labels:
                cursor -= len(label) * draft[:cursor].count(label)
                draft = draft.replace(label, "")
            label = self._skill_draft.display(option["label"], path)
            self.buffer.document = Document(
                draft[:cursor] + label + " " + draft[cursor:], cursor_position=cursor + len(label) + 1,
            )
            self.set_status("Skill selected · Enter submit or add instructions")
            self.application.invalidate()
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
            self.sessions.begin_switch(session_id)
            return
        if interaction.kind == "model_source":
            if cancelled:
                self.interaction = None
                self.set_status("Model switch cancelled")
                return
            source = str(interaction.values.get("source") or "")
            try:
                self._show_model_choices(source, self.runner.list_models())
            except KeyError as exc:
                self.interaction = None
                self.state.add_system(str(exc), error=True)
            return
        if interaction.kind == "model":
            if cancelled:
                source = str(interaction.values.get("source") or "")
                if source:
                    self._show_model_sources(self.runner.list_models())
                    return
                self.interaction = None
                self.set_status("Model switch cancelled")
                return
            model_id = str(interaction.values.get("model") or "")
            self.interaction = None
            if not model_id:
                self.state.add_system("No model selected", error=True)
                return
            try:
                self._show_reasoning_choices(
                    self.runner.settings.get_profile(model_id),
                    str(interaction.values.get("source") or ""),
                )
            except (KeyError, RuntimeError, ValueError) as exc:
                self.state.add_system(str(exc), error=True)
            return
        if interaction.kind == "model_reasoning":
            source = str(interaction.values.get("source") or "")
            if cancelled:
                self._show_model_choices(source, self.runner.list_models())
                model_id = interaction.values["model"]
                for index, option in enumerate(self.interaction.fields[0]["options"]):
                    if option["value"] == model_id:
                        self.interaction.option_index = index
                        break
                return
            self.interaction = None
            try:
                self._switch_model(
                    str(interaction.values["model"]),
                    reasoning_effort=str(interaction.values.get("effort") or "default"),
                )
            except (KeyError, RuntimeError, ValueError) as exc:
                self.state.add_system(str(exc), error=True)
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
            if (mode is PermissionMode.ALLOW and self.runner.permission_mode() is not PermissionMode.ALLOW
                    and self.runner.pending_permission_mode() is not PermissionMode.ALLOW):
                self._begin_allow_permission_confirm()
                return
            try:
                self._request_permission_mode(mode)
            except (RuntimeError, ValueError) as exc:
                self.state.add_system(str(exc), error=True)
                return
            return
        if interaction.kind == "sandbox":
            if cancelled:
                self.interaction = None
                self.set_status("Sandbox change cancelled")
                return
            mode_raw = str(interaction.values.get("mode") or "")
            self.interaction = None
            mode = parse_sandbox_mode(mode_raw)
            if mode is None:
                self.state.add_system("No sandbox mode selected", error=True)
                return
            self._request_sandbox_mode(mode)
            return
        if interaction.kind == "permission_confirm":
            if cancelled:
                self.interaction = None
                if self._reopen_pending_interaction(notify_missing=False):
                    self.state.add_system("ALLOW mode cancelled")
                else:
                    self.set_status("ALLOW mode cancelled")
                return
            typed = str(interaction.values.get("confirm") or "").strip()
            self.interaction = None
            if typed != "ALLOW":
                self.state.add_system(
                    'ALLOW not enabled. Type ALLOW exactly to confirm, or use /permission ask.',
                    error=True,
                )
                if not self._reopen_pending_interaction(notify_missing=False):
                    self.set_status("Permission: ask")
                return
            try:
                self._request_permission_mode(PermissionMode.ALLOW)
            except (RuntimeError, ValueError) as exc:
                self.state.add_system(str(exc), error=True)
            self._reopen_pending_interaction(notify_missing=False)
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

    def _restore_approval_draft(self) -> None:
        if self._approval_draft is None:
            return
        document, pasted, skill = self._approval_draft
        self._approval_draft = None
        self._pasted_content = pasted
        self._skill_draft = skill
        self.buffer.set_document(document)

    def _dismiss_pending_interrupt(self, kind: str) -> None:
        self.interaction = None
        self._reviewing = False
        self.state.running = False
        deferred = self._deferred_config_interaction
        if deferred is not None:
            self.interaction = deferred
            self._deferred_config_interaction = None
            if deferred.kind == "permission_confirm":
                self._show_allow_confirm_status()
            self.application.invalidate()
            return
        label = {"approval": "Approval", "human": "Input", "pause": "Pause"}.get(kind, "Input")
        self.set_status(f"{label} still pending · F2 to decide", transient=False)

    def _reopen_pending_interaction_preserving_allow(self) -> bool:
        """F2: show the pending approval, and return to an open ALLOW prompt on Esc."""
        confirm = self.interaction if self._allow_confirm_open() else None
        if confirm is not None:
            self._deferred_config_interaction = confirm
            self.interaction = None
        opened = self._reopen_pending_interaction()
        if confirm is not None and not opened:
            self._deferred_config_interaction = None
            self.interaction = confirm
            self._show_allow_confirm_status()
        return opened

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
        if interrupt.kind is InterruptKind.RUNTIME_CONFIG_BOUNDARY:
            self.state.add_system("Completing a pending runtime config switch before the next model call.")
            self._start_run("", resume_call=partial(
                self.runner.resume_runtime_config, on_event=self._on_event_thread,
            ))
            return True
        if interrupt.kind is InterruptKind.WAITING_CONFIRMATION:
            if self.interaction is None and self._deferred_config_interaction is None and self._approval_draft is None:
                self._approval_draft = (self.buffer.document, self._pasted_content, self._skill_draft)
                self._pasted_content = PastedContentDraft()
                self._skill_draft = SkillDraft()
                self.buffer.reset()
            self.interaction = InteractionController.approval(list(interrupt.pending_tools))
            self.set_status("Waiting for input", transient=False)
        elif interrupt.kind is InterruptKind.WAITING_HUMAN:
            self.interaction = InteractionController.human(interrupt.payload)
            self.set_status("Waiting for input", transient=False)
        elif interrupt.kind is InterruptKind.PAUSED:
            self.interaction = InteractionController.pause()
            self.set_status("Paused", transient=False)
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
                if is_mutation_tool(str(call.get("name") or ""))
            ]
            if found:
                return found
        for index in range(len(self.state.blocks) - 1, -1, -1):
            block = self.state.blocks[index]
            if isinstance(block, ToolBlock) and is_mutation_tool(block.name):
                mutation = normalize_file_mutation(block)
                if mutation is not None and mutation.operation in {"create", "modify"}:
                    first = index
                    while first > 0:
                        prior = self.state.blocks[first - 1]
                        prior_mutation = normalize_file_mutation(prior) if isinstance(prior, ToolBlock) else None
                        if prior_mutation is None or prior_mutation.operation != mutation.operation:
                            break
                        if mutation.operation == "modify" and prior_mutation.path != mutation.path:
                            break
                        first -= 1
                    return [
                        (item.name, item.arguments)
                        for item in self.state.blocks[first:index + 1]
                        if isinstance(item, ToolBlock)
                    ]
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
        self.set_status("Reviewing diff · Esc or Ctrl+R to close", transient=False)

    def _close_review(self) -> None:
        self._reviewing = False
        self._transcript_anchor = None
        self.set_status("Waiting for input" if self.interaction is not None else "Ready", transient=False)


def default_config_dir() -> Path:
    configured = os.environ.get("DEEP_AGENT_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".deep-agent"


def _fit_footer_line(left: str, right: str, width: int) -> str:
    available = max(1, width)
    if not right:
        left = _truncate_cells(left, available)
        return left + (" " * max(0, available - get_cwidth(left)))
    left_fit, gap, right_fit = _fit_footer_parts(left, right, width)
    return f"{left_fit}{gap}{right_fit}"


def _fit_footer_parts(left: str, right: str, width: int) -> tuple[str, str, str]:
    available = max(1, width)
    right_fit = _truncate_cells(right, available)
    remaining = max(0, available - get_cwidth(right_fit) - 1)
    left_fit = _truncate_cells(left, remaining)
    gap = " " * max(1, available - get_cwidth(left_fit) - get_cwidth(right_fit))
    return left_fit, gap, right_fit


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
