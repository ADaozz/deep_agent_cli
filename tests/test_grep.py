"""rg discovery and the fact that grep is not a model tool."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, HumanMessage

from agent.bootstrap.factory import AgentSpec, build_agent, create_agent
from agent.execution.backend import BackendSelection, BubblewrapBackend
from agent.execution.filesystem import WorkspaceCompositeBackend
from agent.execution.policy import SandboxConfig
from agent.execution.types import ExecutionMode
from agent.runtime.control import RunController
from agent.runtime.permissions import PermissionMode
from agent.execution import ripgrep as implementation
from tests.conftest import graph_tool_names, scripted_model


def usable_bwrap() -> str:
    executable = shutil.which("bwrap")
    reason = "bubblewrap is not installed"
    if executable:
        probe = subprocess.run(
            [executable, "--unshare-all", "--ro-bind", "/", "/", "true"],
            capture_output=True, text=True,
        )
        if probe.returncode == 0:
            return executable
        reason = probe.stderr.strip()
    if os.environ.get("REQUIRE_BWRAP_TEST"):
        pytest.fail(f"bubblewrap is required: {reason}")
    pytest.skip(f"UNSUPPORTED SANDBOX ENVIRONMENT: {reason}")


@pytest.mark.parametrize("permission", [PermissionMode.ASK, PermissionMode.ALLOW])
def test_grep_and_glob_are_not_registered(tmp_path: Path, monkeypatch, permission):
    work = tmp_path / "workspace"
    skills = tmp_path / "skills"
    work.mkdir()
    skills.mkdir()
    local = BubblewrapBackend(SandboxConfig(workspace=work), executable=usable_bwrap())
    backend = WorkspaceCompositeBackend(local, skills_dir=skills)
    model = scripted_model([AIMessage(content="done")])
    schemas = []
    original = type(model).bind_tools

    def bind(self, tools, **kwargs):
        schemas.append(list(tools))
        return original(self, tools, **kwargs)

    monkeypatch.setattr(type(model), "bind_tools", bind)
    prepared = build_agent(
        AgentSpec(sandbox=local.config, skills=()), model,
        permission, None, RunController(), lambda: False,
        backend_selection=BackendSelection(backend, ExecutionMode.SANDBOXED),
    )
    prepared.graph.invoke(
        {"messages": [HumanMessage(content="search")]},
        {"configurable": {"thread_id": "grep-schema"}},
    )
    names = [tool.name for tool in schemas[-1]]
    assert "grep" not in names
    assert "glob" not in names
    assert "write_todos" not in names
    delegation = set() if permission is PermissionMode.ASK else {"start_task"}
    assert set(names) == delegation | {
        "ls", "read", "write", "edit", "delete", "execute", "write_stdin", "request_human_input",
        "compact_conversation", "read_task", "cancel_task",
    }
    from agent.tools.execute import _SEARCH_GUIDANCE, _SEARCH_UNAVAILABLE

    execute = next(tool for tool in schemas[-1] if tool.name == "execute")
    assert set(execute.args) == {"command", "timeout", "network", "yield_time", "tty", "interactive"}
    write_stdin = next(tool for tool in schemas[-1] if tool.name == "write_stdin")
    assert set(write_stdin.args) == {"execution_id", "chars", "yield_time", "action"}
    expected = _SEARCH_GUIDANCE if implementation.resolve_rg() else _SEARCH_UNAVAILABLE
    assert expected in execute.description


def test_execute_description_tracks_rg_availability(monkeypatch):
    from deepagents.backends.protocol import ExecuteResponse
    from agent.tools.execute import _SEARCH_GUIDANCE, _SEARCH_UNAVAILABLE, build_execute_tool

    class Backend:
        def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
            return ExecuteResponse(command, 0, False)

    monkeypatch.setattr(implementation, "resolve_rg", lambda: "/usr/bin/rg")
    available = build_execute_tool(Backend())  # type: ignore[arg-type]
    allowed = build_execute_tool(Backend(), permission=PermissionMode.ALLOW)  # type: ignore[arg-type]
    for tool in (available, allowed):
        assert _SEARCH_GUIDANCE in tool.description
        assert _SEARCH_UNAVAILABLE not in tool.description
        assert set(tool.args) == {"command", "timeout", "network", "yield_time", "tty", "interactive"}
    assert available.description != allowed.description
    assert "Allow mode" in allowed.description
    assert "Ask mode" in available.description

    monkeypatch.setattr(implementation, "resolve_rg", lambda: None)
    missing = build_execute_tool(Backend(), permission=PermissionMode.AUTO)  # type: ignore[arg-type]
    assert _SEARCH_UNAVAILABLE in missing.description
    assert _SEARCH_GUIDANCE not in missing.description
    assert "Auto mode" in missing.description


def test_custom_backend_does_not_register_grep_or_glob(fake_done):
    prepared = create_agent(model=fake_done, backend=StateBackend(), skills=[])
    names = graph_tool_names(prepared.graph)
    assert "grep" not in prepared.filesystem_tools
    assert "glob" not in prepared.filesystem_tools
    assert "grep" not in names
    assert "glob" not in names


def test_resolve_rg_prefers_python_scripts(tmp_path, monkeypatch):
    rg = tmp_path / "rg"
    rg.write_text("#!/bin/sh\nexit 0\n")
    rg.chmod(0o755)
    monkeypatch.setattr(implementation.sysconfig, "get_path", lambda name: str(tmp_path))

    def unexpected_path_lookup(_name):
        pytest.fail("PATH must not be consulted when the Python environment has executable rg")

    monkeypatch.setattr(implementation.shutil, "which", unexpected_path_lookup)
    assert implementation.resolve_rg() == str(rg)


@pytest.mark.parametrize("local", ["missing", "not_executable", "directory"])
def test_resolve_rg_falls_back_to_system_path(tmp_path, monkeypatch, local):
    rg = tmp_path / "rg"
    if local == "not_executable":
        rg.write_text("not executable")
        rg.chmod(0o644)
    elif local == "directory":
        rg.mkdir()
    monkeypatch.setattr(implementation.sysconfig, "get_path", lambda name: str(tmp_path))
    looked_up = []

    def system_rg(name):
        looked_up.append(name)
        return "/usr/bin/rg"

    monkeypatch.setattr(implementation.shutil, "which", system_rg)
    assert implementation.resolve_rg() == "/usr/bin/rg"
    assert looked_up == ["rg"]


def test_resolve_rg_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(implementation.sysconfig, "get_path", lambda name: str(tmp_path))
    monkeypatch.setattr(implementation.shutil, "which", lambda name: None)
    assert implementation.resolve_rg() is None
