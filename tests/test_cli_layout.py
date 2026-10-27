"""Transcript scrolling, selection, footer, and status line."""

from langchain_core.messages import AIMessage
from deepagents.backends import StateBackend
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent
from prompt_toolkit.output import DummyOutput

import asyncio
import pytest

from agent.cli.gitinfo import GitSummary, parse_status
from agent.cli.interactions import InteractionController
from agent.cli.app import (
    FOOTER_LINES,
    CliApplication,
    format_context_usage,
    format_context_window,
    format_workspace_path,
)
from agent.cli.rendering import (
    TranscriptRenderer,
    render_transcript,
)
from agent.cli.state import CliState, ToolBlock, touch
from agent.config import Settings
from agent.runner import AgentRunner, RunEvent
from agent.session import TranscriptBlock
from tests.cli_support import (
    escape as _escape,
    paint as _paint,
    plain as _plain,
    wheel as _wheel,
    with_painted_app as _with_painted_app,
)
from tests.conftest import scripted_model


def test_tui_mouse_scroll_updates_transcript_anchor() -> None:
    from prompt_toolkit.mouse_events import MouseEventType

    def scenario(app: CliApplication) -> None:
        assert app.application.mouse_support()
        assert app.transcript_rows() > app.transcript_viewport_rows()

        assert app._transcript_anchor is None
        assert app.transcript_control.mouse_handler(_wheel(MouseEventType.SCROLL_UP)) is None
        assert app._transcript_anchor == app.transcript_max_scroll() - 3

        click = MouseEvent(
            position=Point(x=0, y=0),
            event_type=MouseEventType.MOUSE_DOWN,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
        assert app.interaction_control.mouse_handler(click) is None

    _with_painted_app("mouse-scroll", scenario)


def test_drag_selection_scrolls_history_and_copies_selected_text(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from prompt_toolkit.mouse_events import MouseEventType

    copied: list[str] = []
    monkeypatch.setattr(
        "agent.cli.app.copy_to_clipboard",
        lambda content, **_kwargs: copied.append(content) or "test",
    )

    async def driver() -> None:
        runner = AgentRunner(
            model=scripted_model([AIMessage(content="unused")]),
            backend=StateBackend(), thread_id="drag-select",
        )
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            for index in range(80):
                app.state.add_system(f"历史 filler line {index}")
            _paint(app)
            app.scroll_transcript_to(0)
            _paint(app)
            bottom = app.transcript_viewport_rows() - 1

            def mouse(kind, row):  # type: ignore[no-untyped-def]
                return MouseEvent(
                    position=Point(x=1, y=row), event_type=kind,
                    button=MouseButton.LEFT, modifiers=frozenset(),
                )

            app.transcript_control.mouse_handler(mouse(MouseEventType.MOUSE_DOWN, 1))
            app.transcript_control.mouse_handler(mouse(MouseEventType.MOUSE_MOVE, bottom))
            assert app.transcript_top() >= 3
            for _ in range(5):
                app._scroll_selection_edge()
            assert app.transcript_top() >= 15
            assert app._selection_end is not None and app._selection_end[0] > bottom
            assert "transcript-selection" in str(app._selected_transcript_line(1, app._transcript_document().get_line(1)))
            app.editor_control.mouse_handler(mouse(MouseEventType.MOUSE_UP, 0))
            await asyncio.sleep(0.01)
            assert app.state.status == "Selection copied to clipboard"
            assert app._selection_scroll_handle is None

    asyncio.run(driver())
    assert len(copied) == 1
    assert "历史" in copied[0]
    assert "filler line" in copied[0]
    assert "\n" in copied[0]


def _selection_app(lines: list[str]) -> CliApplication:
    from agent.cli.rendering import RenderedUnit, TranscriptDocument

    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), thread_id="select-columns",
    )
    app = CliApplication(runner, output=DummyOutput())
    unit = RenderedUnit([("", "\n".join(lines))], len(lines))
    document = TranscriptDocument((unit,), (0,), unit.line_count)
    app._transcript_document = lambda: document  # type: ignore[method-assign]
    return app


def _drag(app: CliApplication, start: tuple[int, int], end: tuple[int, int]) -> str:
    from prompt_toolkit.mouse_events import MouseEventType

    def mouse(kind, row, index):  # type: ignore[no-untyped-def]
        return MouseEvent(
            position=Point(x=index, y=row), event_type=kind,
            button=MouseButton.LEFT, modifiers=frozenset(),
        )

    app._select_transcript(mouse(MouseEventType.MOUSE_DOWN, *start))
    app._selection_end = (end[0], app._selection_column(end[0], end[1]))
    return app._selected_transcript_text()


def test_selection_maps_character_index_to_cells_after_wide_characters() -> None:
    app = _selection_app(["历史 filler line"])
    # prompt_toolkit reports character indexes: "f" is index 3 but cell 5.
    assert app._selection_column(0, 3) == 5
    assert _drag(app, (0, 3), (0, 8)) == "filler"
    styled = app._selected_transcript_line(0, app._transcript_document().get_line(0))
    marked = "".join(text for style, text, *_ in styled if "transcript-selection" in style)
    assert marked == "filler"


def test_selection_keeps_zero_width_characters_with_their_base() -> None:
    app = _selection_app(["ae\u0301x"])
    # "e\u0301" is one cell: the combining accent must travel with the "e".
    assert _drag(app, (0, 0), (0, 1)) == "ae\u0301"
    styled = app._selected_transcript_line(0, app._transcript_document().get_line(0))
    marks = {text: "transcript-selection" in style for style, text, *_ in styled}
    assert marks == {"a": True, "e": True, "\u0301": True, "x": False}
    # "x" follows the accent at index 3 but sits in cell 2.
    assert app._selection_column(0, 3) == 2


def test_selection_includes_last_character_of_each_line() -> None:
    app = _selection_app(["first", "second line"])
    # Clicking right of the text resolves to the last character's index.
    assert _drag(app, (0, 0), (1, 10)) == "first\nsecond line"


def test_selection_rows_are_document_rows_after_scrolling() -> None:
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.mouse_events import MouseEventType

    def scenario(app: CliApplication) -> None:
        app.scroll_transcript_to(40)
        _paint(app)
        app.application.layout.update_parents_relations()
        renderer = app.application.renderer
        where = renderer._last_screen.visible_windows_to_write_positions[app.transcript_window]
        handler = renderer.mouse_handlers.mouse_handlers[where.ypos][where.xpos]
        with set_app(app.application):
            handler(MouseEvent(
                position=Point(x=where.xpos, y=where.ypos), event_type=MouseEventType.MOUSE_DOWN,
                button=MouseButton.LEFT, modifiers=frozenset(),
            ))
        assert app._selection_start is not None and app._selection_start[0] == 40

    _with_painted_app("select-document-rows", scenario)


def test_drag_selection_stops_when_release_is_lost_outside_window(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from prompt_toolkit.mouse_events import MouseEventType

    async def driver() -> None:
        runner = AgentRunner(
            model=scripted_model([AIMessage(content="unused")]),
            backend=StateBackend(), thread_id="drag-lost-up",
        )
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            for index in range(40):
                app.state.add_system(f"历史 filler line {index}")
            _paint(app)
            app.scroll_transcript_to(0)
            _paint(app)
            bottom = app.transcript_viewport_rows() - 1

            def mouse(kind, row, button):  # type: ignore[no-untyped-def]
                return MouseEvent(
                    position=Point(x=1, y=row), event_type=kind,
                    button=button, modifiers=frozenset(),
                )

            app.transcript_control.mouse_handler(mouse(MouseEventType.MOUSE_DOWN, 1, MouseButton.LEFT))
            app.transcript_control.mouse_handler(mouse(MouseEventType.MOUSE_MOVE, bottom, MouseButton.LEFT))
            assert app._selection_dragging is True
            assert app._selection_scroll_handle is not None
            # Release happened outside the terminal: MOUSE_UP never arrives and the
            # next plain motion (1003 any-motion tracking) carries no button.
            app.transcript_control.mouse_handler(mouse(MouseEventType.MOUSE_MOVE, 2, MouseButton.NONE))
            assert app._selection_dragging is False
            assert app._selection_scroll_handle is None
            assert app._selection_edge == 0
            # Later plain motion must not extend the selection or re-arm autoscroll.
            frozen_end = app._selection_end
            app.editor_control.mouse_handler(mouse(MouseEventType.MOUSE_MOVE, 0, MouseButton.NONE))
            assert app._selection_end == frozen_end
            assert app._selection_scroll_handle is None

    asyncio.run(driver())


def test_transcript_scroll_down_moves_viewport_back() -> None:
    """Scrolling down used to be a no-op: prompt_toolkit clamped to the old offset."""
    from prompt_toolkit.mouse_events import MouseEventType

    def scenario(app: CliApplication) -> None:
        maximum = app.transcript_max_scroll()
        assert maximum > 9

        for _ in range(3):
            app.transcript_control.mouse_handler(_wheel(MouseEventType.SCROLL_UP))
            _paint(app)
        assert app.transcript_window.vertical_scroll == maximum - 9

        app.transcript_control.mouse_handler(_wheel(MouseEventType.SCROLL_DOWN))
        _paint(app)
        assert app.transcript_window.vertical_scroll == maximum - 6

        for _ in range(3):
            app.transcript_control.mouse_handler(_wheel(MouseEventType.SCROLL_DOWN))
            _paint(app)
        assert app._transcript_anchor is None
        assert app.transcript_window.vertical_scroll == maximum

    _with_painted_app("scroll-down", scenario)


def test_transcript_page_down_follows_page_up() -> None:
    def scenario(app: CliApplication) -> None:
        maximum = app.transcript_max_scroll()
        app.scroll_transcript_to(0)
        _paint(app)
        assert app.transcript_window.vertical_scroll == 0

        app.scroll_transcript(10)
        _paint(app)
        assert app.transcript_window.vertical_scroll == 10

        app.scroll_transcript_to(maximum + 50)
        _paint(app)
        assert app._transcript_anchor is None
        assert app.transcript_window.vertical_scroll == maximum

    _with_painted_app("page-scroll", scenario)


def test_streamed_transcript_follows_only_while_tail_is_visible() -> None:
    def scenario(app: CliApplication) -> None:
        assert not app.transcript_away_from_bottom()
        app._apply_event(RunEvent(type="assistant_delta", content="new answer\n" * 8))
        _paint(app)
        assert app.transcript_window.vertical_scroll == app.transcript_max_scroll()

        app.scroll_transcript(-6)
        _paint(app)
        pinned = app.transcript_window.vertical_scroll
        assert app.transcript_away_from_bottom()
        app._apply_event(RunEvent(type="assistant_delta", content="new answer\n" * 20))
        _paint(app)
        assert app.transcript_window.vertical_scroll == pinned

        app.follow_transcript()
        _paint(app)
        assert app._transcript_anchor is None
        assert app.transcript_window.vertical_scroll == app.transcript_max_scroll()
        assert not app.transcript_away_from_bottom()

        app.scroll_transcript(-5)
        _paint(app)
        assert app.transcript_offset(0) == 0
        assert app._transcript_anchor is None
        assert not app.transcript_away_from_bottom()

    _with_painted_app("stream-follow", scenario)


def test_back_to_bottom_hint_click_and_escape_priority() -> None:
    from prompt_toolkit.mouse_events import MouseEventType

    def scenario(app: CliApplication) -> None:
        app.state.running = True
        app.runner.control.begin_run()
        app.scroll_transcript(-5)
        _paint(app)
        assert app.transcript_away_from_bottom()
        handlers = app.application.renderer.mouse_handlers.mouse_handlers
        hint_window = next(w for w in app.application.renderer._last_screen.visible_windows_to_write_positions if w.content is app.back_to_bottom_control)
        hint_y = app.application.renderer._last_screen.visible_windows_to_write_positions[hint_window].ypos
        assert handlers[hint_y][1] is not None

        _escape(app)
        _paint(app)
        assert app._transcript_anchor is None
        assert not app.runner.control.cancel_requested
        assert not app.transcript_away_from_bottom()

        app.scroll_transcript(-5)
        _paint(app)
        handlers = app.application.renderer.mouse_handlers.mouse_handlers
        click = MouseEvent(
            position=Point(x=1, y=hint_y),
            event_type=MouseEventType.MOUSE_DOWN,
            button=MouseButton.LEFT,
            modifiers=frozenset(),
        )
        app.application.layout.update_parents_relations()
        with set_app(app.application):
            assert handlers[hint_y][1](click) is None
        _paint(app)
        assert app._transcript_anchor is None
        assert app.transcript_window.vertical_scroll == app.transcript_max_scroll()

        _escape(app)
        assert app.runner.control.cancel_requested

    _with_painted_app("back-to-bottom", scenario)


def test_escape_cancels_slash_command_before_scrolling_or_cancelling_run() -> None:
    def scenario(app: CliApplication) -> None:
        app.state.running = True
        app.runner.control.begin_run()
        app.scroll_transcript(-5)
        app.buffer.text = "/model"
        _escape(app)
        assert app.buffer.text == ""
        assert app.transcript_away_from_bottom()
        assert not app.runner.control.cancel_requested
        assert app.application.ttimeoutlen <= 0.1
        assert app.application.timeoutlen <= 0.2

    _with_painted_app("slash-escape", scenario)


def test_escape_returns_to_bottom_before_cancelling_interaction() -> None:
    def scenario(app: CliApplication) -> None:
        app.interaction = InteractionController.approval([{
            "toolCallId": "call-write", "name": "write_file", "args": {},
        }])
        app._finish_interaction = lambda *, cancelled=False: cancelled_calls.append(cancelled)  # type: ignore[method-assign]
        app.scroll_transcript(-5)
        _paint(app)
        _escape(app)
        assert app._transcript_anchor is None
        assert cancelled_calls == []
        _escape(app)
        assert cancelled_calls == [True]

    cancelled_calls: list[bool] = []
    _with_painted_app("interaction-back-to-bottom", scenario)


def test_back_to_bottom_hint_sits_immediately_above_editor_below_status() -> None:
    def scenario(app: CliApplication) -> None:
        app.state.running = True
        app.state.status = "Working…  Esc to cancel"
        app.scroll_transcript(-5)
        _paint(app)
        screen = app.application.renderer._last_screen
        columns = app.application.output.get_size().columns
        rows = [
            "".join(screen.data_buffer[y][x].char for x in range(columns))
            for y in range(app.application.output.get_size().rows)
        ]
        hint_row = next(y for y, row in enumerate(rows) if "↓ Back to bottom · esc" in row)
        status_row = next(y for y, row in enumerate(rows) if "Working" in row)
        assert hint_row == status_row + 1
        assert "class:editor" in screen.data_buffer[hint_row + 1][0].style
        assert "class:back-to-bottom" not in screen.data_buffer[hint_row][0].style

    _with_painted_app("hint-above-editor", scenario)


@pytest.mark.parametrize("interaction_kind", [None, "approval", "human"])
def test_back_to_bottom_uses_a_row_only_when_needed(interaction_kind) -> None:
    def scenario(app: CliApplication) -> None:
        if interaction_kind == "approval":
            app.interaction = InteractionController.approval([{"toolCallId": "x", "name": "execute", "args": {}}])
        elif interaction_kind == "human":
            app.interaction = InteractionController.human({
                "question": "输入说明", "fields": [{"id": "answer", "type": "text", "label": "回答"}],
            })
        app.state.running = app.interaction is None
        app.state.status = "Working…  Esc to cancel" if app.interaction is None else "Waiting for input"
        app.follow_transcript()
        _paint(app)

        def positions():
            return {
                control: (pos.ypos, pos.height)
                for window, pos in app.application.renderer._last_screen.visible_windows_to_write_positions.items()
                for control in (app.status_control, app.editor_control, app.interaction_control)
                if window.content is control
            }

        before = positions()
        transcript_height = app.transcript_viewport_rows()
        app.scroll_transcript(-5)
        _paint(app)
        if app.interaction is None:
            after = positions()
            assert after[app.editor_control] == before[app.editor_control]
            assert after[app.status_control] == (before[app.status_control][0] - 1, 1)
            assert app.transcript_viewport_rows() == transcript_height - 1
        else:
            assert positions() == before
            assert app.transcript_viewport_rows() == transcript_height - 1
        screen = app.application.renderer._last_screen
        rows = [
            "".join(screen.data_buffer[y][x].char for x in range(app._width()))
            for y in range(app.application.output.get_size().rows)
        ]
        assert sum("↓ Back to bottom · esc" in row for row in rows) == 1
        if app.interaction is not None:
            hint_row = next(y for y, row in enumerate(rows) if "↓ Back to bottom · esc" in row)
            assert hint_row + 2 == before[app.interaction_control][0]
        app.follow_transcript()
        _paint(app)
        assert positions() == before

    _with_painted_app(f"stable-hint-{interaction_kind}", scenario)


def test_waiting_for_input_has_matching_divider_below_status() -> None:
    def scenario(app: CliApplication) -> None:
        app.interaction = InteractionController.approval([{"toolCallId": "x", "name": "execute", "args": {}}])
        app._apply_event(RunEvent(type="interaction_requested"))
        app.scroll_transcript(-5)
        _paint(app)
        screen = app.application.renderer._last_screen
        columns = app.application.output.get_size().columns
        rows = [
            "".join(screen.data_buffer[y][x].char for x in range(columns))
            for y in range(app.application.output.get_size().rows)
        ]
        status_row = next(y for y, row in enumerate(rows) if "Waiting for input" in row)
        divider_rows = [
            y for y, row in enumerate(rows)
            if row.startswith("─") and "class:interaction-divider" in screen.data_buffer[y][0].style
        ]
        assert len(divider_rows) == 2
        assert divider_rows[0] < status_row
        assert divider_rows[1] == status_row + 1
        style = app.application.style
        assert style.get_attrs_for_style_str(screen.data_buffer[divider_rows[0]][0].style) == style.get_attrs_for_style_str(screen.data_buffer[divider_rows[1]][0].style)
        hint_row = next(y for y, row in enumerate(rows) if "↓ Back to bottom · esc" in row)
        assert hint_row + 1 == divider_rows[0]
        assert rows[divider_rows[0]] == "─" * columns
        assert sum("↓ Back to bottom · esc" in row for row in rows) == 1
        app.state.status = "Select a model"
        _paint(app)
        assert len([
            window for window in app.application.renderer._last_screen.visible_windows_to_write_positions
            if window.style == "class:interaction-divider"
        ]) == 2
        app.interaction = InteractionController(
            kind="skill", title="Select skill", question="Choose a skill",
            fields=[{"id": "skill", "type": "single_select", "label": "Skill",
                     "options": [{"value": "alpha", "label": "alpha"}]}],
        )
        app.state.status = "Select a skill · Enter confirm · Esc cancel"
        _paint(app)
        screen = app.application.renderer._last_screen
        visible = screen.visible_windows_to_write_positions
        assert app.interaction_divider_window in visible
        assert app.interaction_bottom_divider_window in visible
        assert visible[app.interaction_bottom_divider_window].ypos > visible[app.interaction_divider_window].ypos
        app.interaction = None
        app.state.status = "Ready"
        _paint(app)
        assert app.interaction_bottom_divider_window not in app.application.renderer._last_screen.visible_windows_to_write_positions

    _with_painted_app("waiting-input-divider", scenario)


def test_transcript_uses_full_width_without_scrollbar_margin() -> None:
    def scenario(app: CliApplication) -> None:
        window = app.transcript_window
        info = window.render_info
        assert info is not None
        assert window.right_margins == []
        assert info.window_width == app.application.output.get_size().columns
        assert app._width() == info.window_width

    _with_painted_app("full-width-transcript", scenario)


def test_status_line_uses_editable_config_from_config_directory(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "working_messages.yaml"
    path.write_text('interval_seconds: 8\njokes:\n  - "需求很简单，工期很勇敢。"\nfgo: []\n', encoding="utf-8")
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), thread_id="working-custom",
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, config_dir=config_dir, input=pipe, output=DummyOutput())
        monkeypatch.setattr(app, "_width", lambda: 200)
        app._apply_event(RunEvent(type="run_started"))
        assert "需求很简单，工期很勇敢。" in "".join(value for _, value in app._status_text())


def test_compaction_status_restores_working_and_clears_on_terminal_events(monkeypatch) -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), thread_id="compacting-status",
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        monkeypatch.setattr(app, "_width", lambda: 200)
        text = lambda: "".join(value for _, value in app._status_text())
        app._apply_event(RunEvent(type="run_started"))
        app._apply_event(RunEvent(type="compaction_started"))
        assert "Compacting context" in text() and "Working" not in text()
        assert " · " in text()
        app._apply_event(RunEvent(type="compaction_finished"))
        assert "Working" in text() and "Compacting" not in text()
        app._apply_event(RunEvent(type="compaction_started"))
        app._apply_event(RunEvent(type="run_cancelling"))
        assert "Cancelling" in text() and "Compacting" not in text()
        app._apply_event(RunEvent(type="run_failed", content="failed"))
        assert not app.state.compacting
        app._apply_event(RunEvent(type="run_started"))
        assert "Working" in text()


def test_status_line_is_above_editor_and_footer_does_not_repeat_it() -> None:
    def scenario(app: CliApplication) -> None:
        app.state.running = True
        app.state.status = "Working…  Esc to cancel"
        _paint(app)
        windows = {window.content: window for window in app.application.layout.find_all_windows()}
        positions = app.application.renderer._last_screen.visible_windows_to_write_positions
        status_position = positions[windows[app.status_control]]
        editor_position = positions[windows[app.editor_control]]
        footer_position = positions[windows[app.footer_control]]
        assert status_position.ypos < editor_position.ypos < footer_position.ypos
        assert status_position.height == 1
        assert "Working" in "".join(value for _, value in app._status_text())
        assert "Working" not in "".join(value for _, value in app._footer_text())
        attrs = app.application.style.get_attrs_for_style_str("class:status")
        assert attrs.bold
        assert attrs.color == "ansicyan"
        app._apply_event(RunEvent(type="run_completed"))
        _paint(app)
        assert windows[app.status_control] not in app.application.renderer._last_screen.visible_windows_to_write_positions
        assert not app._status_visible()

    _with_painted_app("working-position", scenario)


@pytest.mark.parametrize("steering,follow_up", [(1, 0), (0, 1), (1, 2)])
def test_completed_status_shows_pending_queue_and_hides_when_drained(monkeypatch, steering, follow_up) -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), thread_id="completed-queue",
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        monkeypatch.setattr(app, "_width", lambda: 200)
        app._apply_event(RunEvent(type="run_started"))
        for _ in range(steering):
            runner.control.steer("steer")
        for _ in range(follow_up):
            runner.control.follow_up("follow up")
        text = lambda: "".join(value for _, value in app._status_text())
        assert f"{app.state.status} · queued {steering + follow_up}" in text()
        app._apply_event(RunEvent(type="run_completed"))
        assert app._status_visible()
        preview = " | ".join(["steer"] * steering + ["follow up"] * follow_up)
        assert text().strip() == f"queued {steering + follow_up}: {preview}"
        monkeypatch.setattr(app, "_width", lambda: 7)
        assert text() == " queue…"
        for _ in range(steering):
            runner.control.pop_steering()
        for _ in range(follow_up):
            runner.control.pop_follow_up()
        assert not app._status_visible()
        assert text() == ""


def test_working_status_rotates_and_hides_messages_after_stop(monkeypatch) -> None:
    from agent.cli.working_messages import load_working_messages
    config = load_working_messages()
    monkeypatch.setattr("agent.cli.working_messages.random.shuffle", lambda items: None)
    JOKES, FGO_LINES = config.jokes, config.fgo

    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), thread_id="working-rotation",
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        monkeypatch.setattr(app, "_width", lambda: 200)
        now = [100.0]
        monkeypatch.setattr("agent.cli.app.monotonic", lambda: now[0])
        app._apply_event(RunEvent(type="run_started"))
        text = lambda: "".join(value for _, value in app._status_text())
        assert JOKES[0].text in text()
        now[0] += 8
        assert JOKES[1].text in text()
        now[0] += 8
        assert FGO_LINES[0].display in text()
        app._apply_event(RunEvent(type="run_cancelling"))
        assert "Cancelling" in text()
        assert FGO_LINES[0].text not in text()
        app._apply_event(RunEvent(type="run_completed"))
        assert text() == ""
        app._apply_event(RunEvent(type="run_started"))
        assert JOKES[0].text in text()
        app._apply_event(RunEvent(type="interaction_requested"))
        assert text().strip() == "Waiting for input"


@pytest.mark.parametrize("width", [1, 20, 30, 48, 80])
def test_status_line_truncates_chinese_messages_by_terminal_cells(monkeypatch, width) -> None:
    from prompt_toolkit.utils import get_cwidth
    from agent.cli.working_messages import load_working_messages
    FGO_LINES = load_working_messages().fgo

    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), thread_id="working-narrow",
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        monkeypatch.setattr(app, "_width", lambda: width)
        now = [100.0]
        monkeypatch.setattr("agent.cli.app.monotonic", lambda: now[0])
        app._apply_event(RunEvent(type="run_started"))
        for offset in (0, 16):
            now[0] = 100 + offset
            line = "".join(value for _, value in app._status_text())
            assert get_cwidth(line) <= width
            assert "\n" not in line
            if width >= 20:
                assert "Working" in line
            if width == 80 and offset == 16:
                assert any(message.display in line for message in FGO_LINES)


@pytest.mark.parametrize(("path", "expected"), [
    ("/home/neo", "~/"),
    ("/home/neo/project/app", "~/project/app"),
    ("/home/neo/中文项目", "~/中文项目"),
    ("/home/neon/project", "/home/neon/project"),
    ("/home/other/project", "/home/other/project"),
    ("/srv/project", "/srv/project"),
    ("relative/project", "relative/project"),
])
def test_workspace_path_abbreviates_only_current_home(monkeypatch, path, expected) -> None:
    from pathlib import Path

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/neo")))
    assert format_workspace_path(Path(path)) == expected


def test_footer_puts_workspace_model_and_resume_id_on_first_line(monkeypatch) -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        thread_id="footer-model",
        settings=Settings(),
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        monkeypatch.setattr(app, "_width", lambda: 140)
        footer = "".join(fragment[1] for fragment in app._footer_text())
        lines = footer.splitlines()
        assert len(lines) == FOOTER_LINES
        assert lines[0].startswith(f" {format_workspace_path(app._workspace())} · qwen3.5-plus · default · footer-m")
        assert lines[0].rstrip().endswith("⎇ checking git")
        assert "· default ·" in lines[0]
        assert "qwen3.5-plus" not in lines[1]
        assert "Ready" not in footer
        assert "perm:" in lines[1]
        assert not app._status_visible()
        assert "".join(value for _, value in app._status_text()) == ""


def test_footer_uses_terminal_palette_for_workspace_resume_id_and_model(monkeypatch) -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        thread_id="footer-colors",
        settings=Settings(),
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        monkeypatch.setattr(app, "_width", lambda: 140)
        fragments = list(app._footer_text())
        first_line = "".join(value for _, value in fragments).splitlines()[0]
        assert first_line.startswith(f" {format_workspace_path(app._workspace())} · qwen3.5-plus · default · footer-c")
        assert first_line.rstrip().endswith("⎇ checking git")
        assert any(style == "class:footer-workspace" and format_workspace_path(app._workspace()) in value for style, value in fragments)
        assert ("class:footer-resume-id", "footer-c") in fragments
        assert ("class:footer-model", "qwen3.5-plus · default") in fragments
        for label, color in (
            ("footer-workspace", "ansigreen"),
            ("footer-resume-id", "ansicyan"),
            ("footer-model", "ansiyellow"),
        ):
            assert app.application.style.get_attrs_for_style_str(f"class:{label}").color == color


def test_footer_keeps_the_right_side_in_narrow_terminal(monkeypatch) -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(), thread_id="narrow-resume", settings=Settings(),
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        monkeypatch.setattr(app, "_width", lambda: 20)
        lines = "".join(value for _, value in app._footer_text()).splitlines()
        # The right side wins in a narrow terminal; the left side truncates.
        assert lines[0].rstrip().endswith("⎇ checking git")
        assert "narr" in lines[0]
        assert len(lines[0]) <= 20
        assert len(lines[1]) <= 20


def test_footer_reports_workspace_git_and_context_usage() -> None:
    settings = Settings.from_mapping({
        "llm": {
            "default": "local/metered",
            "models": {"local": {"models": {"metered": {"model": "qwen3.5-plus", "context_window": "1m"}}}},
        },
    })
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        thread_id="footer-context",
        settings=settings,
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app._git_summary = GitSummary(branch="main", changed=5, available=True)
        app.state.apply(RunEvent(
            type="usage",
            result={"input_tokens": 39_000, "output_tokens": 12, "total_tokens": 39_012},
        ))
        lines = "".join(fragment[1] for fragment in app._footer_text()).splitlines()
        assert len(lines) == FOOTER_LINES
        assert lines[0].rstrip().endswith("⎇ main · 5 changed")
        assert lines[1].rstrip().endswith("1.0m Context · 3.9% used")


def test_footer_hides_context_meter_without_a_configured_window(monkeypatch) -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        thread_id="footer-unknown",
        settings=Settings(),
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        monkeypatch.setattr(app, "_width", lambda: 140)
        app.state.apply(RunEvent(type="usage", result={"total_tokens": 500}))
        lines = "".join(fragment[1] for fragment in app._footer_text()).splitlines()
        assert len(lines) == FOOTER_LINES
        assert lines[0].startswith(f" {format_workspace_path(app._workspace())} · qwen3.5-plus · default · footer-u")
        assert "Context" not in lines[1]


def test_footer_places_context_on_execution_settings_line_when_git_unavailable() -> None:
    settings = Settings.from_mapping({
        "llm": {
            "default": "token-plan/metered",
            "models": {"token-plan": {"models": {"metered": {"model": "auto", "context_window": "1m"}}}},
        },
    })
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="unused")]),
        backend=StateBackend(),
        settings=settings,
    )
    with create_pipe_input() as pipe:
        app = CliApplication(runner, input=pipe, output=DummyOutput())
        app._git_summary = GitSummary()
        lines = "".join(fragment[1] for fragment in app._footer_text()).splitlines()
        assert len(lines) == FOOTER_LINES
        assert "token-plan · auto · default" in lines[0]
        assert lines[0].rstrip().endswith("⎇ no git")
        assert lines[1].rstrip().endswith("1.0m Context · 0.0% used")

        app.state.apply(RunEvent(type="run_started"))
        assert "1.0m Context · Unknown" in "".join(part[1] for part in app._footer_text())
        app.state.apply(RunEvent(type="run_completed"))
        app.state.clear()
        assert "1.0m Context · Unknown" in "".join(part[1] for part in app._footer_text())
        app.new_session()
        assert "1.0m Context · 0.0% used" in "".join(part[1] for part in app._footer_text())
        app.state.load_transcript([TranscriptBlock(kind="user", content="previous task")])
        assert "1.0m Context · Unknown" in "".join(part[1] for part in app._footer_text())
        app.state.apply(RunEvent(type="usage", result={"input_tokens": 10_000}))
        assert "1.0m Context · 1.0% used" in "".join(part[1] for part in app._footer_text())


def test_context_usage_formatting() -> None:
    assert format_context_window(1_000_000) == "1.0m"
    assert format_context_window(200_000) == "200k"
    assert format_context_window(8_000) == "8k"
    assert format_context_usage({}, 0) == ""
    assert format_context_usage({}, 128_000) == "128k Context · Unknown"
    assert format_context_usage({}, 128_000, empty_session=True) == "128k Context · 0.0% used"
    assert format_context_usage({}, 0, empty_session=True) == ""
    assert format_context_usage({"input_tokens": 5_000}, 128_000) == "128k Context · 3.9% used"
    assert format_context_usage({"input_tokens": 9_000_000}, 1_000_000) == "1.0m Context · 900.0% used"


def test_compact_command_reports_threshold_and_current_usage() -> None:
    model = scripted_model([AIMessage(
        content="done",
        usage_metadata={"input_tokens": 1000, "output_tokens": 10, "total_tokens": 1010},
        response_metadata={"model_provider": "openai"},
    )])
    model.profile = {"max_input_tokens": 128_000}
    runner = AgentRunner(
        model=model, backend=StateBackend(),
        settings=Settings.from_mapping({"llm": {"default": "local/test", "models": {"local": {"context_window": "128k", "models": {"test": {}}}}}}),
    )
    assert runner.invoke("hello").status == "completed"

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await app._dispatch_command("/compact")
            notice = app.state.blocks[-1].content
            assert "42.5%" in notice
            assert "0.8%" in notice
            assert app.state.running is False

    asyncio.run(scenario())


def test_compact_command_does_not_invent_usage_without_model_report() -> None:
    model = scripted_model([AIMessage(content="unused")])
    model.profile = {"max_input_tokens": 128_000}
    runner = AgentRunner(
        model=model, backend=StateBackend(),
        settings=Settings.from_mapping({"llm": {"default": "local/test", "models": {"local": {"context_window": "128k", "models": {"test": {}}}}}}),
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await app._dispatch_command("/compact")
            notice = app.state.blocks[-1].content
            assert "42.5%" in notice
            assert "unknown" in notice
            assert "0.0%" not in notice

    asyncio.run(scenario())


def test_automatic_compaction_emits_status_events_without_streaming_summary() -> None:
    model = scripted_model([
        AIMessage(
            content="first answer",
            usage_metadata={"input_tokens": 110_000, "output_tokens": 10, "total_tokens": 110_010},
            response_metadata={"model_provider": "openai"},
        ),
        AIMessage(content="internal summary"),
        AIMessage(content="final answer"),
    ])
    model.profile = {"max_input_tokens": 128_000}
    runner = AgentRunner(model=model, backend=StateBackend())
    assert runner.invoke("long " * 15_000).status == "completed"
    events = []
    result = runner.invoke("continue", on_event=events.append)
    assert result.status == "completed"
    types = [event.type for event in events]
    assert types.index("compaction_started") < types.index("compaction_finished") < types.index("assistant_started")
    assert all("internal summary" not in event.content for event in events)


def test_compact_command_runs_upstream_tool_and_reports_success() -> None:
    model = scripted_model([
        AIMessage(
            content="done",
            usage_metadata={"input_tokens": 60_000, "output_tokens": 10, "total_tokens": 60_010},
            response_metadata={"model_provider": "openai"},
        ),
        AIMessage(content="short summary"),
    ])
    model.profile = {"max_input_tokens": 128_000}
    runner = AgentRunner(
        model=model, backend=StateBackend(),
        settings=Settings.from_mapping({"llm": {"default": "local/test", "models": {"local": {"context_window": "128k", "models": {"test": {}}}}}}),
    )
    assert runner.invoke("long " * 15_000).status == "completed"

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app.state.usage = {"input_tokens": 60_000, "total_tokens": 60_010}
            task = asyncio.create_task(app._dispatch_command("/compact"))
            # A real Application has a refresh timer; keep the DummyOutput test
            # loop ticking while LangGraph finishes work in the IO executor.
            for _ in range(100):
                if task.done():
                    break
                await asyncio.sleep(0.05)
            assert task.done()
            await task
            assert "Conversation compacted." in app.state.blocks[-1].content
            assert app.state.usage == {"input_tokens": 60_000, "total_tokens": 60_010}
            assert app.state.running is False

    asyncio.run(scenario())


def test_git_status_parsing() -> None:
    clean = parse_status("## main...origin/main\n")
    assert clean == GitSummary(branch="main", changed=0, available=True)
    assert clean.label() == "⎇ main · clean"

    dirty = parse_status("## feature/x...origin/feature/x [ahead 2]\n M a.py\n?? b.py\n")
    assert dirty.branch == "feature/x"
    assert dirty.changed == 2
    assert dirty.label() == "⎇ feature/x · 2 changed"

    assert parse_status("## No commits yet on main\n?? a.py\n").branch == "main"
    assert parse_status("## HEAD (no branch)\n").branch == "detached"
    assert GitSummary().label() == ""


def test_transcript_renderer_renders_each_unit_once(monkeypatch) -> None:
    """Frozen history must not go back through Rich on every frame."""
    import agent.cli.rendering as rendering

    original = rendering._render_unit
    rendered: list[int] = []

    def counting(renderable: object, width: int) -> object:
        rendered.append(width)
        return original(renderable, width)

    monkeypatch.setattr(rendering, "_render_unit", counting)

    state = CliState()
    state.add_system("first frozen block")
    state.add_system("second frozen block")
    renderer = TranscriptRenderer()

    first, lines = renderer.render(state, 80)
    assert lines > 3
    assert len(rendered) == 3  # header + two system blocks

    again, repeated = renderer.render(state, 80)
    assert repeated == lines
    assert len(rendered) == 3
    assert "".join(part[1] for part in again) == "".join(part[1] for part in first)

    state.apply(RunEvent(type="assistant_delta", content="streaming"))
    streaming, grown = renderer.render(state, 80)
    assert grown > lines
    assert len(rendered) == 4  # only the new active block
    assert "streaming" in "".join(part[1] for part in streaming)

    state.apply(RunEvent(type="assistant_delta", content="streaming further"))
    renderer.render(state, 80)
    assert len(rendered) == 5

    renderer.render(state, 40)
    assert len(rendered) == 5 + 4  # a width change rebuilds every unit


def test_transcript_renderer_caches_collapsed_explore_groups(monkeypatch) -> None:
    """A collapsed read/grep/glob group is one unit and must not re-render when frozen."""
    import agent.cli.rendering as rendering

    original = rendering._render_unit
    rendered: list[int] = []

    def counting(renderable: object, width: int) -> object:
        rendered.append(width)
        return original(renderable, width)

    monkeypatch.setattr(rendering, "_render_unit", counting)

    def explore(index: int, name: str) -> ToolBlock:
        return ToolBlock(
            tool_call_id=f"e{index}", name=name,
            arguments={"file_path": f"f{index}.py"}, output="x", status="completed",
        )

    state = CliState(blocks=[explore(0, "read_file"), explore(1, "grep"), explore(2, "glob")])
    renderer = TranscriptRenderer()

    fragments, _ = renderer.render(state, 80)
    assert len(rendered) == 2  # header + one collapsed group
    assert "Explored 3 items" in _plain("".join(part[1] for part in fragments))

    for _ in range(3):
        renderer.render(state, 80)
    assert len(rendered) == 2

    state.blocks.append(explore(3, "read_file"))
    renderer.render(state, 80)
    assert len(rendered) == 3  # the group gained a member

    state.blocks.extend(explore(index, "read_file") for index in range(4, 7))
    fragments, _ = renderer.render(state, 80)
    text = _plain("".join(part[1] for part in fragments))
    assert "Explored 7 items" in text
    assert "… 2 more" in text
    assert "Read f6.py" in text
    assert "Read f0.py" not in text

    state.tools_expanded = True
    fragments, _ = renderer.render(state, 80)
    assert len(rendered) == 11  # cached header, seven tool units built
    expanded = _plain("".join(part[1] for part in fragments))
    assert "read f0.py" in expanded
    assert "read f6.py" in expanded
    assert "Ctrl+O to expand" not in expanded


def test_expanded_tool_cache_survives_collapse_and_invalidates_changed_tool(monkeypatch) -> None:
    import agent.cli.rendering as rendering

    original = rendering._render_unit
    rendered: list[int] = []

    def counting(renderable: object, width: int) -> object:
        rendered.append(width)
        return original(renderable, width)

    monkeypatch.setattr(rendering, "_render_unit", counting)
    state = CliState(blocks=[
        ToolBlock(str(index), "read_file", {"file_path": f"f{index}.py"}, output="old", status="completed")
        for index in range(6)
    ])
    renderer = TranscriptRenderer()
    state.tools_expanded = True
    first = renderer.render_document(state, 80)
    assert len(rendered) == 7  # header and six tool cards
    assert renderer.render_document(state, 80) is first

    state.tools_expanded = False
    renderer.render_document(state, 80)
    assert len(rendered) == 8  # one collapsed summary
    state.tools_expanded = True
    assert renderer.render_document(state, 80) is first
    assert len(rendered) == 8

    tool = state.blocks[0]
    assert isinstance(tool, ToolBlock)
    touch(tool).output = "new"
    changed = renderer.render_document(state, 80)
    assert changed is not first
    assert len(rendered) == 9  # only the changed card
    assert any(
        "new" in "".join(part[1] for part in changed.get_line(index))
        for index in range(changed.line_count)
    )


def test_expanded_document_splits_only_requested_tool_lines(monkeypatch) -> None:
    import agent.cli.rendering as rendering

    original = rendering.split_lines
    calls: list[int] = []

    def counting(fragments):  # type: ignore[no-untyped-def]
        calls.append(1)
        return original(fragments)

    monkeypatch.setattr(rendering, "split_lines", counting)
    state = CliState(
        blocks=[
            ToolBlock(str(index), "read_file", {"file_path": f"f{index}.py"}, output="one\ntwo", status="completed")
            for index in range(20)
        ],
        tools_expanded=True,
    )
    renderer = TranscriptRenderer()
    document = renderer.render_document(state, 80)
    assert calls == []
    assert "two" in "".join(part[1] for part in document.get_line(document.line_count - 1))
    assert len(calls) == 1
    document.get_line(document.line_count - 2)
    assert len(calls) == 1
    assert renderer.render_document(state, 80) is document
    assert len(calls) == 1


def test_transcript_renderer_matches_uncached_render() -> None:
    state = CliState()
    state.add_user("hello there")
    state.blocks.append(ToolBlock(
        tool_call_id="t1", name="execute", arguments={"command": "pytest -q"},
        output="ok\nfailed", status="completed",
    ))
    state.blocks.append(ToolBlock(
        tool_call_id="t2", name="read_file", arguments={"file_path": "a.py"},
        output="print(1)", status="completed",
    ))
    renderer = TranscriptRenderer()
    fragments, lines = renderer.render(state, 90)
    expected = _plain(render_transcript(state, 90))
    assert "".join(part[1] for part in fragments) == expected
    assert lines == expected.count("\n") + 1




@pytest.mark.parametrize("kind", ["approval", "human", "multi_select"])
def test_interaction_options_follow_selection_without_scrolling_heading(kind) -> None:
    from prompt_toolkit.data_structures import Size

    def scenario(app: CliApplication) -> None:
        if kind == "approval":
            controller = InteractionController.approval([{
                "toolCallId": "resize-approval", "name": "execute",
                "args": {"command": "python process_data.py --source incoming --output results", "network": True},
            }])
        else:
            controller = InteractionController.human({
                "title": "Choose environment",
                "question": "Review the deployment environments before confirming your selection.",
                "fields": [{"id": "target", "type": kind if kind == "multi_select" else "single_select",
                            "label": "Environment", "options": [
                                {"value": str(i), "label": f"OPTION_{i:02}",
                                 "description": "A deployment environment with a long explanatory description."}
                                for i in range(8)
                            ]}],
            })
        app.interaction = controller
        app.state.status = "Waiting for input"
        size = [Size(rows=15, columns=40)]
        app.application.output.get_size = lambda: size[0]

        def visible_panel():
            _paint(app)
            screen = app.application.renderer._last_screen
            window = next(w for w in app.application.layout.find_all_windows() if w.content is app.interaction_control)
            position = screen.visible_windows_to_write_positions[window]
            return ["".join(screen.data_buffer[y][x].char for x in range(size[0].columns))
                    for y in range(position.ypos, position.ypos + position.height)]

        def key(name):
            binding = next(b for b in reversed(app.bindings.bindings) if b.keys == (name,))
            binding.handler(None)

        initial = visible_panel()
        assert controller.title in initial[0]
        assert "↑↓ select" in "\n".join(initial)
        for direction, count in [("down", len(controller.current["options"]) - 1), ("down", 1), ("up", 1)]:
            for _ in range(count):
                key(direction)
                panel = visible_panel()
                selected = controller.current["options"][controller.option_index]["label"]
                assert selected in "\n".join(panel)
                assert "→" in "\n".join(panel)
                assert panel[0] == initial[0]
                assert panel[-1] == initial[-1]
        selected_index = controller.option_index
        for resized in [Size(rows=40, columns=100), Size(rows=24, columns=60), Size(rows=15, columns=40)]:
            size[0] = resized
            panel = visible_panel()
            assert controller.title in panel[0]
            assert controller.option_index == selected_index
            assert controller.current["options"][selected_index]["label"] in "\n".join(panel)

    _with_painted_app(f"options-follow-{kind}", scenario, lines=5)


@pytest.mark.parametrize("content", [None, "", "\n", "  \n  "])
def test_tool_tail_has_one_blank_row_before_editor(content) -> None:
    def scenario(app: CliApplication) -> None:
        app._apply_event(RunEvent(
            type="tool_started", tool_call_id="spacing-tool", name="execute",
            arguments={"command": "printf spacing-output"},
        ))
        app._apply_event(RunEvent(
            type="tool_completed", tool_call_id="spacing-tool", name="execute",
            content="spacing-output",
        ))
        if content is not None:
            app._apply_event(RunEvent(type="assistant_delta", content=content))
        _paint(app)
        screen = app.application.renderer._last_screen
        positions = screen.visible_windows_to_write_positions
        editor = next(window for window in positions if window.content is app.editor_control)
        editor_row = positions[editor].ypos
        rows = ["".join(screen.data_buffer[y][x].char for x in range(app._width())) for y in range(editor_row)]
        last_content = max(y for y, row in enumerate(rows) if row.strip())
        assert "spacing-output" in rows[last_content]
        assert editor_row - last_content == 2

    _with_painted_app("tool-editor-spacing", scenario)


@pytest.mark.parametrize('count, label', [(0, ''), (1, '1 subagent'), (2, '2 subagents')])
def test_subagent_counter_uses_existing_editor_top_row(count, label):
    def scenario(app):
        def rows():
            _paint(app)
            screen = app.application.renderer._last_screen
            columns = app.application.output.get_size().columns
            return [''.join(screen.data_buffer[y][x].char for x in range(columns))
                    for y in range(app.application.output.get_size().rows)]
        before = rows()
        app.state.apply(RunEvent(type='subagents_changed', result={'count': count}))
        after = rows()
        editor_y = next(y for y, row in enumerate(after) if row.startswith('› '))
        assert editor_y == next(y for y, row in enumerate(before) if row.startswith('› '))
        assert after[editor_y - 1].strip() == label
        if label:
            assert after[editor_y - 1].rstrip().endswith(label)
        app.state.apply(RunEvent(type='tool_started', name='task', tool_call_id='delegate'))
        app.state.apply(RunEvent(type='tool_completed', tool_call_id='delegate', content='private result'))
        cards = [block for block in app.state.blocks if isinstance(block, ToolBlock)]
        assert len(cards) == 1 and cards[0].output == 'private result'
    _with_painted_app('subagent-counter', scenario)
