"""验证命令补全的连续子串匹配，以及退格后的候选刷新。"""
import asyncio

from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent.cli.app import CliApplication, SlashCompleter
from agent.cli.commands import command_table
from agent.config import ModelProfile
from agent.runner import AgentRunner
from tests.conftest import scripted_model


def candidates(completer, value):
    return [item.text for item in completer.get_completions(Document(value), None)]


def test_commands_require_contiguous_substrings_and_ignore_case():
    completer = SlashCompleter(command_table())
    assert candidates(completer, "/elp") == ["/help"]
    assert candidates(completer, "/ELP") == ["/help"]
    assert "/resume" in candidates(completer, "/sum")
    assert candidates(completer, "/hlp") == []
    assert candidates(completer, "/rsm") == []
    assert candidates(completer, "/model extra text") == []
    assert candidates(completer, "普通文本") == []
    assert candidates(completer, "/hel\n") == []
    assert len(candidates(completer, "/")) == len(command_table())
    assert candidates(completer, "/exit")[0] == "/exit"
    assert SlashCompleter._rank("clear", "cl") < SlashCompleter._rank("nuclear", "cl")


def test_arguments_use_the_same_substring_matching():
    completer = SlashCompleter(
        command_table(),
        model_profiles=lambda: [ModelProfile("source/qwen-max", "qwen-max")],
        permission_modes=lambda: ["ask", "allow"],
    )
    assert candidates(completer, "/permission low") == ["allow"]
    assert candidates(completer, "/permission lw") == []
    assert candidates(completer, "/model wen") == ["source/qwen-max"]
    assert candidates(completer, "/model qwn") == []


def test_backspace_recovers_candidates_and_clears_accepted_suppression():
    """实际按键验证无匹配、确认补全、连续退格和重新输入的完整流程。"""
    async def wait_until(predicate):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise TimeoutError("命令补全未刷新")

    async def scenario():
        runner = AgentRunner(model=scripted_model([AIMessage(content="unused")]), backend=StateBackend())
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            task = asyncio.create_task(app.run_async())

            def menu_has(value):
                state = app.buffer.complete_state
                return bool(state and any(item.text == value for item in state.completions))

            try:
                await asyncio.sleep(0.03)
                pipe.send_text("/helzzz")
                await wait_until(lambda: app.buffer.text == "/helzzz")
                assert not menu_has("/help")
                pipe.send_text("\x7f" * 3)
                await wait_until(lambda: app.buffer.text == "/hel" and menu_has("/help"))
                pipe.send_text("\r")
                await wait_until(lambda: app.buffer.text == "/help" and app.buffer.complete_state is None)
                assert app.slash_completer.accepted_text == "/help"
                pipe.send_text("\x7f")
                await wait_until(lambda: app.buffer.text == "/hel" and menu_has("/help"))
                assert app.slash_completer.accepted_text is None
                pipe.send_text("p")
                await wait_until(lambda: app.buffer.text == "/help")
                assert candidates(app.slash_completer, "/help") == ["/help"]
                pipe.send_text("\x7f" * 4)
                await wait_until(lambda: app.buffer.text == "/" and menu_has("/help"))
                pipe.send_text("\x7f")
                await wait_until(lambda: app.buffer.text == "" and app.buffer.complete_state is None)
            finally:
                app.exit()
                await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())
