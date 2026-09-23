"""Permission mode ask/allow on AgentRunner and CLI /permission."""
from __future__ import annotations

import asyncio
import warnings
from pathlib import Path

import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent.cli.app import CliApplication
from agent.cli.interactions import InteractionController
from agent.config import SandboxConfig
from agent.factory import create_agent
from agent.tools.examples import build_example_tools
from agent.network import network_requested
from agent.network import get_execute_network
from agent.tools.execute import build_execute_tool
from deepagents.backends.protocol import ExecuteResponse
from agent.permission import (
    ASK_INTERRUPT_ON,
    PermissionMode,
    allow_mode_available,
    interrupt_on_for_mode,
    parse_permission_mode,
)
from agent.runner import AgentRunner
from agent.sandbox import ExecutionMode, SandboxUnavailableError, select_backend
from tests.conftest import scripted_model


def test_parse_permission_aliases() -> None:
    assert parse_permission_mode("ask") is PermissionMode.ASK
    assert parse_permission_mode("all-approve") is PermissionMode.ASK
    assert parse_permission_mode("allow") is PermissionMode.ALLOW
    assert parse_permission_mode("yolo") is PermissionMode.ALLOW
    assert parse_permission_mode("nope") is None


def test_interrupt_on_for_modes() -> None:
    ask = interrupt_on_for_mode(PermissionMode.ASK)
    assert ask is not None
    assert "send_email" not in ask
    assert ask["execute"] == {"allowed_decisions": ["approve", "reject"]}
    assert "when" not in ask["execute"]
    assert interrupt_on_for_mode(PermissionMode.ALLOW) == {}


def test_allow_only_for_sandboxed() -> None:
    assert allow_mode_available(ExecutionMode.SANDBOXED)
    assert not allow_mode_available(ExecutionMode.UNSANDBOXED)
    assert not allow_mode_available(ExecutionMode.CUSTOM)


def test_network_requested_truthy() -> None:
    assert network_requested({"network": True})
    assert network_requested({"network": "true"})
    assert not network_requested({"network": False})
    assert not network_requested({})


def test_execute_network_setting_is_scoped_to_one_call() -> None:
    seen: list[bool] = []

    class Backend:
        def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
            seen.append(get_execute_network())
            return ExecuteResponse(command, 0, False)

    execute = build_execute_tool(Backend())  # type: ignore[arg-type]
    assert execute.invoke({"command": "first", "network": True}) == "first"
    assert execute.invoke({"command": "second"}) == "second"
    assert seen == [True, False]
    assert not get_execute_network()


def test_runner_defaults_to_ask() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        thread_id="perm-default",
    )
    assert runner.permission_mode() is PermissionMode.ASK
    assert runner.prepared.interrupt_on == ASK_INTERRUPT_ON
    assert runner.prepared.execution_mode is ExecutionMode.CUSTOM


def test_custom_interrupt_mapping_is_applied() -> None:
    prepared = create_agent(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{"id": "lookup-1", "name": "lookup_docs", "args": {"query": "x"}}]),
            AIMessage(content="done"),
        ]), backend=StateBackend(), extra_tools=build_example_tools(),
        interrupt_on={"lookup_docs": True},
    )
    runner = AgentRunner(prepared=prepared)
    assert prepared.interrupt_on == {**ASK_INTERRUPT_ON, "lookup_docs": True}
    waiting = runner.invoke("look up")
    assert waiting.status == "waiting_confirmation"
    assert waiting.pending_tool_calls[0]["name"] == "lookup_docs"


def test_set_permission_mode_rejected_while_busy() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        thread_id="perm-busy",
    )
    runner._busy = True
    with pytest.raises(RuntimeError, match="in progress"):
        runner.set_permission_mode(PermissionMode.ALLOW)


def _execute_messages(call_id: str, *, network: bool = False, final: str = "done") -> list[AIMessage]:
    args: dict = {"command": "printf net" if network else "printf ok"}
    if network:
        args["network"] = True
    return [
        AIMessage(content="", tool_calls=[{
            "id": call_id,
            "name": "execute",
            "args": args,
        }]),
        AIMessage(content=final),
    ]


def _custom_runner(tmp_path: Path, messages: list, *, thread_id: str) -> AgentRunner:
    config = SandboxConfig(workspace=tmp_path, bwrap_path="/missing/bwrap", allow_unsandboxed=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        selected = select_backend(config)
    return AgentRunner(
        model=scripted_model(messages),
        backend=selected.backend,
        sandbox_config=config,
        thread_id=thread_id,
    )


def _unsandboxed_runner(tmp_path: Path, messages: list, *, thread_id: str) -> AgentRunner:
    config = SandboxConfig(workspace=tmp_path, bwrap_path="/missing/bwrap", allow_unsandboxed=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        runner = AgentRunner(
            model=scripted_model(messages),
            sandbox_config=config,
            thread_id=thread_id,
        )
    assert runner.prepared.execution_mode is ExecutionMode.UNSANDBOXED
    return runner


def _sandboxed_runner(tmp_path: Path, messages: list, *, thread_id: str) -> AgentRunner:
    config = SandboxConfig(workspace=tmp_path)
    try:
        selected = select_backend(config)
    except SandboxUnavailableError:
        pytest.skip("bubblewrap sandbox unavailable")
    if selected.mode is not ExecutionMode.SANDBOXED:
        pytest.skip("bubblewrap sandbox unavailable")
    runner = AgentRunner(
        model=scripted_model(messages),
        sandbox_config=config,
        thread_id=thread_id,
    )
    assert runner.prepared.execution_mode is ExecutionMode.SANDBOXED
    return runner


def test_sandboxed_ask_execute_without_network_interrupts(tmp_path: Path) -> None:
    runner = _sandboxed_runner(tmp_path, _execute_messages("ex-sb-ask"), thread_id="sb-ask-off")
    waiting = runner.invoke("run")
    assert waiting.status == "waiting_confirmation"
    assert waiting.pending_tool_calls[0]["name"] == "execute"
    assert not network_requested(waiting.pending_tool_calls[0].get("args") or {})
    resumed = runner.resume({"type": "approve", "toolCallId": "ex-sb-ask"})
    assert resumed.status == "completed"


def test_sandboxed_ask_execute_with_network_interrupts(tmp_path: Path) -> None:
    runner = _sandboxed_runner(
        tmp_path, _execute_messages("ex-sb-net", network=True, final="online"), thread_id="sb-ask-net",
    )
    waiting = runner.invoke("need net")
    assert waiting.status == "waiting_confirmation"
    call = waiting.pending_tool_calls[0]
    assert call["name"] == "execute"
    assert network_requested(call.get("args") or {})
    ui = InteractionController.approval([call])
    assert "NETWORK" in ui.question
    resumed = runner.resume({"type": "approve", "toolCallId": "ex-sb-net"})
    assert resumed.status == "completed"
    assert resumed.output == "online"


def test_sandboxed_allow_execute_without_network_runs(tmp_path: Path) -> None:
    runner = _sandboxed_runner(tmp_path, _execute_messages("ex-sb-allow"), thread_id="sb-allow-off")
    runner.set_permission_mode(PermissionMode.ALLOW)
    result = runner.invoke("run")
    assert result.status == "completed"


def test_sandboxed_allow_execute_with_network_runs(tmp_path: Path) -> None:
    runner = _sandboxed_runner(
        tmp_path, _execute_messages("ex-sb-allow-net", network=True, final="online"), thread_id="sb-allow-net",
    )
    runner.set_permission_mode(PermissionMode.ALLOW)
    result = runner.invoke("need net")
    assert result.status == "completed"
    assert result.output == "online"


def test_unsandboxed_ask_execute_without_network_interrupts(tmp_path: Path) -> None:
    runner = _unsandboxed_runner(tmp_path, _execute_messages("ex-un-ask"), thread_id="un-ask-off")
    waiting = runner.invoke("run")
    assert waiting.status == "waiting_confirmation"
    assert waiting.pending_tool_calls[0]["name"] == "execute"
    resumed = runner.resume({"type": "approve", "toolCallId": "ex-un-ask"})
    assert resumed.status == "completed"


def test_unsandboxed_ask_execute_with_network_interrupts(tmp_path: Path) -> None:
    runner = _unsandboxed_runner(
        tmp_path, _execute_messages("ex-un-net", network=True, final="online"), thread_id="un-ask-net",
    )
    waiting = runner.invoke("need net")
    assert waiting.status == "waiting_confirmation"
    assert waiting.pending_tool_calls[0]["name"] == "execute"
    resumed = runner.resume({"type": "approve", "toolCallId": "ex-un-net"})
    assert resumed.status == "completed"
    assert resumed.output == "online"


def test_unsandboxed_rejects_allow(tmp_path: Path) -> None:
    runner = _unsandboxed_runner(tmp_path, [AIMessage(content="ok")], thread_id="un-allow")
    with pytest.raises(ValueError, match="SANDBOXED"):
        runner.set_permission_mode(PermissionMode.ALLOW)
    assert runner.permission_mode() is PermissionMode.ASK


def test_custom_ask_execute_without_network_interrupts(tmp_path: Path) -> None:
    runner = _custom_runner(tmp_path, _execute_messages("ex-cu-ask"), thread_id="cu-ask-off")
    assert runner.prepared.execution_mode is ExecutionMode.CUSTOM
    waiting = runner.invoke("run")
    assert waiting.status == "waiting_confirmation"
    assert waiting.pending_tool_calls[0]["name"] == "execute"
    resumed = runner.resume({"type": "approve", "toolCallId": "ex-cu-ask"})
    assert resumed.status == "completed"


def test_custom_rejects_allow_by_default(tmp_path: Path) -> None:
    runner = _custom_runner(tmp_path, [AIMessage(content="ok")], thread_id="cu-allow")
    assert runner.prepared.execution_mode is ExecutionMode.CUSTOM
    with pytest.raises(ValueError, match="SANDBOXED"):
        runner.set_permission_mode(PermissionMode.ALLOW)
    assert runner.permission_mode() is PermissionMode.ASK


def test_custom_backend_rejects_allow_before_graph_is_exposed() -> None:
    with pytest.raises(ValueError, match="SANDBOXED"):
        create_agent(model=scripted_model([AIMessage(content="ok")]),
                     backend=StateBackend(), interrupt_on={}, skills=[])


def test_custom_approval_cannot_disable_builtin_approval() -> None:
    with pytest.raises(ValueError, match="write_file"):
        create_agent(model=scripted_model([AIMessage(content="ok")]),
                     backend=StateBackend(), interrupt_on={"write_file": False})


def test_approval_ui_mentions_declared_network() -> None:
    interaction = InteractionController.approval([{
        "name": "execute",
        "args": {"command": "curl example.com", "network": True},
    }])
    assert "NETWORK" in interaction.question
    assert len(interaction.fields[0]["options"]) == 2
    labels = {opt["label"] for opt in interaction.fields[0]["options"]}
    assert labels == {"Reject", "Run"}
    values = {opt["value"] for opt in interaction.fields[0]["options"]}
    assert values == {"approve", "reject"}


def test_cli_permission_ask_direct() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        thread_id="cli-perm-ask",
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await app.select_permission("ask")
            assert app.runner.permission_mode() is PermissionMode.ASK
            assert any("ask" in getattr(block, "content", "") for block in app.state.blocks)

    asyncio.run(scenario())


def test_cli_permission_allow_rejected_when_not_sandboxed() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        thread_id="cli-perm-custom",
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await app.select_permission("allow")
            assert app.runner.permission_mode() is PermissionMode.ASK
            assert app.interaction is None
            assert any("SANDBOXED" in getattr(block, "content", "") for block in app.state.blocks)

    asyncio.run(scenario())


def test_cli_permission_selector_locked_when_not_sandboxed() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        thread_id="cli-perm-locked",
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await app.select_permission("")
            assert app.interaction is None
            assert any("locked to ask" in getattr(block, "content", "") for block in app.state.blocks)

    asyncio.run(scenario())


def test_cli_permission_allow_requires_typed_confirm(tmp_path: Path) -> None:
    runner = _sandboxed_runner(tmp_path, [AIMessage(content="ok")], thread_id="cli-perm-allow")

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await app.select_permission("allow")
            assert app.runner.permission_mode() is PermissionMode.ASK
            assert app.interaction is not None
            assert app.interaction.kind == "permission_confirm"
            assert "HIGH RISK" in app.interaction.question
            assert app.interaction.accept("ALLOW")
            app._finish_interaction()
            assert app.runner.permission_mode() is PermissionMode.ALLOW
            assert app.interaction is None

    asyncio.run(scenario())


def test_cli_permission_allow_reject_wrong_token(tmp_path: Path) -> None:
    runner = _sandboxed_runner(tmp_path, [AIMessage(content="ok")], thread_id="cli-perm-wrong")

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await app.select_permission("allow")
            assert app.interaction is not None
            assert app.interaction.accept("yes")
            app._finish_interaction()
            assert app.runner.permission_mode() is PermissionMode.ASK
            assert any("ALLOW not enabled" in getattr(block, "content", "") for block in app.state.blocks)

    asyncio.run(scenario())
