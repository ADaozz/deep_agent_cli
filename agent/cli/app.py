from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import partial
import logging
import os
import signal
import time
import warnings
from time import monotonic
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from prompt_toolkit import ANSI, Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText, to_formatted_text
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.containers import WindowAlign
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from agent.cli.widgets import (
    _ScrollableTextControl,
    _InteractionControl,
    _BackToBottomControl,
    _TranscriptControl,
    _EditorScrollControl,
    _PastedContentProcessor,
    _TranscriptWindow,
)
from agent.cli.formatting import (
    format_workspace_path,
    format_context_usage,
    _fit_footer_line,
    _truncate_cells,
)
from agent.cli.clipboard import ClipboardAdapter, ClipboardError, copy_to_clipboard
from agent.cli.commands import command_table
from agent.cli.completion import SlashCompleter, model_display_name
from agent.cli.key_bindings import create_bindings
from agent.cli.attachments_controller import AttachmentsController
from agent.cli.transcript_navigation import TranscriptNavigation
from agent.cli.run_events_controller import RunEventsController
from agent.cli.skill_selection import SkillSelection
from agent.cli.context_compaction import ContextCompaction
from agent.cli.view_state import DialogState, InputDrafts, RunPresentation
from agent.cli.model_selection import ModelSelection
from agent.cli.access_selection import AccessSelection
from agent.cli.run_dispatch import RunDispatch
from agent.cli.interaction_flow import InteractionFlow
from agent.cli.gitinfo import REFRESH_SECONDS, GitProbe
from agent.cli.input import Keymap
from agent.cli.interactions import InteractionController
from agent.cli.mcp_ui import finish_mcp_interaction
from agent.cli.pasted_content import PastedContentDraft
from agent.cli.skills import SkillDraft
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
from agent.cli.session_summaries import SessionSummaries
from agent.cli.state import CliState, ToolBlock
from agent.cli.working_messages import WorkingMessageRotation, load_working_messages
from agent.bootstrap.config import require_keybindings_outside_workspace
from agent.cli.display import DEFAULT_UI_DISPLAY_LIMITS, DEFAULT_UI_TIMEZONE
from agent.cli.lifecycle import AppLifecycle, TaskStop, restore_terminal_modes
from agent.cli.mcp_controller import MCPController
from agent.cli.input_actions import (
    DOUBLE_PRESS_WINDOW_SECONDS,
    InputAction,
    InputActionRouter,
    InputView,
)
from agent.runtime.metrics import model_metric_groups, tool_usage_by_model
from agent.mcp.logging import MCPLogHandler
from agent.runtime.runner import AgentRunner
from agent.runtime.contracts import RunEvent


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
        self.dialogs = DialogState()
        self.drafts = InputDrafts()
        self.run_view = RunPresentation()
        self.state.load_model_metrics(runner.model_metrics_history())
        self.state.context_pristine = not runner.thread_has_content()
        self.commands = command_table()
        self.command_by_name = {item.name: item for item in self.commands}
        keymap_path = (config_dir / "keybindings.json") if config_dir else None
        self.keymap = Keymap.load(keymap_path)
        self.dialogs.current: InteractionController | None = None
        self.dialogs.deferred: InteractionController | None = None
        self.dialogs.reviewing = False
        self._io_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="deep-agent-io")
        self.run_view.loop: asyncio.AbstractEventLoop | None = None
        self.run_view.task: asyncio.Task[None] | None = None
        # Each dispatched run gets an epoch; its Runner events carry it so a
        # late event of an earlier run cannot change the current run's UI.
        self.run_view.epoch = 0
        self.run_view.cancel_sent = False
        self.run_view.compacting = False
        self._input_actions = InputActionRouter()
        self._status_notice_handle: asyncio.TimerHandle | None = None
        self._status_before_notice = "Ready"
        self._status_notice: str | None = None
        self.lifecycle = AppLifecycle(
            state=self.state, runner=self.runner,
            notify_session_exit=lambda: self.sessions.notify_exit(),
            stop_status_notice=self._cancel_status_notice,
            stop_selection=lambda: self.transcript._stop_selection_scroll(),
            output=lambda: self.application.output,
            exit_ui=lambda: self.application.exit(),
            shutdown_io=lambda: self._io_executor.shutdown(wait=False, cancel_futures=True),
            finish_shutdown=lambda: setattr(self, "_git_task", None),
        )
        self.terminated_by: signal.Signals | None = None
        self.mcp_controller = MCPController()
        self._mcp_draft: dict[str, str] = {}
        # None = stick to bottom (follow new output); int = pinned scroll row.
        self._queue_scroll = 0
        self._queue_preview_cache: tuple[Any, FormattedText, int] | None = None
        timezone_name = runner.settings.ui_timezone if runner.settings is not None else DEFAULT_UI_TIMEZONE
        display_limits = runner.settings.ui_display_limits if runner.settings is not None else DEFAULT_UI_DISPLAY_LIMITS
        self._renderer = TranscriptRenderer(ZoneInfo(timezone_name), limits=display_limits)
        self._git = GitProbe(self._workspace())
        self._git_summary = self._git.summary
        self._git_task: asyncio.Task[None] | None = None

        self.slash_completer = SlashCompleter(
            self.commands,
            model_profiles=self.runner.list_models,
            permission_modes=lambda: ["ask", "allow"] if self.access_selection._allow_available() else ["ask"],
            sandbox_modes=lambda: ["restricted", "full-access"],
        )
        self.buffer = Buffer(
            multiline=True,
            history=InMemoryHistory(),
            completer=self.slash_completer,
            complete_while_typing=True,
        )
        self.drafts.pasted = PastedContentDraft()
        self.drafts.skill = SkillDraft()
        self.dialogs.approval_draft: tuple[Document, PastedContentDraft, SkillDraft] | None = None
        self.dialogs.skill_picker_draft: tuple[str, int] | None = None
        self.model_selection = ModelSelection(
            dialogs=self.dialogs,
            run_events=lambda: self.run_events,
            runner=self.runner,
            sessions=lambda: self.sessions,
            set_status=lambda *args, **kwargs: self.set_status(*args, **kwargs),
            state=self.state,
            invalidate=lambda: self.application.invalidate(),
        )
        self.access_selection = AccessSelection(
            dialogs=self.dialogs,
            interaction_flow=lambda: self.interaction_flow,
            run_events=lambda: self.run_events,
            runner=self.runner,
            sessions=lambda: self.sessions,
            set_status=lambda *args, **kwargs: self.set_status(*args, **kwargs),
            state=self.state,
            invalidate=lambda: self.application.invalidate(),
        )
        self.attachments_controller = AttachmentsController(
            _run_blocking=lambda *args, **kwargs: self._run_blocking(*args, **kwargs),
            buffer=self.buffer,
            clipboard=ClipboardAdapter(),
            drafts=self.drafts,
            lifecycle=lambda: self.lifecycle,
            runner=self.runner,
            set_status=lambda *args, **kwargs: self.set_status(*args, **kwargs),
            state=self.state,
            invalidate=lambda: self.application.invalidate(),
        )
        self.transcript = TranscriptNavigation(
            _copy_selection=lambda *args, **kwargs: self._copy_selection(*args, **kwargs),
            _transcript_document=lambda *args, **kwargs: self._transcript_document(*args, **kwargs),
            lifecycle=lambda: self.lifecycle,
            transcript_window=lambda: self.transcript_window,
            invalidate=lambda: self.application.invalidate(),
            output=lambda: self.application.output,
        )
        self.run_dispatch = RunDispatch(
            execute_command=lambda command, arg: command.handler(self, arg),
            _on_event_thread=lambda *args, **kwargs: self._on_event_thread(*args, **kwargs),
            _run_blocking=lambda *args, **kwargs: self._run_blocking(*args, **kwargs),
            _working_messages=lambda: self._working_messages,
            buffer=self.buffer,
            command_by_name=self.command_by_name,
            dialogs=self.dialogs,
            drafts=self.drafts,
            interaction_flow=lambda: self.interaction_flow,
            lifecycle=lambda: self.lifecycle,
            run_events=lambda: self.run_events,
            run_view=self.run_view,
            runner=self.runner,
            sessions=lambda: self.sessions,
            set_status=lambda *args, **kwargs: self.set_status(*args, **kwargs),
            slash_completer=self.slash_completer,
            state=self.state,
            invalidate=lambda: self.application.invalidate(),
            _call_in_run_epoch=_call_in_run_epoch,
        )
        self.session_summaries = SessionSummaries(
            runner=self.runner, lifecycle=lambda: self.lifecycle,
            invalidate=lambda: self.application.invalidate(),
        )
        self.run_events = RunEventsController(
            run_view=self.run_view,
            request_summary=self.session_summaries.request,
            _allow_confirm_open=lambda *args, **kwargs: self.access_selection._allow_confirm_open(*args, **kwargs),
            _announce_mcp_switch=lambda *args, **kwargs: self._announce_mcp_switch(*args, **kwargs),
            _note_interrupt_behind_allow_confirm=lambda *args, **kwargs: self.access_selection._note_interrupt_behind_allow_confirm(*args, **kwargs),
            _reopen_pending_interaction=lambda *args, **kwargs: self.interaction_flow._reopen_pending_interaction(*args, **kwargs),
            _show_allow_confirm_status=lambda *args, **kwargs: self.access_selection._show_allow_confirm_status(*args, **kwargs),
            _start_queued_run=lambda *args, **kwargs: self.run_dispatch._start_queued_run(*args, **kwargs),
            _working_messages=lambda: self._working_messages,
            buffer=self.buffer,
            dialogs=self.dialogs,
            runner=self.runner,
            set_status=lambda *args, **kwargs: self.set_status(*args, **kwargs),
            state=self.state,
            invalidate=lambda: self.application.invalidate(),
        )
        self.interaction_flow = InteractionFlow(
            skills=lambda: self.skills,
            _on_event_thread=lambda *args, **kwargs: self._on_event_thread(*args, **kwargs),
            access_selection=lambda: self.access_selection,
            buffer=self.buffer,
            dialogs=self.dialogs,
            drafts=self.drafts,
            model_selection=lambda: self.model_selection,
            run_dispatch=lambda: self.run_dispatch,
            runner=self.runner,
            sessions=lambda: self.sessions,
            set_status=lambda *args, **kwargs: self.set_status(*args, **kwargs),
            state=self.state,
            invalidate=lambda: self.application.invalidate(),
            finish_mcp=lambda interaction, **kwargs: finish_mcp_interaction(self, interaction, **kwargs),
        )
        self.sessions = SessionController(
            summaries=lambda: self.session_summaries,
            _restore_queued_to_editor=lambda *args, **kwargs: self._restore_queued_to_editor(*args, **kwargs),
            _run_blocking=lambda *args, **kwargs: self._run_blocking(*args, **kwargs),
            buffer=self.buffer,
            dialogs=self.dialogs,
            interaction_flow=lambda: self.interaction_flow,
            lifecycle=lambda: self.lifecycle,
            reset_session_view=lambda *args, **kwargs: self.reset_session_view(*args, **kwargs),
            runner=self.runner,
            set_status=lambda *args, **kwargs: self.set_status(*args, **kwargs),
            state=self.state,
            invalidate=lambda: self.application.invalidate(),
        )
        self.skills = SkillSelection(
            runner=self.runner, sessions=lambda: self.sessions, dialogs=self.dialogs,
            drafts=self.drafts, buffer=self.buffer, run_view=self.run_view,
            is_exiting=lambda: self.lifecycle.exiting, run_blocking=self._run_blocking,
            set_status=self.set_status, state=self.state,
            invalidate=lambda: self.application.invalidate(),
        )
        self.context_compaction = ContextCompaction(
            runner=self.runner, sessions=lambda: self.sessions, dialogs=self.dialogs,
            run_view=self.run_view, run_blocking=self._run_blocking,
            set_status=self.set_status, state=self.state,
            invalidate=lambda: self.application.invalidate(),
        )
        self.transcript_control = _TranscriptControl(
            document=self._transcript_document,
            cursor=self.transcript._transcript_cursor,
            on_scroll=self.transcript.scroll_transcript,
            on_select=self.transcript._select_transcript,
            selected_line=self.transcript._selected_transcript_line,
        )
        self.interaction_control = _InteractionControl(
            text=self._interaction_text, focusable=False, on_scroll=self.transcript.scroll_transcript,
            controller=lambda: self.dialogs.current,
            on_select_outside=self.transcript._select_below_transcript,
        )
        self.attachment_control = _ScrollableTextControl(
            text=self._attachment_text, focusable=False, on_scroll=self.transcript.scroll_transcript,
            on_select_outside=self.transcript._select_below_transcript,
        )
        self.footer_control = _ScrollableTextControl(
            text=self._footer_text, focusable=False, on_scroll=self.transcript.scroll_transcript,
            on_select_outside=self.transcript._select_below_transcript,
        )
        messages_path = config_dir / "working_messages.yaml" if config_dir else None
        self._working_messages = WorkingMessageRotation(load_working_messages(messages_path))
        self.status_control = _ScrollableTextControl(
            text=self._status_text, focusable=False, on_scroll=self.transcript.scroll_transcript,
            on_select_outside=self.transcript._select_below_transcript,
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
            if self.dialogs.current is None and self.transcript.transcript_away_from_bottom() else FormattedText([]),
            focusable=False,
            on_scroll=self.transcript.scroll_transcript,
            on_click=self.transcript.follow_transcript,
            on_select_outside=self.transcript._select_below_transcript,
        )
        self.editor_control = _EditorScrollControl(
            buffer=self.buffer, focusable=True, on_scroll=self.transcript.scroll_transcript,
            on_select_outside=self.transcript._select_below_transcript,
            input_processors=[
                _PastedContentProcessor(self.drafts.pasted),
                _PastedContentProcessor(self.drafts.skill, "skill-block"),
            ],
        )
        self.bindings = self._create_bindings()

        interaction_visible = Condition(lambda: self.dialogs.current is not None)
        editor_visible = Condition(lambda: self.dialogs.current is None or self.dialogs.current.accepts_text)
        attachments_visible = Condition(lambda: bool(self.state.attachments) and self.dialogs.current is None)
        back_to_bottom_visible = Condition(self.transcript.transcript_away_from_bottom)

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
            if self.dialogs.current is None:
                return Dimension(min=3)
            text = render_interaction(self.dialogs.current, self._width())
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
            viewport=self.transcript,
        )
        self.interaction_divider_window = Window(height=1, char="─", style="class:interaction-divider")
        self.interaction_bottom_divider_window = Window(height=1, char="─", style="class:interaction-divider")
        self.interaction_hint_window = Window(
            _BackToBottomControl(
                text=FormattedText([("class:back-to-bottom", "↓ Back to bottom · esc")]),
                focusable=False, on_scroll=self.transcript.scroll_transcript,
                on_click=self.transcript.follow_transcript, on_select_outside=self.transcript._select_below_transcript,
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
        self.run_view.loop = asyncio.get_running_loop()
        installed = self._install_signal_handlers(self.run_view.loop)
        try:
            self._start_git_watch()
            with self._capture_runtime_logs():
                try:
                    await self.sessions.start()
                    await self.application.run_async(pre_run=self._before_ui)
                finally:
                    await self.lifecycle.shutdown()
        finally:
            # Removing a handler restores SIG_DFL, so a signal during
            # runner.close() terminates the process directly.
            for sig in installed:
                self.run_view.loop.remove_signal_handler(sig)

    def _install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> list[signal.Signals]:
        installed: list[signal.Signals] = []
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                loop.add_signal_handler(sig, self._on_terminate_signal, sig)
            except (NotImplementedError, RuntimeError, ValueError):
                continue
            installed.append(sig)
        return installed

    def _on_terminate_signal(self, sig: signal.Signals) -> None:
        """First signal leaves through the normal exit path; a second one stops now."""
        if self.terminated_by is None:
            self.terminated_by = sig
            if not self.application.is_running:
                # application.exit() needs a running UI; pre_run exits instead.
                return
            try:
                self.lifecycle.request_exit()
            except OSError:
                # SIGHUP: the terminal may already be gone.
                pass
            return
        restore_terminal_modes()
        signal.signal(sig, signal.SIG_DFL)
        os.kill(os.getpid(), sig)

    def _before_ui(self) -> None:
        if self.terminated_by is not None:
            self.lifecycle.request_exit()
            return
        self._start_mcp_connections()

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
                if self.run_view.loop is not None and self.run_view.loop.is_running():
                    self.run_view.loop.call_soon_threadsafe(self._show_runtime_log, message)
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
            if self.run_view.loop is not None and self.run_view.loop.is_running():
                self.run_view.loop.call_soon_threadsafe(self._show_runtime_log, text)
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
            self.dialogs.current is None and previous.startswith(("Select ", "Type ALLOW", "Reviewing diff"))
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
                loop = self.run_view.loop
            if loop is not None and loop.is_running():
                self._status_before_notice = previous
                self._status_notice = text
                self._status_notice_handle = loop.call_later(
                    DOUBLE_PRESS_WINDOW_SECONDS if text == INPUT_CLEARED_HINT else STATUS_NOTICE_SECONDS,
                    self._hide_status_notice,
                )
        self.application.invalidate()

    def _show_input_cleared_hint(self) -> None:
        """空闲时短暂提示输入已清空，执行和待处理状态保持原样。"""
        if (self.state.running or self.run_view.compacting or self.state.compacting
                or self.state.status == "Waiting for input"
                or self.state.status.endswith("still pending · F2 to decide")):
            self.application.invalidate()
            return
        self.set_status(INPUT_CLEARED_HINT)

    def _cancel_status_notice(self) -> None:
        if self._status_notice_handle is not None:
            self._status_notice_handle.cancel()
            self._status_notice_handle = None

    def _hide_status_notice(self) -> None:
        """仅恢复仍有效的提示，避免覆盖后来出现的运行或审批状态。"""
        self._status_notice_handle = None
        notice = self._status_notice
        self._status_notice = None
        if self.state.status == notice:
            previous = self._status_before_notice
            if previous == "Compacting context" and not (self.run_view.compacting or self.state.compacting):
                previous = "Ready"
            self.set_status(previous, transient=False)


    def show_help(self) -> None:
        commands = "\n".join(
            f"/{item.name:<9} {item.description}"
            for item in self.commands
        )
        self.state.add_system(
            "/help\n"
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
            "/status\n"
            f"Status: {'running' if self.state.running else 'idle'}\n"
            f"{model_line}"
            f"{tool_line}\n"
            f"{self._model_metrics_lines()}\n\n"
            f"{self._tool_usage_lines()}"
        )

    def _tool_usage_lines(self) -> str:
        models, rows = tool_usage_by_model(self.state.model_metrics_history)
        if not rows:
            return "Tool usage: none yet"
        header = ["Tool", *models]
        table = [header, *([name, *(str(row.get(model, 0)) for model in models)] for name, row in rows)]
        widths = [max(get_cwidth(line[index]) for line in table) for index in range(len(header))]

        def format_line(cells: list[str]) -> str:
            name = cells[0] + " " * (widths[0] - get_cwidth(cells[0]))
            values = (" " * (widths[i] - get_cwidth(cell)) + cell for i, cell in enumerate(cells[1:], 1))
            return "  " + "  ".join([name, *values]).rstrip()

        return "Tool usage\n" + "\n".join(format_line(line) for line in table)

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
                "/session\n"
                f"Session: {self.runner.thread_id}\n"
                f"{model_line}"
                "Checkpointer: in-memory\nPersistent /resume is not configured."
            )
            return
        info = store.get(self.runner.thread_id)
        status = info.status if info else "unknown"
        stop_reason = info.last_run_status.value if info else "unknown"
        title = info.summary if info and info.summary else ""
        created_at = info.created_at.astimezone().isoformat(sep=" ", timespec="seconds") if info else "unknown"
        updated_at = info.updated_at.astimezone().isoformat(sep=" ", timespec="seconds") if info else "unknown"
        self.state.add_system(
            "/session\n"
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

    def reset_session_view(self) -> None:
        """Drop editor chrome that belonged to the previous session."""
        self.skills._clear_skill_draft()
        self.transcript.clear_transcript_selection()
        self.dialogs.current = None
        self.dialogs.deferred = None
        self.dialogs.reviewing = False
        self.transcript.transcript_anchor = None
        self._renderer.clear()


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

    @property
    def _exiting(self) -> bool:
        return self.lifecycle.exiting

    @property
    def _last_ctrl_c(self) -> float | None:
        """Double-press clock. Owned by InputActionRouter; exposed for existing checks."""
        return self._input_actions.last_ctrl_c

    def _input_view(self, buffer: Buffer) -> InputView:
        text = buffer.text
        interaction = self.dialogs.current
        return InputView(
            has_interaction=interaction is not None,
            custom_entry=bool(interaction is not None and interaction.custom_entry),
            completion_open=buffer.complete_state is not None,
            slash_line=text.startswith("/") and "\n" not in text,
            transcript_away=self.transcript.transcript_away_from_bottom(),
            session_waiting=self.sessions.waiting,
            reviewing=self.dialogs.reviewing,
            compacting=self.run_view.compacting,
            running=self.state.running,
            cancel_sent=self.run_view.cancel_sent,
            mcp_busy=self.mcp_controller.busy,
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
            self.drafts.pasted.clear()
            self.drafts.skill.clear()
            self.application.invalidate()
            return
        if action is InputAction.FOLLOW_TRANSCRIPT:
            self.transcript.follow_transcript()
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
            assert self.dialogs.current is not None
            self.dialogs.current.custom_entry = False
            self.dialogs.current.error = ""
            buffer.reset()
            self.drafts.pasted.clear()
            self.drafts.skill.clear()
            self.application.invalidate()
            return
        if action is InputAction.CANCEL_INTERACTION:
            self.interaction_flow._finish_interaction(cancelled=True)
            return
        if action is InputAction.WAIT_FOR_COMPACTION:
            self.set_status("Wait for compaction to finish")
            return
        if action is InputAction.CANCEL_ALREADY_SENT:
            self.set_status("Cancelling… waiting for the run to stop", transient=False)
            self.application.invalidate()
            return
        if action is InputAction.CANCEL_RUN:
            self.run_view.send_queue_after_cancel = bool(self.runner.control.pending_message_texts())
            self.run_view.cancel_sent = True
            self.runner.request_cancel()
            if self.run_view.send_queue_after_cancel:
                self.set_status("Interrupting… queued messages will be sent next", transient=False)
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
            self.drafts.pasted.clear()
            self.drafts.skill.clear()
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


    async def _copy_selection(self, content: str) -> None:
        try:
            await self._run_blocking(copy_to_clipboard, content, output=self.application.output)
        except ClipboardError as exc:
            self.set_status(f"Could not copy selection: {exc}")
        else:
            self.set_status("Selection copied to clipboard")

    def _transcript_document(self) -> TranscriptDocument:
        if self.dialogs.reviewing:
            rendered = render_review(self._review_calls(), self._width())
            unit = RenderedUnit(to_formatted_text(ANSI(rendered)), rendered.count("\n") + 1)
            document = TranscriptDocument((unit,), (0,), unit.line_count)
        else:
            document = self._renderer.render_document(self.state, self._width())
        self.transcript.transcript_line_count = document.line_count
        return document

    def _interaction_text(self):  # type: ignore[no-untyped-def]
        return to_formatted_text(ANSI(render_interaction(self.dialogs.current, self._width())))

    def _attachment_text(self):  # type: ignore[no-untyped-def]
        fragments = []
        for index, ref in enumerate(self.state.attachments, 1):
            size = f"{ref.size / (1024 * 1024):.1f} MiB" if ref.size >= 1024 * 1024 else f"{ref.size / 1024:.0f} KiB"
            fragments.append(("class:attachments", f" ▣ Image #{index}  {ref.filename} · {size}\n"))
        return FormattedText(fragments)

    def _status_visible(self) -> bool:
        status = self.state.status
        interaction_hint = self.dialogs.current is not None and " · Enter confirm · Esc " in status
        return bool(
            self.state.running or (status != "Ready" and not interaction_hint)
            or self.runner.control.pending_steering_count()
            or self.runner.control.pending_follow_up_count()
        )

    def _queue_expanded(self) -> bool:
        return bool(self.runner.control.pending_message_texts())

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
        active = self.state.running and self.dialogs.current is None and not self.state.status.startswith("Cancelling")
        now = monotonic()
        message = self._working_messages.current(active, now)
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(now * 10) % 10] + " " if self.state.running else ""
        width = self._width()
        counter = ""
        if self.state.subagent_count:
            count = self.state.subagent_count
            counter = _truncate_cells(f"{count} subagent" + ("s" if count != 1 else ""), width)
        left_width = max(0, width - get_cwidth(counter) - 1) if counter else width
        label = self.state.status if self.state.running or self.state.status != "Ready" else ""
        if self.dialogs.current is not None and " · Enter confirm · Esc " in label:
            label = ""
        if (self.run_view.compacting or self.state.compacting) and not label.startswith("Cancelling"):
            label = "Compacting context"
        raw_status = f" {spinner}{label}"
        status = _truncate_cells(raw_status, left_width)
        fragments = [("class:status", status)]
        remaining = left_width - get_cwidth(status)
        if message is not None and remaining > 3:
            fragments.append(("class:status-message", " · " + _truncate_cells(message.display, remaining - 3)))
        if counter:
            used = sum(get_cwidth(text) for _, text in fragments)
            fragments.append(("class:status", " " * max(0, width - used - get_cwidth(counter)) + counter))
        return FormattedText(fragments)

    def _footer_text(self):  # type: ignore[no-untyped-def]
        mode = self.runner.prepared.execution_mode.value.lower()
        if mode == "sandboxed":
            mode = "sandbox"
        perm = self.runner.permission_mode().value
        pending_permission = self.runner.pending_permission_mode()
        if pending_permission is not None:
            perm += f"→{pending_permission.value}(pending)"
        width = self._width()
        model = self.runner.current_model()
        model_label = model.model if model is not None else "fixed model"
        model_label += f" · {self.runner.reasoning_effort()}"
        pending_model = self.runner.pending_model()
        pending_effort = self.runner.pending_reasoning_effort()
        if pending_model is not None:
            model_label += f" → {pending_model.model} · {pending_effort or 'default'} (pending)"
        elif pending_effort is not None:
            model_label += f" → {pending_effort} (pending)"
        resume_id = self.session_summaries.label(self.runner.thread_id)
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
        return create_bindings(self, lambda: time.monotonic())


    def _on_event_thread(self, event: RunEvent) -> None:
        epoch = _RUN_EPOCH.get()
        if self.run_view.loop is not None:
            self.run_view.loop.call_soon_threadsafe(self._apply_run_event, event, epoch)
        else:
            self._apply_run_event(event, epoch)

    def _apply_run_event(self, event: RunEvent, epoch: int | None) -> None:
        # Events without an epoch come from the UI thread (steering, control).
        # Summary metadata carries its own thread_id and survives a later UI run.
        if (epoch is not None and epoch != self.run_view.epoch
                and event.type != "session_summary_requested"):
            return
        self.run_events._apply_event(event)


    def _announce_mcp_switch(self) -> None:
        """Show the global MCP state only after the rebuilt graph has published it."""
        from agent.cli.mcp_ui import _announce_published_switch

        if getattr(self.runner, "mcp", None) is None:
            return
        _announce_published_switch(self)


    def _review_calls(self) -> list[tuple[str, dict[str, Any]]]:
        if self.dialogs.current is not None and self.dialogs.current.kind == "approval":
            found = [
                (str(call.get("name") or "tool"), call.get("args") if isinstance(call.get("args"), dict) else {})
                for call in self.dialogs.current.calls
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
        if self.dialogs.reviewing:
            self._close_review()
            return
        if not self._review_calls():
            self.set_status("No pending review")
            return
        self.dialogs.reviewing = True
        self.transcript.transcript_anchor = 0
        self.set_status("Reviewing diff · Esc or Ctrl+R to close", transient=False)

    def _close_review(self) -> None:
        self.dialogs.reviewing = False
        self.transcript.transcript_anchor = None
        self.set_status("Waiting for input" if self.dialogs.current is not None else "Ready", transient=False)


def default_config_dir() -> Path:
    configured = os.environ.get("DEEP_AGENT_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".deep-agent"


def _register_terminal_sequences() -> None:
    # Kitty/CSI-u and xterm modified-key encodings used for Shift+Enter.
    # Ctrl+J remains the portable fallback when a terminal does not distinguish it.
    ANSI_SEQUENCES.setdefault("\x1b[13;2u", Keys.ControlJ)
    ANSI_SEQUENCES.setdefault("\x1b[13;2~", Keys.ControlJ)
