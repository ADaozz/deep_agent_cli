"""Real subprocess tests for both engines, registration, and workspace boundaries."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, HumanMessage

from agent.cancel import ToolCancelContext, clear_cancel_context, set_cancel_context
from agent.config import SandboxConfig
from agent.factory import AgentSpec, build_agent, create_agent
from agent.control import RunController
from agent.permission import PermissionMode
from agent.sandbox import BackendSelection, ExecutionMode, BubblewrapBackend, UnsandboxedShellBackend, WorkspaceCompositeBackend
from agent.tools import grep as implementation
from tests.conftest import graph_tool_names, scripted_model


@pytest.fixture(params=["rg", "python"])
def engine(request, monkeypatch):
    executable = shutil.which("rg")
    if request.param == "rg":
        assert executable, "Install ripgrep to validate both engines"
    monkeypatch.setattr(implementation.shutil, "which", lambda _: executable if request.param == "rg" else None)
    return request.param


@pytest.fixture
def workspace(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_text("def run():\nfoo foo\nconfig.yaml\n中文 café\n", encoding="utf-8")
    (work / "b.py").write_text("def execute():\nbar\nconfigXyaml\n", encoding="utf-8")
    (work / "a.txt").write_text("foo\n")
    (work / "src").mkdir()
    (work / "src" / "nested.py").write_text("foo\n")
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "SKILL.md").write_text("foo skill\n")
    backend = WorkspaceCompositeBackend(UnsandboxedShellBackend(SandboxConfig(workspace=work)), skills_dir=skills)
    return backend, work, skills


def invoke(workspace, **kwargs):
    return implementation.build_regex_grep_tool(workspace[0]).invoke(kwargs)


@pytest.mark.parametrize("pattern,expected", [
    ("foo", ["/workspace/a.py", "/workspace/a.txt", "/workspace/src/nested.py"]),
    ("foo|bar", ["/workspace/a.py", "/workspace/b.py"]),
    (r"def \w+\(", ["/workspace/a.py", "/workspace/b.py"]),
    ("中文|café", ["/workspace/a.py"]),
])
def test_common_searches(engine, workspace, pattern, expected):
    result = invoke(workspace, pattern=pattern)
    for file in expected:
        assert file in result
    assert "Error:" not in result


def test_default_regex_and_literal_mode(engine, workspace):
    assert "/workspace/b.py" in invoke(workspace, pattern="config.yaml")
    assert invoke(workspace, pattern="config.yaml", fixed_string=True) == "/workspace/a.py"
    assert invoke(workspace, pattern="foo|bar", fixed_string=True) == "No matches found"


def test_modes_count_matching_lines(engine, workspace):
    assert invoke(workspace, pattern="foo", path="/workspace/a.py") == "/workspace/a.py"
    assert invoke(workspace, pattern="foo", path="/workspace/a.py", output_mode="content") == "/workspace/a.py:\n  2: foo foo"
    assert invoke(workspace, pattern="foo", path="/workspace/a.py", output_mode="count") == "/workspace/a.py: 1"


@pytest.mark.parametrize("glob,expected", [
    ("*.py", {"/workspace/a.py", "/workspace/src/nested.py"}),
    ("/*.py", {"/workspace/a.py"}),
    ("src/**/*.py", {"/workspace/src/nested.py"}),
    ("*.{py,txt}", {"/workspace/a.py", "/workspace/a.txt", "/workspace/src/nested.py"}),
])
def test_glob(engine, workspace, glob, expected):
    assert set(invoke(workspace, pattern="foo", glob=glob).splitlines()) == expected


def test_limit_no_match_and_invalid_regex(engine, workspace):
    (workspace[1] / "many.py").write_text("hit\n" * 100)
    result = invoke(workspace, pattern="hit", max_count=5, output_mode="content")
    assert result.count("hit") == 5
    assert implementation.TRUNCATED in result
    assert invoke(workspace, pattern="unmatched-string") == "No matches found"
    assert "Error:" in invoke(workspace, pattern="[")
    empty = workspace[1] / "empty"
    empty.mkdir()
    assert "Error:" in invoke(workspace, pattern="[", path="/workspace/empty")
    assert "max_count must be positive" in invoke(workspace, pattern="foo", max_count=0)


@pytest.mark.parametrize("path", ["/etc", "/home/agent/.ssh", "../../", "/workspace/../etc", "/workspace2"])
def test_forbidden_paths(engine, workspace, path):
    assert "Error:" in invoke(workspace, pattern="foo", path=path)


def test_skills_readonly_and_symlinks(engine, workspace, tmp_path):
    assert invoke(workspace, pattern="foo", path="/skills") == "/skills/SKILL.md"
    assert workspace[2].joinpath("SKILL.md").read_text() == "foo skill\n"
    secret = tmp_path / "secret.txt"
    secret.write_text("secret-only")
    (workspace[1] / "escape").symlink_to(secret)
    (workspace[1] / "escape-dir").symlink_to(tmp_path, target_is_directory=True)
    assert invoke(workspace, pattern="secret-only") == "No matches found"
    assert "Error:" in invoke(workspace, pattern="secret", path="/workspace/escape")
    assert "Error:" in invoke(workspace, pattern="secret", path="/workspace/escape-dir/secret.txt")
    no_skills = (WorkspaceCompositeBackend(workspace[0].executor, skills_dir=None), workspace[1], None)
    assert "Error:" in invoke(no_skills, pattern="foo", path="/skills")


def test_shell_characters_are_data(engine, workspace):
    marker = workspace[1] / "INJECTED"
    pattern = f"$(touch {marker}); `touch {marker}`"
    (workspace[1] / "literal.txt").write_text(pattern)
    assert invoke(workspace, pattern=pattern, fixed_string=True) == "/workspace/literal.txt"
    assert not marker.exists()


def test_output_is_bounded(engine, workspace):
    (workspace[1] / "huge.py").write_text(("match" + "x" * 5000 + "\n") * 100)
    result = invoke(workspace, pattern="match", output_mode="content", max_count=100000)
    assert len(result.encode()) <= implementation.MAX_OUTPUT_BYTES
    assert "truncated" in result
    (workspace[1] / "large.py").write_bytes(b"a" * (10 * 1024 * 1024 + 1))
    assert "files larger" in invoke(workspace, pattern="nomatch")


def test_engine_common_regex_parity(workspace, monkeypatch):
    rg = shutil.which("rg")
    assert rg
    for pattern in ["foo|bar", r"def \w+\(", "中文|café", "config.yaml"]:
        monkeypatch.setattr(implementation.shutil, "which", lambda _: rg)
        accelerated = invoke(workspace, pattern=pattern, output_mode="content")
        monkeypatch.setattr(implementation.shutil, "which", lambda _: None)
        assert invoke(workspace, pattern=pattern, output_mode="content") == accelerated


def test_rg_failure_is_not_no_matches(workspace, monkeypatch, tmp_path):
    failing = tmp_path / "rg"
    failing.write_text("#!/bin/sh\nprintf 'deliberate rg failure\\n'\nexit 2\n")
    failing.chmod(0o755)
    monkeypatch.setattr(implementation.shutil, "which", lambda _: str(failing))
    assert "ripgrep execution" in invoke(workspace, pattern="foo")


def _record_processes(monkeypatch):
    processes = []
    original = subprocess.Popen
    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(implementation.subprocess, "Popen", record)
    return processes


def test_python_backtracking_timeout_and_cleanup(workspace, monkeypatch):
    monkeypatch.setattr(implementation.shutil, "which", lambda _: None)
    monkeypatch.setattr(implementation, "SEARCH_TIMEOUT", 2.0)
    (workspace[1] / "bad.py").write_text("a" * 100000 + "!")
    processes = _record_processes(monkeypatch)
    start = time.monotonic()
    assert "timed out" in invoke(workspace, pattern="(a+)+$", path="/workspace/bad.py")
    assert time.monotonic() - start < 4
    assert all(p.poll() is not None for p in processes)


def test_cancel_cleans_worker_and_rg_child(workspace, monkeypatch, tmp_path):
    child_pid = tmp_path / "pid"
    slow = tmp_path / "rg"
    slow.write_text(f"#!/bin/sh\necho $$ > '{child_pid}'\nexec sleep 60\n")
    slow.chmod(0o755)
    monkeypatch.setattr(implementation.shutil, "which", lambda _: str(slow))
    processes = _record_processes(monkeypatch)
    context = ToolCancelContext("grep", "search", threading.Event())
    result = []
    def run():
        set_cancel_context(context)
        try:
            result.append(invoke(workspace, pattern="foo"))
        finally:
            clear_cancel_context()
    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 5
    while not child_pid.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert child_pid.exists()
    context.request_cancel()
    thread.join(3)
    assert not thread.is_alive()
    assert "cancelled" in result[0]
    assert all(p.poll() is not None for p in processes)
    child = Path(f"/proc/{child_pid.read_text().strip()}/stat")
    assert not child.exists(), "Search child must be reaped before the tool returns"


@pytest.mark.parametrize("permission", [PermissionMode.ASK, PermissionMode.ALLOW])
def test_model_schema_and_readonly_call(workspace, monkeypatch, permission):
    local = BubblewrapBackend(SandboxConfig(workspace=workspace[1]), executable="/bin/true")
    backend = WorkspaceCompositeBackend(local, skills_dir=workspace[2])
    model = scripted_model([
        AIMessage(content="", tool_calls=[{"id": "g1", "name": "grep", "args": {"pattern": "foo|bar", "glob": "*.py"}}]),
        AIMessage(content="done"),
    ])
    schemas = []
    original = type(model).bind_tools
    def bind(self, tools, **kwargs):
        schemas.append(list(tools))
        return original(self, tools, **kwargs)
    monkeypatch.setattr(type(model), "bind_tools", bind)
    prepared = build_agent(AgentSpec(sandbox=local.config, skills=()), model,
                           permission, None, RunController(), lambda: False,
                           backend_selection=BackendSelection(backend, ExecutionMode.SANDBOXED))
    result = prepared.graph.invoke({"messages": [HumanMessage(content="search")]},
                                   {"configurable": {"thread_id": "grep-schema"}})
    names = [tool.name for tool in schemas[-1]]
    assert names.count("grep") == 1
    assert "write_todos" not in names
    grep = next(tool for tool in schemas[-1] if tool.name == "grep")
    assert grep.args_schema.model_json_schema()["properties"]["fixed_string"]["default"] is False
    assert any(getattr(message, "name", None) == "grep" and "/workspace/b.py" in message.content
               for message in result["messages"])
    assert "__interrupt__" not in result
    assert set(names) == {"ls", "read_file", "glob", "grep", "write_file", "edit_file", "delete",
                          "execute", "request_human_input", "compact_conversation"}


def test_custom_backend_uses_its_own_grep(fake_done):
    prepared = create_agent(model=fake_done, backend=StateBackend(), skills=[])
    assert "grep" in prepared.filesystem_tools
    assert "grep" not in prepared.exposed_tool_names
    assert "grep" in graph_tool_names(prepared.graph)


def test_rg_timeout_reaps_child(workspace, monkeypatch, tmp_path):
    child_pid = tmp_path / "rg-pid"
    slow = tmp_path / "slow-rg"
    slow.write_text(f"#!/bin/sh\necho $$ > '{child_pid}'\nexec sleep 60\n")
    slow.chmod(0o755)
    monkeypatch.setattr(implementation.shutil, "which", lambda _: str(slow))
    monkeypatch.setattr(implementation, "SEARCH_TIMEOUT", 2.0)
    processes = _record_processes(monkeypatch)
    assert "timed out" in invoke(workspace, pattern="foo")
    assert all(p.poll() is not None for p in processes)
    assert child_pid.exists()
    assert not Path(f"/proc/{child_pid.read_text().strip()}").exists()


def test_python_unsupported_syntax_and_literal_escape(workspace, monkeypatch):
    monkeypatch.setattr(implementation.shutil, "which", lambda _: None)
    assert "Invalid Python regular expression" in invoke(workspace, pattern=r"\p{L}")
    assert invoke(workspace, pattern="[", fixed_string=True) == "No matches found"


def test_duplicate_grep_is_rejected(workspace, fake_done):
    with pytest.raises(ValueError, match="extra grep"):
        create_agent(model=fake_done, backend=workspace[0], skills=[],
                     extra_tools=[implementation.build_regex_grep_tool(workspace[0])])
