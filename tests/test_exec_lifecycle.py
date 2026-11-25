"""Yielded execute + write_stdin lifecycle (requires Bubblewrap)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import os
import time

import pytest

from agent.execution.backend import select_backend
from agent.execution.filesystem import WorkspaceCompositeBackend
from agent.execution.lifecycle import execution_registry, interact
from agent.execution.ownership import reset_execution_ownership, set_execution_ownership
from agent.execution.network import reset_execute_network, set_execute_network
from agent.execution.policy import SandboxConfig
from agent.execution.types import SandboxUnavailableError
from agent.tools.catalog import WRITE_STDIN_TOOL, node_tools


pytestmark = pytest.mark.sandbox


def _bwrap_config(workspace: Path) -> SandboxConfig:
    import shutil

    executable = shutil.which("bwrap")
    if executable is None:
        if os.environ.get("REQUIRE_BWRAP_TEST"):
            pytest.fail("bubblewrap is required in CI")
        pytest.skip("UNSUPPORTED SANDBOX ENVIRONMENT [not_installed]: bubblewrap is not installed")
    return SandboxConfig(workspace=workspace, bwrap_path=executable, timeout_seconds=30)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def backend(workspace: Path) -> Iterator[WorkspaceCompositeBackend]:
    try:
        selected = select_backend(_bwrap_config(workspace))
    except SandboxUnavailableError as exc:
        if os.environ.get("REQUIRE_BWRAP_TEST"):
            pytest.fail(str(exc))
        pytest.skip(f"UNSUPPORTED SANDBOX ENVIRONMENT [{exc.kind}]: {exc}")
    assert isinstance(selected.backend, WorkspaceCompositeBackend)
    ownership = set_execution_ownership(thread_id="test-thread", agent_scope="root")
    yield selected.backend
    execution_registry().shutdown()
    reset_execution_ownership(ownership)
    selected.backend.executor.pool.shutdown("test finished")


def test_write_stdin_is_common_tool() -> None:
    assert WRITE_STDIN_TOOL in node_tools("root")
    assert WRITE_STDIN_TOOL in node_tools("sync")


def test_sync_execute_unchanged(backend: WorkspaceCompositeBackend) -> None:
    token = set_execute_network(False)
    try:
        response = backend.execute("printf hello")
    finally:
        reset_execute_network(token)
    assert "hello" in response.output
    assert not getattr(response, "running", False)
    assert response.exit_code == 0


def test_yield_then_poll_and_cancel(backend: WorkspaceCompositeBackend) -> None:
    token = set_execute_network(False)
    try:
        first = backend.execute("sleep 5; printf done", yield_time=0.2)
    finally:
        reset_execute_network(token)
    assert first.running
    assert first.execution_id
    live = execution_registry().require(first.execution_id)
    polled = interact(live, chars="", yield_time=0.2, action="write")
    assert polled.running
    cancelled = interact(live, chars="", yield_time=2.0, action="cancel")
    assert not cancelled.running
    assert cancelled.exit_code in {130, 137, 143} or cancelled.termination_reason == "cancelled"


def test_interactive_pipe_write_stdin(backend: WorkspaceCompositeBackend) -> None:
    token = set_execute_network(False)
    try:
        first = backend.execute(
            "python3 -c 'import sys; print(sys.stdin.readline().strip())'",
            yield_time=0.3,
            stdin_mode="pipe",
            timeout=10,
        )
    finally:
        reset_execute_network(token)
    assert first.running
    assert first.execution_id
    live = execution_registry().require(first.execution_id)
    time.sleep(0.2)
    done = interact(live, chars="piped-line\n", yield_time=3.0, action="write")
    assert not done.running
    assert "piped-line" in done.output
