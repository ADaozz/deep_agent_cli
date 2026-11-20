"""Companion geometry, offline animation, observation evidence and isolation."""
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import shutil
import signal
import sys
from threading import Event
from zoneinfo import ZoneInfo

from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
import pytest

from agent.cli.app import CliApplication
from agent.companion import STATES
from agent.companion.theme import ACCENT
from agent.companion.animation import ASSETS, frames
from agent.companion.controller import Companion
from agent.companion.monitor import Service, services
from agent.companion.worker import create_observer
from agent.execution.process import _run_process
from agent.runtime.contracts import RunEvent
from agent.runtime.runner import AgentRunner
from tests.cli_support import paint
from tests.conftest import scripted_model


@pytest.mark.parametrize("draft", ["single line", "输入 very long " * 28])
def test_resizing_preserves_draft_and_clips_both_columns(draft, monkeypatch):
    from io import StringIO
    import re

    from prompt_toolkit.output.color_depth import ColorDepth
    from prompt_toolkit.output.vt100 import Vt100_Output

    from agent.cli.gitinfo import GitSummary
    from agent.companion.theme import MUTED

    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            assert tz == ZoneInfo("Asia/Shanghai")
            return cls(2026, 11, 20, 13, 16, seconds, tzinfo=tz)

    seconds = 0
    monkeypatch.setattr("agent.companion.panel.datetime", Clock)
    monkeypatch.setattr("agent.companion.panel.detect_runtime_environment", lambda: "WSL2")
    monkeypatch.setattr("agent.companion.panel.host_distribution", lambda: "Ubuntu 24.04")

    async def run():
        nonlocal seconds
        runner = AgentRunner(model=scripted_model([]), backend=StateBackend())
        terminal = StringIO()
        output = Vt100_Output(
            terminal, lambda: Size(rows=48, columns=180), term="xterm-256color",
            default_color_depth=ColorDepth.DEPTH_24_BIT,
        )
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=output)
            app.buffer.text = draft
            app.lain_buffer.text = "Lain draft"
            app.state.add_system("tool output " * 500)
            editor_positions = {}
            for seconds, (columns, rows, count) in enumerate([
                (300, 48, 0), (240, 48, 0), (180, 48, 0), (160, 40, 2), (142, 32, 1), (120, 32, 2),
                (106, 26, 1), (80, 24, 0), (180, 48, 2),
            ]):
                app.companion.services = tuple(
                    Service(300 + i, "", 300 + i, "job", False, (8000 + i,)) for i in range(count)
                )
                app._git_summary = GitSummary(branch="master", available=True) if count else GitSummary()
                app.application.output.get_size = lambda: Size(rows=rows, columns=columns)
                paint(app)
                screen = app.application.renderer._last_screen
                width = app._width()
                assert app.transcript_window.render_info.window_width == width
                assert app.application.layout.current_window.render_info.window_width == width - 2
                assert app.buffer.text == draft
                if width == columns:
                    continue
                positions = screen.visible_windows_to_write_positions
                main_editor = positions[app.application.layout.current_window]
                lain_editor = next(pos for window, pos in positions.items() if window.content is app.lain_editor)
                assert lain_editor.ypos == main_editor.ypos + main_editor.height - 1
                prompt = screen.data_buffer[lain_editor.ypos][lain_editor.xpos - 2]
                assert prompt.char == "›" and ACCENT in prompt.style
                assert screen.data_buffer[lain_editor.ypos][lain_editor.xpos].char == "L"
                for main_y, lain_y in (
                    (main_editor.ypos - 1, lain_editor.ypos - 1),
                    (main_editor.ypos, lain_editor.ypos),
                    (main_editor.ypos + main_editor.height, lain_editor.ypos + 1),
                ):
                    main_cell = screen.data_buffer[main_y][main_editor.xpos]
                    lain_cell = screen.data_buffer[lain_y][lain_editor.xpos]
                    for cell in (main_cell, lain_cell):
                        assert app.application.style.get_attrs_for_style_str(cell.style).bgcolor == "303030"
                assert screen.data_buffer[lain_editor.ypos - 1][lain_editor.xpos].char == " "
                assert screen.data_buffer[lain_editor.ypos + 1][lain_editor.xpos].char == " "
                assert all(screen.data_buffer[y][width].char == "│" for y in range(2, rows))
                right = "\n".join("".join(screen.data_buffer[y][x].char for x in range(width+1, columns))
                                  for y in range(rows))
                for label in (
                    "CPU", "GPU", "RAM", "DISK", "GIT", "SERVICES",
                    "Talk TO Lain", "Ctrl←/→", "Tool Health",
                ):
                    assert label in right
                panel = right.splitlines()[2:]
                timestamp = f"2026-11-20 13:16:{seconds:02d}"
                separator = "·" if columns == 120 else " · "
                suffix = " · Ubuntu 24.04" if columns == 300 else ""
                assert panel[0].endswith(f"{timestamp}{separator}WSL2{suffix}  ")
                if not suffix:
                    assert panel[1].endswith("Ubuntu 24.04  ")
                title = panel[0].split(timestamp, 1)[0].strip()
                assert title == (
                    "Companion Agent [ Lain Iwakura ]" if columns >= 240 else
                    "Lain Iwakura" if columns >= 160 else "Lain"
                )
                heading = next(i for i, row in enumerate(panel) if "SERVICES" in row)
                assert [text.strip() for text in panel[heading].split("│")] == ["GIT", "SERVICES"]
                assert [text.strip() for text in panel[heading + 1].split("│")] == [
                    "master · clean" if count else "No repository", f"{count} running",
                ]
                chart_heading = next(i for i, row in enumerate(panel) if "Tool Health" in row)
                assert chart_heading == heading + 3 + count
                assert set(panel[chart_heading - 1].strip()) == {"─"}
                assert panel[chart_heading + 1].strip() == "No tool calls yet"
                assert not panel[chart_heading + 2].strip()
                assert panel[chart_heading + 3].strip().startswith("Talk TO Lain")
                watching = chart_heading + 4
                assert panel[watching].strip() == "Watching current session..."
                assert MUTED in screen.data_buffer[watching + 2][width + 3].style
                editor_positions[columns] = lain_editor.ypos
                assert all(row.startswith("  ") and row.endswith("  ") for row in panel)
                assert columns - width - 1 == round(columns * .28)
                assert any(row.strip() == "IDLE" for row in panel)
                resources = next(i for i, row in enumerate(panel) if "CPU" in row)
                assert panel[resources].count(" │ ") == 3
                resource_dividers = [i for i, char in enumerate(panel[resources]) if char == "│"]
                assert panel[heading].index("│") == resource_dividers[1]
                assert panel[heading + 1].index("│") == resource_dividers[1]
                title_divider = 1 if suffix else 2
                for divider in (title_divider, resources - 1, resources + 2, chart_heading - 1):
                    assert panel[divider].strip() == "─" * (columns - width - 5)
                    assert ACCENT in screen.data_buffer[divider + 2][width + 3].style
                for service in app.companion.services:
                    assert f"{service.pid}" in right
                    assert f":{service.ports[0]}" in right
                assert "tool output" not in right
            chart_y = chart_heading + 2

            def assert_live_cell(expected_rate, expected_glyph):
                paint(app)
                screen = app.application.renderer._last_screen
                def side_row(y):
                    return "".join(screen.data_buffer[y][x].char for x in range(app._width(), 180))

                assert expected_rate in side_row(chart_y)
                assert expected_glyph in side_row(chart_y + 2)

            app.companion.event(RunEvent("tool_started", tool_call_id="live", name="read"), "fixed model")
            app.companion.event(RunEvent("tool_completed", tool_call_id="live"), "fixed model")
            assert_live_cell("100% success", "■")
            app.companion.event(RunEvent("tool_started", tool_call_id="live-failed", name="read"), "fixed model")
            app.companion.event(RunEvent("tool_completed", tool_call_id="live-failed", is_error=True), "fixed model")
            assert_live_cell("50% success", "×")
            for i, name in enumerate(("read", "write", "execute", "search", "grep")):
                app.companion.event(RunEvent("tool_started", tool_call_id=str(i), name=name), "fixed model")
                app.companion.event(RunEvent("tool_completed", tool_call_id=str(i), is_error=i == 0), "fixed model")
            for columns, rows in ((180, 48), (120, 32)):
                terminal.seek(0)
                terminal.truncate()
                app.application.output.get_size = lambda: Size(rows=rows, columns=columns)
                paint(app)
                if columns == 180:
                    update = terminal.getvalue()
                    assert "\x1b[J" not in update and "\x1b[2J" not in update
                    sgr = re.findall(r"\x1b\[([0-9;]+)m", update)
                    assert any("38;2;255;85;133" in sequence for sequence in sgr)
                    assert any("38;2;255;213;236" in sequence for sequence in sgr)
                screen = app.application.renderer._last_screen
                width = app._width()
                side_rows = ["".join(screen.data_buffer[y][x].char for x in range(width + 1, columns))
                             for y in range(rows)]
                chat_y = next(y for y, row in enumerate(side_rows) if "Talk TO Lain" in row)
                assert not side_rows[chat_y - 1].strip()
                assert side_rows[chat_y - 2].strip().endswith("failed")
                assert side_rows[chat_y + 1].strip() == "Watching current session..."
                editor = next(pos for window, pos in screen.visible_windows_to_write_positions.items()
                              if window.content is app.lain_editor)
                assert editor.ypos == editor_positions[columns]
                side_cells = [screen.data_buffer[y][x] for y in range(2, chat_y) for x in range(width, columns)]
                assert any(cell.char == "×" and "#FF5585" in cell.style for cell in side_cells)
                assert "× Failed" in "".join(cell.char for cell in side_cells)
        runner.close()
    asyncio.run(run())


def test_mouse_drag_copies_lain_chat_and_preserves_scroll_and_input_focus(monkeypatch):
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    copied = []
    monkeypatch.setattr(
        "agent.cli.app.copy_to_clipboard", lambda content, **kwargs: copied.append(content)
    )

    async def run():
        runner = AgentRunner(model=scripted_model([]), backend=StateBackend())
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            application = app.application
            application.output.get_size = lambda: Size(rows=60, columns=180)
            for i in range(20):
                app.companion.log(f"history {i}")
            app.companion.log("你好", speaker="You")
            # Use a Markdown hard line break so the drag spans two rendered rows.
            app.companion.log("玲音 alpha line  \n下一行 beta")
            paint(app)
            application.layout.update_parents_relations()
            renderer = application.renderer
            screen = renderer._last_screen
            side_x = app._width() + 1

            def find_line(text):
                for y in range(60):
                    cells = screen.data_buffer[y]
                    visible = "".join(cells[x].char for x in range(side_x, 180))
                    if text in visible:
                        return y, cells
                raise AssertionError(f"Missing sidebar line: {text}")

            first_y, first_cells = find_line("玲音 alpha line")
            last_y, last_cells = find_line("下一行 beta")
            assert first_y < last_y
            _, hint_cells = find_line("Ctrl←/→")
            hint_start = next(x for x in range(side_x, 180) if hint_cells[x].char == "C")
            _, user_cells = find_line("你 · ")
            user_start = next(x for x in range(side_x, 180) if user_cells[x].char == "你")
            _, lain_cells = find_line("玲音 · ")
            lain_start = next(x for x in range(side_x, 180) if lain_cells[x].char == "玲")
            assert ACCENT in hint_cells[hint_start].style
            assert ACCENT in user_cells[user_start].style
            assert ACCENT in lain_cells[lain_start].style
            first_x = next(x for x in range(side_x, 180) if first_cells[x].char == "玲")
            last_x = max(x for x in range(side_x, 180) if last_cells[x].char == "a")

            def mouse(kind, x, y, button=MouseButton.LEFT):
                handler = renderer.mouse_handlers.mouse_handlers[y][x]
                with set_app(application):
                    handler(MouseEvent(Point(x=x, y=y), kind, button, frozenset()))

            mouse(MouseEventType.MOUSE_DOWN, first_x + 1, first_y)
            mouse(MouseEventType.MOUSE_MOVE, last_x, last_y)
            paint(app)
            selected = renderer._last_screen.data_buffer[first_y][first_x]
            assert "class:transcript-selection" in selected.style
            mouse(MouseEventType.MOUSE_UP, last_x, last_y)
            async with asyncio.timeout(2):
                while not copied:
                    await asyncio.sleep(0.01)
            assert [line.lstrip() for line in copied[0].splitlines()] == [
                "玲音 alpha line", "下一行 beta",
            ]
            assert application.layout.current_control is app.editor_control

            mouse(MouseEventType.SCROLL_UP, last_x, last_y, MouseButton.NONE)
            assert app.companion.anchor is not None
            editor = next(
                pos for window, pos in renderer._last_screen.visible_windows_to_write_positions.items()
                if window.content is app.lain_editor
            )
            mouse(MouseEventType.MOUSE_DOWN, editor.xpos, editor.ypos)
            mouse(MouseEventType.MOUSE_UP, editor.xpos, editor.ypos)
            assert application.layout.current_control is app.lain_editor
            assert len(copied) == 1
            chart_y, chart_cells = find_line("Tool Health")
            chart_x = next(x for x in range(side_x, 180) if chart_cells[x].char == "T")
            mouse(MouseEventType.MOUSE_DOWN, chart_x, chart_y)
            mouse(MouseEventType.MOUSE_UP, chart_x + len("Tool Health") - 1, chart_y)
            async with asyncio.timeout(2):
                while len(copied) < 2:
                    await asyncio.sleep(0.01)
            assert copied[1] == "Tool Health"
        runner.close()

    asyncio.run(run())


@pytest.mark.parametrize("behavior", STATES)
def test_each_supplied_gif_animates_inside_a_fixed_viewport(behavior):
    assert json.loads((ASSETS / "manifest.json").read_text())["states"][behavior]["file"] == f"lain_{behavior}.gif"
    rendered = []
    for fragments, delay in frames(behavior, 64, 16):
        assert delay > 0
        text = "".join(s for _, s in fragments)
        assert [len(row) for row in text.splitlines()] == [64] * 16
        assert all(color == ACCENT for color, _ in fragments)
        assert all(c in " \n" or 0x2801 <= ord(c) <= 0x28ff for c in text)
        assert all(row[:6].isspace() for row in text.splitlines())
        rendered.append(text)
    assert len(set(rendered)) > 1
    assert len({tuple(text.splitlines()[-5:]) for text in rendered}) > 1
    # Each clip loops on its own first/last frame. Non-idle sprites use a
    # 418x418 canvas and do not share idle's 400x412 neutral frame.
    assert rendered[0] == rendered[-1]


@pytest.mark.parametrize(
    ("source_size", "expected"),
    [((60, 20), (24, 4)), ((20, 40), (12, 12)), ((400, 412), (23, 12))],
)
def test_animation_fits_source_aspect_and_centers_it(tmp_path, monkeypatch, source_size, expected):
    from PIL import Image

    Image.new("RGB", source_size, "white").save(tmp_path / "lain_idle.gif")
    monkeypatch.setattr("agent.companion.animation.ASSETS", tmp_path)
    width, height = 24, 12
    fragments, _ = next(frames("idle", width, height))
    cols, rows = expected
    left = (width - cols) // 2
    top = (height - rows) // 2
    content = (" " * left + "⣿" * cols).ljust(width)
    assert fragments[0][1].splitlines() == [
        " " * width if row < top or row >= top + rows else content
        for row in range(height)
    ]


def companion(tmp_path, session):
    return Companion(workspace=tmp_path, config_dir=tmp_path, session=session, geometry=lambda: (64,16),
                     invalidate=lambda: None, timezone=ZoneInfo("UTC"))


def test_statistics_idempotent_attributed_at_start_and_persisted(tmp_path):
    async def run():
        c = companion(tmp_path, lambda: "session")
        c.event(RunEvent("run_started"), "first")
        for i in range(3):
            event = RunEvent("tool_started", tool_call_id=str(i), name="read", message_id="parallel")
            c.event(event, "first")
            c.event(event, "second")
        c.event(RunEvent("tool_started", tool_call_id="four", name="edit"), "second")
        c.event(RunEvent("tool_completed", tool_call_id="0"), "second")
        c.event(RunEvent("tool_completed", tool_call_id="1", is_error=True), "second")
        c.event(RunEvent("run_completed"), "second")
        assert c.batches == {"parallel": ["read"] * 3}
        await c.save_statistics()
        other = companion(tmp_path, lambda: "session")
        await other.save_statistics(read=True)
        series = other.timeline.series
        assert series["first","default","read"].counts == {"success":1, "failed":1, "unknown":1}
        assert series["second","default","edit"].counts == {"unknown":1}
    asyncio.run(run())


def test_real_react_graph_can_express_or_remain_silent():
    async def run():
        observe = AIMessage(content="", tool_calls=[dict(name="observe", args={}, id="look")])
        speak = AIMessage(content="", tool_calls=[dict(name="express", args=dict(behavior="curious", message="看到变化了。"), id="show")])
        respond = create_observer(scripted_model([observe, speak, AIMessage(content="")]))
        assert await respond({"windows": []}) == {"behavior":"curious","message":"看到变化了。"}
        assert await respond({"windows": []}) == {}
    asyncio.run(run())


def test_persona_is_shared_and_each_graph_gets_its_own_instructions(monkeypatch):
    from langchain_core.messages import AIMessageChunk

    from agent.companion.worker import create_chat

    prompts = []
    requests = []
    chunks = []
    environment = "环境信息\nos=Ubuntu 24.04 runtime=WSL2"

    class Graph:
        async def ainvoke(self, data, config):
            system, user = data["messages"]
            prompts.append(system.content)
            requests.append(user[1])
            if "callbacks" not in config:
                return {}
            callback = config["callbacks"][0]
            callback.on_llm_start()
            for text in ("<thi", "nk>内部内容</think>我看到", "了一次超时。"):
                callback.on_llm_new_token(text, chunk=AIMessageChunk(content=text))
            return {"messages": [AIMessage(content="我看到了一次超时。")]}

    monkeypatch.setattr("agent.companion.worker.create_react_agent", lambda model, tools: Graph())
    observer = create_observer(None, persona="Shared persona", instructions="Observe only")
    chat = create_chat(None, None, persona="Shared persona", instructions="Chat only")
    assert asyncio.run(observer({"execute_environment": environment})) == {}
    snapshot = {"question": "为什么execute工具出错多", "tool_evidence": [], "execute_environment": environment}
    assert asyncio.run(chat(snapshot, on_chunk=chunks.append)) == {
        "message": "我看到了一次超时。",
    }
    for prompt, instruction in zip(prompts, ("Observe only", "Chat only"), strict=True):
        assert prompt.startswith("Shared persona") and prompt.endswith(f"{instruction}\n\n{environment}")
    assert chunks == ["我看到", "我看到了一次超时。"]
    assert requests[1].endswith("\n为什么execute工具出错多")
    assert '"tool_evidence": []' in requests[1]
    assert '"execute_environment"' not in requests[1]


def test_late_reply_from_old_session_and_worker_crash(tmp_path, monkeypatch):
    async def run():
        session = ["first"]
        c = companion(tmp_path, lambda: session[0])
        # Separate fake process waits on a test signal before sending a stale reply.
        signal_path = tmp_path / "release"
        code = """
import json, sys, time
from pathlib import Path
print('{"status":"ready"}', flush=True)
r = json.loads(input())
p = Path(sys.argv[1])
p.with_suffix('.waiting').touch()
while not p.exists(): time.sleep(.01)
print(json.dumps(dict(session=r['session'],seq=r['seq'],token=r['token'],behavior='curious',message='stale',status='ok')),flush=True)
"""
        async def launch(*args):
            return await asyncio.create_subprocess_exec(sys.executable,"-c",code,str(signal_path),
                                                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        monkeypatch.setattr("agent.companion.controller.start_worker",launch)
        for i in range(3):
            c.event(RunEvent("tool_started",tool_call_id=str(i),name="write"),"a")
            c.event(RunEvent("tool_completed",tool_call_id=str(i),is_error=True),"a")
        c.event(RunEvent("run_completed"),"a")
        task = asyncio.create_task(c.worker_session())
        async with asyncio.timeout(5):
            while not signal_path.with_suffix(".waiting").exists():
                await asyncio.sleep(.01)
            session[0] = "second"
            c.sync_session()
            signal_path.touch()
            await task
        assert c.connection == "STARTING"
        assert all(text != "stale" for _, _, text in c.logs)
    asyncio.run(run())


def test_host_background_service_outlives_launch_shell(tmp_path):
    result = _run_process("sleep 60 </dev/null >/dev/null 2>&1 & echo $!", shell=True, cwd=tmp_path,
                          timeout=3, max_output_bytes=4096)
    pid = int(result.output.strip())
    try:
        assert any(row.pid == pid and row.name == "sleep" for row in services())
    finally:
        os.kill(pid, signal.SIGTERM)


@pytest.mark.sandbox
def test_sandbox_services_use_namespace_pids_and_expire(tmp_path):
    from time import monotonic, sleep

    from agent.execution.backend import BubblewrapBackend
    from agent.execution.policy import SandboxConfig
    if not shutil.which("bwrap"):
        pytest.skip("Bubblewrap unavailable")
    probe = subprocess.run([shutil.which("bwrap"), "--unshare-user", "--unshare-net", "--ro-bind", "/", "/", "true"],
                           capture_output=True, text=True)
    if probe.returncode and any(s in probe.stderr.lower() for s in ("not permitted", "permission denied", "namespace")):
        if os.getenv("REQUIRE_BWRAP_TEST") == "1":
            pytest.fail(probe.stderr)
        pytest.skip(probe.stderr)
    assert probe.returncode == 0, probe.stderr
    backend = BubblewrapBackend(SandboxConfig(workspace=tmp_path), executable=shutil.which("bwrap"))
    try:
        result = backend.execute("python3 -m http.server 8001 >/dev/null 2>&1 & echo $!; "
                                 "python3 -m http.server 8002 >/dev/null 2>&1 & echo $!; "
                                 "sleep 60 >/dev/null 2>&1 & echo $!",timeout=5)
        assert result.exit_code == 0, result.output
        ids = set(map(int, result.output.split()))
        deadline = monotonic() + 3
        while True:
            rows = [row for row in services() if row.sandbox]
            ports = {port for row in rows for port in row.ports}
            if {8001, 8002} <= ports or monotonic() >= deadline:
                break
            sleep(.05)
        assert {row.pid for row in rows} == ids
        assert sorted(row.name for row in rows) == ["http.server","http.server","sleep"]
        assert sorted(row.ports for row in rows) == [(), (8001,), (8002,)]
    finally:
        backend.pool.shutdown()
    assert not any(row.sandbox for row in services())


def test_log_anchor_survives_appends_and_resumes_at_bottom(tmp_path):
    from agent.companion.panel import ChatRows

    c = companion(tmp_path, lambda: "session")
    view = ChatRows(c)
    for i in range(15):
        c.log(f"line {i} " + "长文本" * 8)
    newest = view.visible_lines(30, 4)
    view.scroll_lines(-1)
    pinned = view.visible_lines(30, 4)
    assert pinned != newest
    c.log("the next event")
    assert view.visible_lines(30, 4) == pinned
    view.scroll_lines(100)
    assert c.anchor is None
    assert "the next event" in "".join(text for row in view.visible_lines(30, 4) for _,text in row)


def test_chat_rows_render_markdown_like_main_transcript(tmp_path):
    from agent.companion.panel import ChatRows

    c = companion(tmp_path, lambda: "session")
    c.log("查到了：**失败率 40%**\n\n- execute\n- read_file")
    plain = "\n".join(
        "".join(text for _, text in line)
        for line in ChatRows(c).visible_lines(48, 12)
    )
    assert "失败率 40%" in plain
    assert "**失败率" not in plain
    assert "execute" in plain
    assert "read_file" in plain


def test_broken_clip_is_local_and_other_behavior_recovers(tmp_path, monkeypatch):
    from agent.companion.config import CompanionConfig

    c = companion(tmp_path, lambda: "session")
    custom_idle = tmp_path / "custom-idle.gif"
    c.config = CompanionConfig(gifs={"idle": custom_idle})

    def clips(state, width, height, *, path=None):
        if state == "idle":
            assert path == custom_idle
            raise ValueError("corrupt GIF")
        assert path is None
        yield [("", "frame")], .12

    monkeypatch.setattr("agent.companion.controller._frames", clips)

    async def run():
        task = asyncio.create_task(c.animate())
        try:
            async with asyncio.timeout(5):
                while not c.frame:
                    await asyncio.sleep(.02)
                assert c.frame[0][1] == "Animation unavailable"
                c.event(RunEvent("run_started"), "model")
                while c.frame != [("", "frame")]:
                    await asyncio.sleep(.02)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_companion_finishes_each_animation_before_playing_queued_states(tmp_path, monkeypatch):
    c = companion(tmp_path, lambda: "session")
    decoding_focused = Event()
    allow_focused = Event()

    def clips(state, width, height, *, path=None):
        if state == "focused":
            decoding_focused.set()
            assert allow_focused.wait(2)
        for frame in (1, 2):
            yield [(ACCENT, f"{state}:{frame}")], .04

    monkeypatch.setattr("agent.companion.controller._frames", clips)

    async def run():
        displayed = []
        first_frame = asyncio.Event()
        finished = asyncio.Event()

        def invalidate():
            if not c.frame:
                return
            frame = c.frame[0][1]
            if not displayed or displayed[-1] != frame:
                displayed.append(frame)
            first_frame.set()
            if frame == "relieved:1":
                finished.set()

        c.invalidate = invalidate
        task = asyncio.create_task(c.animate())
        try:
            async with asyncio.timeout(3):
                await first_frame.wait()
                c.event(RunEvent("run_started"), "model")
                c.event(RunEvent("tool_started", name="read", tool_call_id="a"), "model")
                c.event(RunEvent("tool_started", name="edit", tool_call_id="b"), "model")
                c.event(RunEvent("run_completed"), "model")
                assert c.behavior == "idle"
                while not decoding_focused.is_set():
                    await asyncio.sleep(.01)
                assert c.frame == [(ACCENT, "idle:2")]
                allow_focused.set()
                await finished.wait()
            assert displayed == [
                "idle:1", "idle:2",
                "focused:1", "focused:2",
                "observing:1", "observing:2",
                "relieved:1",
            ]
            assert not c.behavior_queue
        finally:
            allow_focused.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_repository_reads_only_workspace_source(tmp_path):
    from agent.companion.repository import Repository

    root = tmp_path / "repo"
    root.mkdir()
    (root / "code.py").write_text("import widget\nwidget.load()\n")
    private = root / "private"
    private.mkdir()
    (private / "state.json").write_text("excluded")
    (root / ".env").write_text("excluded")
    outside = tmp_path / "outside.py"
    outside.write_text("excluded")
    (root / "alias.py").symlink_to(outside)
    (root / "linked").symlink_to(tmp_path, target_is_directory=True)
    os.link(outside, root / "hard.py")
    os.mkfifo(root / "pipe.py")

    repo = Repository(root, [private])
    assert repo.read("code.py", 2) == "code.py:2 widget.load()"
    assert "code.py:1 import widget" in repo.search("widget")
    for path in ("../outside.py", str(outside), "alias.py", "linked/outside.py", "hard.py",
                 "private/state.json", ".env", "pipe.py"):
        assert repo.read(path) == "File unavailable."
    assert repo.search("excluded").startswith("No match")


def test_chat_receives_session_evidence_without_waiting_for_observer_threshold(tmp_path, monkeypatch):
    from agent.companion.panel import ChatRows

    async def run():
        session = ["one"]
        c = companion(tmp_path, lambda: session[0])
        c.execute_environment = lambda: "环境信息\nos=Ubuntu 24.04 runtime=WSL2"
        release = tmp_path / "finish-reply"
        clock = [100.0]
        monkeypatch.setattr("agent.companion.controller.monotonic", lambda: clock[0])
        code = """
import json, sys, time
from pathlib import Path
print('{"status":"ready"}', flush=True)
r = json.loads(input())
q = r['snapshot']
assert q['question'] == '为什么execute工具出错多'
assert q['execute_environment'].splitlines() == ['环境信息', 'os=Ubuntu 24.04 runtime=WSL2']
assert q['recent_errors'] == [['execute', 'command not found']]
assert len(q['tool_evidence']) == 1
assert q['tool_evidence'][0]['name'] == 'execute'
assert q['tool_evidence'][0]['error_category'] == 'dependency'
assert q['tool_evidence'][0]['tool_call_id'] == 'a'
identity = {k: r[k] for k in ('session', 'seq', 'token')}
print(json.dumps(dict(**identity, partial='这次是')), flush=True)
while not Path(sys.argv[1]).exists(): time.sleep(.01)
print(json.dumps(dict(**identity, message='这次是命令找不到。', status='ok')), flush=True)
"""
        async def launch(*args):
            return await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(release),
                                                        stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.PIPE)
        monkeypatch.setattr("agent.companion.controller.start_worker", launch)
        c.event(RunEvent("tool_started", tool_call_id="a", name="execute"), "fixed model")
        c.event(RunEvent("tool_completed", tool_call_id="a", is_error=True, content="command not found",
                         artifact={"exit_code": 127}), "fixed model")
        c.ask("为什么execute工具出错多")
        rows = ChatRows(c)

        def rendered():
            return "\n".join("".join(text for _, text in line) for line in rows.visible_lines(45, 10))

        for dots in ("·", "··", "···", "·"):
            assert rendered().splitlines()[-1] == f"玲音 {dots}"
            clock[0] += 0.26
        task = asyncio.create_task(c.worker_session())
        try:
            async with asyncio.timeout(5):
                while c.reply_stamp is None:
                    await asyncio.sleep(.01)
                stamp = c.reply_stamp
                assert f"玲音 · {stamp[:5]}" in rendered()
                assert "这次是" in rendered()
                assert c.reply_refresh is None
                assert all(role != "Lain" for _, role, _ in c.logs)
                release.touch()
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert any((role, msg) == ("You", "为什么execute工具出错多") for _, role, msg in c.logs)
        assert [(time, msg) for time, role, msg in c.logs if role == "Lain"] == [(stamp, "这次是命令找不到。")]
        assert c.chat_preview() is None
        session[0] = "two"
        c.sync_session()
        assert not (c.logs or c.errors or c.question)
    asyncio.run(run())


def test_sensitive_names_and_embedded_credentials_are_unavailable(tmp_path):
    from agent.companion.repository import Repository
    repo = Repository(tmp_path, secrets=("fixture-sensitive-value",))
    for name, content in (("credentials.json",'{"value":"private"}'),("config.yaml","private"),
                          ("innocent.py","print('fixture-sensitive-value')"),
                          ("example.md","api_key = 'private-value'")):
        (tmp_path/name).write_text(content)
        assert repo.read(name) == "File unavailable."
    assert repo.search("private").startswith("No match")


def test_quiet_activity_does_not_request_model(tmp_path, monkeypatch):
    async def run():
        c = companion(tmp_path, lambda:"s")
        def launch(*_):
            return asyncio.create_subprocess_exec(sys.executable,"-c",
                "import sys;print('{\u0022status\u0022:\u0022ready\u0022}',flush=True);"
                "sys.stdin.readline();sys.exit(2)", stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE)
        monkeypatch.setattr("agent.companion.controller.start_worker",launch)
        task = asyncio.create_task(c.worker_session())
        while not c.worker_ready:
            await asyncio.sleep(.01)
        for i in range(5):
            c.event(RunEvent("tool_started",name="read",tool_call_id=str(i)),"m")
            c.event(RunEvent("tool_completed",tool_call_id=str(i)),"m")
        c.timeline.sample("services", ["service"], c.turn)
        c.timeline.sample("resources", [1,None,2,3], c.turn)
        await c.save_statistics()
        assert not c.timeline.queue and c.request_version == 0 and not task.done()
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())


def test_missing_key_keeps_local_evidence_and_chart(tmp_path, monkeypatch):
    from agent.companion.chart import ActivityHeatmap

    async def run():
        monkeypatch.delenv("COMPANION_DEEPSEEK_API_KEY", raising=False)
        c = companion(tmp_path, lambda:"one")
        assert await c.worker_session() == "no key"
        c.event(RunEvent("tool_started", name="read", tool_call_id="id"),"fixed model")
        c.event(RunEvent("tool_completed", tool_call_id="id", is_error=True),"fixed model")
        assert "1 call · 0 success · 1 failed" in str(ActivityHeatmap(c).draw(46, 9))
        assert c.request_version == 0
        await c.finish()
    asyncio.run(run())


def test_old_worker_cannot_read_new_session(tmp_path, monkeypatch):
    async def run():
        session = ["old"]
        c = companion(tmp_path, lambda:session[0])
        release = tmp_path/"release"
        code = """
import json,sys,time
from pathlib import Path
p=Path(sys.argv[1])
print(json.dumps({'status':'ready'}),flush=True)
r=json.loads(input())
identity={k:r[k] for k in ('session','seq','token')}
p.with_suffix('.waiting').touch()
while not p.exists(): time.sleep(.01)
print(json.dumps(dict(**identity,query='turns',args={})),flush=True)
p.with_suffix('.result').write_text(json.loads(input())['data'])
print(json.dumps(dict(**identity,status='ok',message='old answer')),flush=True)
"""
        async def launch(*args):
            return await asyncio.create_subprocess_exec(sys.executable,"-c",code,str(release),
                                                       stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE)
        monkeypatch.setattr("agent.companion.controller.start_worker",launch)
        c.ask("review")
        task=asyncio.create_task(c.worker_session())
        async with asyncio.timeout(6):
            while not release.with_suffix(".waiting").exists():
                await asyncio.sleep(.01)
            session[0]="new"
            c.sync_session()
            release.touch()
            await task
        assert not release.with_suffix(".result").exists()
        assert not any(msg=="old answer" for _,_,msg in c.logs)
    asyncio.run(run())


def test_inspector_live_completion_cache_and_session_boundary(tmp_path):
    import json

    from agent.companion.session_inspector import build_session_inspector
    from agent.sessions.contracts import TranscriptBlock

    async def run():
        session = ["one"]
        c = companion(tmp_path, lambda: session[0])
        checkpoint = []
        c.inspect_session = build_session_inspector(
            load_blocks=lambda sid, budget: list(checkpoint) if sid == "one" else [],
            companion_provider=lambda: c,
        )

        async def query(**args):
            return json.loads(await c.host_query("inspect_current_session", args))

        c.event(RunEvent("run_started", run_id="run", turn_id="turn"), "model")
        c.event(RunEvent("tool_started", tool_call_id="a", name="execute", message_id="message",
                         arguments={"command": "pytest -q", "api_key": "opaque-private-value"}), "model")
        assert (await query())["tools"][0]["status"] == "running"
        c.event(RunEvent("tool_completed", tool_call_id="a", content="AssertionError: failed",
                         artifact={"exit_code": 1}, is_error=True), "model")
        detail = dict(view="tool_detail", tool_call_id="a")
        result = await query(**detail)
        assert result["arguments"]["command"] == "pytest -q"
        assert "opaque-private-value" not in json.dumps(result)
        assert result["result"]["error_category"] == "assertion"
        assert result["result"]["output_excerpt"] == "AssertionError: failed"
        checkpoint.append(TranscriptBlock(kind="tool", tool_call_id="a", name="execute",
                                          message_id="message", status="error"))
        assert len((await query())["tools"]) == 1

        for i in range(130):
            c.event(RunEvent("tool_started", tool_call_id=str(i), name="read_file"), "model")
            c.event(RunEvent("tool_completed", tool_call_id=str(i), content=f"output {i}"), "model")
        assert len(c.call_cache) == 128
        assert not (await query(**detail))["result"]["output_excerpt"]
        old_request = c.inspect_session(**detail)
        session[0] = "two"
        c.sync_session()
        assert old_request()["error"]["code"] == "request_expired"
        assert (await query(**detail))["error"]["code"] == "tool_call_not_found"
        assert not c.call_cache

    asyncio.run(run())


def test_host_query_timeout_stops_the_reader_and_next_query_works(tmp_path):
    from threading import Event
    from time import sleep

    from agent.companion.config import CompanionConfig

    async def run():
        c = companion(tmp_path, lambda: "session")
        c.config = CompanionConfig(rpc_timeout_seconds=.06)
        finished = Event()

        def slow(budget):
            try:
                while True:
                    budget.check()
                    sleep(.005)
            finally:
                finished.set()

        c.inspect_session = lambda **args: slow
        response = json.loads(await c.host_query("inspect_current_session", {}))
        assert response["error"]["code"] == "tool_timeout"
        assert await asyncio.to_thread(finished.wait, .5)
        c.inspect_session = lambda **args: lambda budget: {"tools": [], "truncated": False}
        assert json.loads(await c.host_query("inspect_current_session", {}))["tools"] == []

    asyncio.run(run())


def test_worker_ipc_recovers_after_tool_and_model_timeouts():
    from agent.companion.config import CompanionConfig
    from agent.companion.worker import serve
    from httpx import ReadTimeout

    async def run():
        reader = asyncio.StreamReader()
        outputs = asyncio.Queue()
        queries = []

        def build_chat(query):
            async def chat(snapshot, *, on_chunk):
                if snapshot["question"] == "model timeout":
                    raise ReadTimeout("private API detail")
                result = await query("turns")
                queries.append(json.loads(result))
                if snapshot["question"] == "tool timeout":
                    return {"message": "Tool timed out"}
                return {"message": "Recovered"}

            return chat

        async def observe(snapshot):
            return {}

        task = asyncio.create_task(serve(
            reader, outputs.put_nowait, build_chat=build_chat, observe=observe,
            config=CompanionConfig(rpc_timeout_seconds=.06, question_timeout_seconds=1),
        ))

        def feed(packet):
            reader.feed_data(json.dumps(packet).encode() + b"\n")

        def request(seq, question):
            identity = dict(session="session", seq=seq, token=str(seq))
            feed(dict(**identity, snapshot={"question": question}))
            return identity

        async with asyncio.timeout(3):
            assert (await outputs.get())["status"] == "ready"
            first = request(1, "tool timeout")
            first_query = await outputs.get()
            assert (await outputs.get())["message"] == "Tool timed out"
            assert queries[0]["error"]["code"] == "tool_timeout"
            request(2, "model timeout")
            error = await outputs.get()
            assert error["status"] == "model_timeout"
            assert "private API detail" not in json.dumps(error)
            identity = request(3, "recover")
            query = await outputs.get()
            feed(dict(**first, query_id=first_query["query_id"], data="late"))
            feed(dict(**identity, query_id=query["query_id"], data='{"records": []}'))
            assert (await outputs.get())["message"] == "Recovered"
            assert queries[-1]["records"] == []
            reader.feed_eof()
            await task

    asyncio.run(run())


def test_inspector_reads_real_restored_checkpoint(tmp_path):
    from agent.sessions.store import SessionStore

    async def run():
        store = SessionStore(tmp_path / "sessions.sqlite3")
        model = scripted_model([AIMessage(content="checkpoint answer")])
        runner = AgentRunner(model=model, backend=StateBackend(), session_store=store)
        try:
            runner.invoke("checkpoint question")
            with create_pipe_input() as pipe:
                app = CliApplication(runner, config_dir=tmp_path, input=pipe, output=DummyOutput())
                result = json.loads(await app.companion.host_query(
                    "inspect_current_session", {"view": "recent_messages"},
                ))
            assert not result.get("checkpoint_unavailable")
            assert [(row["role"], row["content"]) for row in result["messages"]] == [
                ("user", "checkpoint question"), ("assistant", "checkpoint answer"),
            ]
        finally:
            runner.close()
            store.close()

    asyncio.run(run())


def test_source_reader_paginates_and_search_reports_partial_scan(tmp_path, monkeypatch):
    from agent.companion import repository

    path = tmp_path / "example.py"
    path.write_text("".join(f"line_{number}\n" for number in range(1, 63)))
    (tmp_path / "other.py").write_text("other content")
    repo = repository.Repository(tmp_path)
    result = repo.read(path.name, start=2, limit=50)
    assert f"{path.name}:51 line_51" in result
    assert "Continue with start=52" in result
    monkeypatch.setattr(repository, "SEARCH_BYTES", 2)
    assert "Scan incomplete: byte budget" in repo.search("absent")
    monkeypatch.setattr(repository, "SEARCH_ENTRIES", 1)
    assert "entry budget" in repo.search("absent")
    monkeypatch.setattr(repository, "SEARCH_SECONDS", 0)
    assert "Scan incomplete: time budget" in repo.search("absent")


def test_evidence_filters_failure_changes_and_checks_retry_arguments(tmp_path):
    async def run():
        c = companion(tmp_path, lambda: "session")
        c.event(RunEvent("run_started", run_id="r", turn_id="t"), "model")
        attempts = (
            ("a", "python -m pytest", "No module named pytest", 1),
            ("b", "python -m pytest", "No module named pytest", 1),
            ("c", "pytest -q", "AssertionError: assertion failed", 1),
            ("d", "pytest -q", "2 passed", 0),
        )
        for call_id, command, output, code in attempts:
            c.event(RunEvent("tool_started", tool_call_id=call_id, name="execute",
                             arguments={"command": command}), "model")
            c.event(RunEvent("tool_completed", tool_call_id=call_id, content=output,
                             artifact={"exit_code": code}), "model")
        c.event(RunEvent("tool_started", tool_call_id="read", name="read_file"), "model")
        c.event(RunEvent("tool_completed", tool_call_id="read", content="other tool"), "model")
        options = dict(tool_name="execute", turn_id="t")
        failures = json.loads(await c.host_query("failures", {**options, "limit": 2}))
        assert failures["truncated"]
        assert [row["tool_call_id"] for row in failures["records"]] == ["b", "c"]
        assert failures["records"][-1]["previous_error_category"] == "dependency"
        assert failures["records"][-1]["error_category"] == "assertion"
        sequence = json.loads(await c.host_query("sequence", options))
        assert [row["status"] for row in sequence["records"]] == ["failed"] * 3 + ["success"]
        retries = json.loads(await c.host_query("retries", options))["records"]
        assert [(row["previous"], row["call"], row["outcome"]) for row in retries] == [
            ("a", "b", "failed"), ("c", "d", "success"),
        ]
        await c.save_statistics()
        timeline = json.loads(await c.host_query("timeline", {**options, "limit": 2}))
        assert timeline["truncated"]
        assert [data["tool_call_id"] for _, _, data in timeline["records"]] == ["d", "c"]

    asyncio.run(run())
