"""Helpers shared by the CLI test modules."""
from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent
from prompt_toolkit.output import DummyOutput

from agent.cli.app import CliApplication
from agent.runner import AgentRunner
from tests.conftest import scripted_model


_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")


def without_diff_number(text: str) -> str:
    return re.sub(r"^\s*\d+ (?=[+-])", "", text)


def plain(rendered: str) -> str:
    """Strip SGR sequences so assertions survive Rich splitting a run mid-word."""
    return _ANSI_SGR.sub("", rendered)


async def await_cli_io(awaitable, *, timeout: float = 5.0):  # type: ignore[no-untyped-def]
    """Drive the loop while testing CLI I/O without Application.run's refresh timer."""
    task = asyncio.ensure_future(awaitable)
    deadline = asyncio.get_running_loop().time() + timeout
    while not task.done() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    if not task.done():
        task.cancel()
        raise TimeoutError("CLI I/O did not finish")
    return await task


def wheel(event_type: object) -> object:
    return MouseEvent(
        position=Point(x=0, y=0),
        event_type=event_type,
        button=MouseButton.LEFT,
        modifiers=frozenset(),
    )


def paint(app: CliApplication) -> None:
    """Render one frame. `_redraw` is a no-op until the application is running."""
    app.application.render_counter += 1
    with set_app(app.application):
        app.application.renderer.render(app.application, app.application.layout)


def escape(app: CliApplication) -> None:
    for binding in app.bindings.bindings:
        if binding.keys == ("escape",):
            class Event:
                current_buffer = app.buffer

            binding.handler(Event())
            return
    raise AssertionError("Escape binding missing")


def with_painted_app(
    thread_id: str,
    scenario: Callable[[CliApplication], None],
    *,
    lines: int = 80,
) -> None:
    """Run `scenario` against an app that painted a taller-than-viewport transcript.

    Painting needs a running loop: `Application.invalidate` schedules the redraw
    through it, and `_redraw` is a no-op until the application is running.
    """
    async def driver() -> None:
        runner = AgentRunner(
            model=scripted_model([AIMessage(content="unused")]),
            backend=StateBackend(),
            thread_id=thread_id,
        )
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            for index in range(lines):
                app.state.add_system(f"filler line {index}")
            paint(app)
            scenario(app)

    asyncio.run(driver())
