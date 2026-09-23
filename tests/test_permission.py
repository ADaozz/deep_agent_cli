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
from agent.network import network_requested
from agent.permission import (
    ASK_INTERRUPT_ON,
    PermissionMode,
    interrupt_on_for_mode,
    parse_permission_mode,
)
from agent.runner import AgentRunner
from agent.sandbox import select_backend
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
    assert "send_email" in ask
    assert "execute" in ask
    assert ask["execute"]["when"] is ASK_INTERRUPT_ON["execute"]["when"]
    assert interrupt_on_for_mode(PermissionMode.ALLOW) == {}


def test_network_requested_truthy() -> None:
    assert network_requested({"network": True})
    assert network_requested({"network": "true"})
    assert not network_requested({"network": False})
    assert not network_requested({})


def test_runner_defaults_to_ask() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        thread_id="perm-default",
    )
    assert runner.permission_mode() is PermissionMode.ASK
    assert runner.prepared.interrupt_on == ASK_INTERRUPT_ON


def test_allow_mode_skips_tool_confirmation() -> None:
    runner = AgentRunner(
        model=scripted_model([
            AIMessage(content="", tool_calls=[{
                "id": "call-mail",
                "name": "send_email",
                "args": {"to": "a@b.com", "subject": "hi", "body": "hello"},
            }]),
            AIMessage(content="sent"),
        ]),
        backend=StateBackend(),
        thread_id="perm-allow-run",
    )
    runner.set_permission_mode(PermissionMode.ALLOW)
    assert runner.prepared.interrupt_on == {}
    result = runner.invoke("send mail")
    assert result.status == "completed"
    assert result.output == "sent"


def test_set_permission_mode_rejected_while_busy() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        thread_id="perm-busy",
    )
    runner._busy = True
    with pytest.raises(RuntimeError, match="in progress"):
        runner.set_permission_mode(PermissionMode.ALLOW)


def _unsandboxed_runner(tmp_path: Path, messages: list, *, thread_id: str) -> AgentRunner:
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


def test_ask_execute_without_network_runs(tmp_path: Path) -> None:
    runner = _unsandboxed_runner(
        tmp_path,
        [
            AIMessage(content="", tool_calls=[{
                "id": "ex1",
                "name": "execute",
                "args": {"command": "printf ok"},
            }]),
            AIMessage(content="done"),
        ],
        thread_id="exec-no-net",
    )
    result = runner.invoke("run")
    assert result.status == "completed"
    assert result.output == "done"


def test_ask_execute_with_network_interrupts_then_approve(tmp_path: Path) -> None:
    runner = _unsandboxed_runner(
        tmp_path,
        [
            AIMessage(content="", tool_calls=[{
                "id": "ex-net",
                "name": "execute",
                "args": {"command": "printf net", "network": True},
            }]),
            AIMessage(content="online"),
        ],
        thread_id="exec-net-ask",
    )
    waiting = runner.invoke("need net")
    assert waiting.status == "waiting_confirmation"
    assert waiting.pending_tool_calls[0]["name"] == "execute"
    resumed = runner.resume({"type": "approve", "toolCallId": "ex-net"})
    assert resumed.status == "completed"
    assert resumed.output == "online"


def test_allow_execute_with_network_skips_interrupt(tmp_path: Path) -> None:
    runner = _unsandboxed_runner(
        tmp_path,
        [
            AIMessage(content="", tool_calls=[{
                "id": "ex-allow",
                "name": "execute",
                "args": {"command": "printf net", "network": True},
            }]),
            AIMessage(content="online"),
        ],
        thread_id="exec-net-allow",
    )
    runner.set_permission_mode(PermissionMode.ALLOW)
    result = runner.invoke("need net")
    assert result.status == "completed"
    assert result.output == "online"


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
    runner.set_permission_mode(PermissionMode.ALLOW)

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            await app.select_permission("ask")
            assert app.runner.permission_mode() is PermissionMode.ASK
            assert any("ask" in getattr(block, "content", "") for block in app.state.blocks)

    asyncio.run(scenario())


def test_cli_permission_allow_requires_typed_confirm() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        thread_id="cli-perm-allow",
    )

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


def test_cli_permission_allow_reject_wrong_token() -> None:
    runner = AgentRunner(
        model=scripted_model([AIMessage(content="ok")]),
        backend=StateBackend(),
        thread_id="cli-perm-wrong",
    )

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
