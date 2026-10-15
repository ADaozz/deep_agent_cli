"""Real subprocess tests for both engines, registration, and workspace boundaries.

The ``rg`` engine runs inside the real read-only Bubblewrap search sandbox; the
``python`` engine is the descriptor-confined worker used for UNSANDBOXED.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.cancel import ToolCancelContext, clear_cancel_context, set_cancel_context
from agent.config import SandboxConfig
from agent.factory import AgentSpec, build_agent, create_agent
from agent.control import RunController
from agent.permission import PermissionMode
from agent.sandbox import BackendSelection, ExecutionMode, BubblewrapBackend, UnsandboxedShellBackend, WorkspaceCompositeBackend
from agent.tools import grep as implementation
from tests.conftest import graph_tool_names, scripted_model


def usable_bwrap() -> str:
    executable = shutil.which("bwrap")
    reason = "bubblewrap is not installed"
    if executable:
        probe = subprocess.run([executable, "--unshare-all", "--ro-bind", "/", "/", "true"],
                               capture_output=True, text=True)
        if probe.returncode == 0:
            return executable
        reason = probe.stderr.strip()
    if os.environ.get("REQUIRE_BWRAP_TEST"):
        pytest.fail(f"bubblewrap is required to test the rg search sandbox: {reason}")
    pytest.skip(f"UNSUPPORTED SANDBOX ENVIRONMENT: {reason}")


def make_backend(engine: str, work: Path, skills: Path | None) -> WorkspaceCompositeBackend:
    if engine == "rg":
        executor = BubblewrapBackend(SandboxConfig(workspace=work), executable=usable_bwrap())
    else:
        executor = UnsandboxedShellBackend(SandboxConfig(workspace=work))
    return WorkspaceCompositeBackend(executor, skills_dir=skills)


@pytest.fixture(params=["rg", "python"])
def engine(request):
    if request.param == "rg":
        assert implementation.resolve_rg(), "Install ripgrep to validate both engines"
        usable_bwrap()
    return request.param


@pytest.fixture
def workspace(request, tmp_path):
    engine = request.getfixturevalue("engine") if "engine" in request.fixturenames else "python"
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
    return make_backend(engine, work, skills), work, skills


def invoke(workspace, **kwargs):
    return implementation.build_regex_grep_tool(workspace[0]).invoke(kwargs)


def lines(result: str) -> set[str]:
    return set(result.splitlines())


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


def test_glob_is_relative_to_search_path(engine, workspace):
    assert invoke(workspace, pattern="foo", path="/workspace/src", glob="/*.py") == "/workspace/src/nested.py"


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
    (workspace[1] / "inner-link.py").symlink_to(workspace[1] / "a.py")
    assert invoke(workspace, pattern="secret-only") == "No matches found"
    assert "/workspace/inner-link.py" not in invoke(workspace, pattern="foo")
    assert "Error:" in invoke(workspace, pattern="secret", path="/workspace/escape")
    assert "Error:" in invoke(workspace, pattern="secret", path="/workspace/escape-dir/secret.txt")
    assert "Error:" in invoke(workspace, pattern="secret", path="/workspace/escape-dir")
    no_skills = (WorkspaceCompositeBackend(workspace[0].executor, skills_dir=None), workspace[1], None)
    assert "Error:" in invoke(no_skills, pattern="foo", path="/skills")


def test_shell_characters_are_data(engine, workspace):
    marker = workspace[1] / "INJECTED"
    pattern = f"$(touch {marker}); `touch {marker}`"
    (workspace[1] / "literal.txt").write_text(pattern)
    assert invoke(workspace, pattern=pattern, fixed_string=True) == "/workspace/literal.txt"
    assert invoke(workspace, pattern="x", glob=f"$(touch {marker})*") == "No matches found"
    assert not marker.exists()


def test_output_is_bounded(engine, workspace):
    (workspace[1] / "huge.py").write_text(("match" + "x" * 5000 + "\n") * 100)
    result = invoke(workspace, pattern="match", output_mode="content", max_count=100000)
    assert len(result.encode()) <= implementation.MAX_OUTPUT_BYTES
    assert "truncated" in result
    (workspace[1] / "large.py").write_bytes(b"a" * (10 * 1024 * 1024 + 1))
    # Explicit files report the skip for both engines; rg's walker skips silently.
    assert "files larger" in invoke(workspace, pattern="a", path="/workspace/large.py")
    assert "/workspace/large.py" not in invoke(workspace, pattern="^a+$")
    if engine == "python":
        assert "files larger" in invoke(workspace, pattern="nomatch")


def test_binary_files_are_skipped(engine, workspace):
    (workspace[1] / "blob.bin").write_bytes(b"foo\x00binary\n")
    assert "/workspace/blob.bin" not in invoke(workspace, pattern="foo")
    assert invoke(workspace, pattern="foo", path="/workspace/blob.bin") == "No matches found"


def test_engine_common_regex_parity(tmp_path):
    usable_bwrap()
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_text("def run():\nfoo foo\nconfig.yaml\n中文 café\n", encoding="utf-8")
    (work / "b.py").write_text("def execute():\nbar\nconfigXyaml\n", encoding="utf-8")
    accelerated = make_backend("rg", work, None)
    fallback = make_backend("python", work, None)
    for pattern in ["foo|bar", r"def \w+\(", "中文|café", "config.yaml"]:
        for mode in ("content", "count", "files_with_matches"):
            expected = implementation.search(accelerated, pattern, None, None, mode, None, False)
            assert implementation.search(fallback, pattern, None, None, mode, None, False) == expected


def test_rg_failure_is_not_no_matches(workspace, monkeypatch):
    rg_workspace = (make_backend("rg", workspace[1], None), workspace[1], None)
    result = invoke(rg_workspace, pattern="[")
    assert "ripgrep execution" in result and "No matches" not in result


def test_rg_non_elf_executable_is_reported(workspace, monkeypatch, tmp_path):
    rg_workspace = (make_backend("rg", workspace[1], None), workspace[1], None)
    script = tmp_path / "rg"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setattr(implementation, "resolve_rg", lambda: str(script))
    result = invoke(rg_workspace, pattern="foo")
    assert result.startswith("Error:") and "ELF" in result
    assert str(tmp_path) not in result


def test_broken_sandbox_is_not_reported_as_no_matches(workspace):
    broken = (WorkspaceCompositeBackend(
        BubblewrapBackend(SandboxConfig(workspace=workspace[1]), executable="/bin/true"), skills_dir=None,
    ), workspace[1], None)
    assert invoke(broken, pattern="foo").startswith("Error: ripgrep could not run")


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
    monkeypatch.setattr(implementation, "SEARCH_TIMEOUT", 2.0)
    (workspace[1] / "bad.py").write_text("a" * 100000 + "!")
    processes = _record_processes(monkeypatch)
    start = time.monotonic()
    assert "timed out" in invoke(workspace, pattern="(a+)+$", path="/workspace/bad.py")
    assert time.monotonic() - start < 4
    assert processes and all(p.poll() is not None for p in processes)


def test_python_timeout_keeps_partial_results_marked(workspace, monkeypatch):
    monkeypatch.setattr(implementation, "SEARCH_TIMEOUT", 2.0)
    (workspace[1] / "slow").mkdir()
    (workspace[1] / "slow" / "a_ok.py").write_text("aaa\n")
    (workspace[1] / "slow" / "z_bad.py").write_text("a" * 100000 + "!")
    result = invoke(workspace, pattern="^(a+)+$", path="/workspace/slow")
    if result.startswith("Error:"):
        assert result == implementation.TIMED_OUT  # scandir order put the slow file first
    else:
        assert result.splitlines() == ["/workspace/slow/a_ok.py", implementation.PARTIAL_TIMEOUT]


def _search_processes(marker: str) -> list[tuple[int, str]]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if marker.encode() in argv:
            found.append((int(entry.name), os.path.basename(argv[0].decode())))
    return found


def _hanging_rg(monkeypatch, work: Path, marker: str) -> None:
    """Make the real sandboxed rg block on a FIFO after the requested file.

    ``-j 1`` keeps rg on one thread, so the file is printed before the FIFO open.
    """
    del marker
    os.mkfifo(work / "hang.fifo")
    original = implementation._stream

    def stream(argv, consume, **kwargs):
        # The first "--" belongs to bwrap; rg's own "--" separates pattern and paths.
        separator = argv.index("--", argv.index("--") + 1)
        return original([*argv[:separator], "-j", "1", *argv[separator:], "hang.fifo"], consume, **kwargs)

    monkeypatch.setattr(implementation, "_stream", stream)


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_cancel_reaps_bwrap_and_rg(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_text("cancel-marker-1\n")
    backend = make_backend("rg", work, None)
    marker = "cancel-marker-1"
    _hanging_rg(monkeypatch, work, marker)
    processes = _record_processes(monkeypatch)
    context = ToolCancelContext("grep", "search", threading.Event())
    result = []
    def run():
        set_cancel_context(context)
        try:
            result.append(implementation.search(backend, marker, None, None, "files_with_matches", None, False))
        finally:
            clear_cancel_context()
    thread = threading.Thread(target=run)
    thread.start()
    assert _wait_for(lambda: any(name == "rg" for _, name in _search_processes(marker)))
    assert any(name == "bwrap" for _, name in _search_processes(marker))
    context.request_cancel()
    thread.join(3)
    assert not thread.is_alive()
    assert result == ["Error: Search cancelled."]
    searches = [process for process in processes if "--help" not in process.args]
    assert len(searches) == 1 and searches[0].poll() is not None
    assert _wait_for(lambda: not _search_processes(marker), 2), _search_processes(marker)
    monkeypatch.undo()
    assert implementation.search(backend, marker, None, None, "files_with_matches", None, False) == "/workspace/a.py"


@pytest.mark.parametrize("mode,line", [
    ("count", "/workspace/a.py: 1"),
    ("content", "/workspace/a.py:\n  1: timeout-marker-1"),
    # --null file lists are block-buffered, so rg does not flush the finished path.
    ("files_with_matches", None),
])
def test_rg_timeout_reaps_sandbox_and_keeps_partial_results(tmp_path, monkeypatch, mode, line):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_text("timeout-marker-1\n")
    backend = make_backend("rg", work, None)
    marker = "timeout-marker-1"
    _hanging_rg(monkeypatch, work, marker)
    monkeypatch.setattr(implementation, "SEARCH_TIMEOUT", 1.5)
    processes = _record_processes(monkeypatch)
    start = time.monotonic()
    # An explicit file keeps rg sequential: a.py is printed before it blocks on the FIFO.
    result = implementation.search(backend, marker, "/workspace/a.py", None, mode, None, False)
    assert time.monotonic() - start < 3
    if line is None:
        assert result == implementation.TIMED_OUT
    else:
        assert result == f"{line}\n{implementation.PARTIAL_TIMEOUT}"
    assert all(p.poll() is not None for p in processes)
    assert _wait_for(lambda: not _search_processes(marker), 2), _search_processes(marker)


def test_python_cancel_during_backtracking(workspace):
    (workspace[1] / "bad.py").write_text("a" * 100000 + "!")
    context = ToolCancelContext("grep", "search", threading.Event())
    result = []
    def run():
        set_cancel_context(context)
        try:
            result.append(invoke(workspace, pattern="(a+)+$", path="/workspace/bad.py"))
        finally:
            clear_cancel_context()
    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.5)
    context.request_cancel()
    thread.join(3)
    assert not thread.is_alive()
    assert result == ["Error: Search cancelled."]
    assert invoke(workspace, pattern="foo", path="/workspace/a.py") == "/workspace/a.py"


def test_rg_engine_starts_exactly_one_process(tmp_path, monkeypatch):
    work = tmp_path / "work"
    for index in range(300):
        directory = work / f"d{index % 7}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"f{index}.py").write_text("needle\n" if index % 3 == 0 else "hay\n")
    backend = make_backend("rg", work, None)
    processes = _record_processes(monkeypatch)
    for mode in ("files_with_matches", "count", "content"):
        processes.clear()
        result = implementation.search(backend, "needle", None, None, mode, None, False)
        assert "Error" not in result and "truncated" not in result
        assert len(processes) == 1
        argv = processes[0].args
        assert argv[0] == backend.executor.executable
        rg_argv = argv[argv.index("--") + 1:]
        assert rg_argv[0].endswith("/rg") and rg_argv[-3:] == ["--", "needle", "."]
        assert not any("/proc/self/fd/" in arg for arg in rg_argv)
        for absent in ("--no-ignore", "--hidden", "--follow", "--text", "--no-ignore-dot", "--no-ignore-vcs"):
            assert absent not in rg_argv
        assert "--no-config" in rg_argv and "--no-ignore-global" in rg_argv and "!.git" in rg_argv
    assert len(implementation.search(backend, "needle", None, None, "files_with_matches", None, False).splitlines()) == 100


@pytest.mark.parametrize("permission", [PermissionMode.ASK, PermissionMode.ALLOW])
def test_model_schema_and_readonly_call(workspace, monkeypatch, permission):
    local = BubblewrapBackend(SandboxConfig(workspace=workspace[1]), executable=usable_bwrap())
    backend = WorkspaceCompositeBackend(local, skills_dir=workspace[2])
    model = scripted_model([
        AIMessage(content="", tool_calls=[{"id": "g1", "name": "grep", "args": {"pattern": "foo|bar", "glob": "*.py"}}]),
        AIMessage(content="", tool_calls=[{"id": "g2", "name": "grep", "args": {
            "pattern": "foo", "glob": ["*.py", "!src/**"], "output_mode": "count"}}]),
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
    properties = grep.args_schema.model_json_schema()["properties"]
    assert properties["fixed_string"]["default"] is False
    assert {"type": "array", "items": {"type": "string"}} in properties["glob"]["anyOf"]
    outputs = {m.tool_call_id: m.content for m in result["messages"] if isinstance(m, ToolMessage)}
    assert "/workspace/b.py" in outputs["g1"]
    assert outputs["g2"] == "/workspace/a.py: 1"
    assert "__interrupt__" not in result
    assert set(names) == {"ls", "read_file", "glob", "grep", "write_file", "edit_file", "delete",
                          "execute", "request_human_input", "compact_conversation"}


def test_timeout_does_not_break_agent_session(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_text("session-marker\n")
    local = BubblewrapBackend(SandboxConfig(workspace=work), executable=usable_bwrap())
    backend = WorkspaceCompositeBackend(local, skills_dir=None)
    os.mkfifo(work / "hang.fifo")
    monkeypatch.setattr(implementation, "SEARCH_TIMEOUT", 1.0)
    original = implementation._rg_arguments
    calls = []
    def arguments(*args):
        calls.append(args)
        hang = ["-e", "absent", "hang.fifo"] if len(calls) == 1 else []
        return [*original(*args), *hang]
    monkeypatch.setattr(implementation, "_rg_arguments", arguments)
    model = scripted_model([
        AIMessage(content="", tool_calls=[{"id": "slow", "name": "grep", "args": {"pattern": "absent"}}]),
        AIMessage(content="", tool_calls=[{"id": "fast", "name": "grep", "args": {"pattern": "session-marker"}}]),
        AIMessage(content="done"),
    ])
    prepared = build_agent(AgentSpec(sandbox=local.config, skills=()), model,
                           PermissionMode.ALLOW, None, RunController(), lambda: False,
                           backend_selection=BackendSelection(backend, ExecutionMode.SANDBOXED))
    result = prepared.graph.invoke({"messages": [HumanMessage(content="search")]},
                                   {"configurable": {"thread_id": "grep-timeout"}})
    outputs = {m.tool_call_id: m.content for m in result["messages"] if isinstance(m, ToolMessage)}
    assert "timed out" in outputs["slow"]
    assert outputs["fast"] == "/workspace/a.py"
    assert result["messages"][-1].content == "done"


def test_custom_backend_uses_its_own_grep(fake_done):
    prepared = create_agent(model=fake_done, backend=StateBackend(), skills=[])
    assert "grep" in prepared.filesystem_tools
    assert "grep" not in prepared.exposed_tool_names
    assert "grep" in graph_tool_names(prepared.graph)


def test_python_unsupported_syntax_and_literal_escape(workspace):
    assert "Invalid Python regular expression" in invoke(workspace, pattern=r"\p{L}")
    assert invoke(workspace, pattern="[", fixed_string=True) == "No matches found"


def test_sandboxed_backend_without_rg_uses_python_engine(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_text("foo\n")
    backend = WorkspaceCompositeBackend(BubblewrapBackend(SandboxConfig(workspace=work), executable="/bin/true"),
                                        skills_dir=None)
    monkeypatch.setattr(implementation, "resolve_rg", lambda: None)
    processes = _record_processes(monkeypatch)
    assert implementation.search(backend, "foo", None, None, "files_with_matches", None, False) == "/workspace/a.py"
    assert processes[0].args[0] != "/bin/true"


def test_duplicate_grep_is_rejected(workspace, fake_done):
    with pytest.raises(ValueError, match="extra grep"):
        create_agent(model=fake_done, backend=workspace[0], skills=[],
                     extra_tools=[implementation.build_regex_grep_tool(workspace[0])])


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
    monkeypatch.setattr(implementation.shutil, "which", lambda _: None)
    assert implementation.resolve_rg() is None


def _write(root: Path, relative: str, text: str = "needle\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _filter_tree(root: Path) -> None:
    for relative in (
        "a.py", "a.txt", "keep.py", "src/b.py", "src/b.test.py", "tests/t.py", "tests/t.txt",
        "node_modules/pkg/a.js", "repo/a.py", "sub/repo/a.py", "sub/deep/repo/a.py", ".hidden.py",
        ".git/HEAD", ".git/config", "sub/.git/HEAD",
    ):
        _write(root, relative)


def _paths(root: Path, backend, **kwargs) -> set[str]:
    result = implementation.search(backend, "needle", kwargs.get("path"), kwargs.get("glob"),
                                   "files_with_matches", None, False)
    assert not result.startswith("Error:"), result
    return set() if result == "No matches found" else lines(result)


@pytest.mark.parametrize("glob,expected", [
    ("*.py", {"/workspace/a.py", "/workspace/keep.py", "/workspace/src/b.py", "/workspace/src/b.test.py",
              "/workspace/tests/t.py", "/workspace/repo/a.py", "/workspace/sub/repo/a.py",
              "/workspace/sub/deep/repo/a.py", "/workspace/.hidden.py"}),
    ("!*.py", {"/workspace/a.txt", "/workspace/tests/t.txt", "/workspace/node_modules/pkg/a.js"}),
    ("!**/.git/**", None),  # negation must not suppress ordinary files; .git stays excluded
    ("!*/.git/*", None),
    (["*.py", "!**/tests/**"], {"/workspace/a.py", "/workspace/keep.py", "/workspace/src/b.py",
                                "/workspace/src/b.test.py", "/workspace/repo/a.py", "/workspace/sub/repo/a.py",
                                "/workspace/sub/deep/repo/a.py", "/workspace/.hidden.py"}),
    (["!**/tests/**", "*.py"], {"/workspace/a.py", "/workspace/keep.py", "/workspace/src/b.py",
                                "/workspace/src/b.test.py", "/workspace/tests/t.py", "/workspace/repo/a.py",
                                "/workspace/sub/repo/a.py", "/workspace/sub/deep/repo/a.py", "/workspace/.hidden.py"}),
    (["*.py", "!*.test.py"], {"/workspace/a.py", "/workspace/keep.py", "/workspace/src/b.py",
                              "/workspace/tests/t.py", "/workspace/repo/a.py", "/workspace/sub/repo/a.py",
                              "/workspace/sub/deep/repo/a.py", "/workspace/.hidden.py"}),
    (["!**/node_modules/**"], None),
    ("!*/repo/*", None),
    ("!**/repo/**", None),
    ("!repo/", None),
    ("!/repo/", None),
])
def test_negated_and_combined_globs(engine, tmp_path, glob, expected):
    work = tmp_path / "work"
    _filter_tree(work)
    backend = make_backend(engine, work, None)
    found = _paths(work, backend, glob=glob)
    assert not any("/.git/" in path for path in found)
    if glob in ("!**/.git/**", "!*/.git/*"):
        assert "/workspace/a.py" in found and "/workspace/src/b.py" in found
        return
    if glob == ["!**/node_modules/**"]:
        assert "/workspace/node_modules/pkg/a.js" not in found and "/workspace/a.txt" in found
        return
    if glob == "!*/repo/*":
        assert "/workspace/sub/repo/a.py" not in found
        assert "/workspace/repo/a.py" in found and "/workspace/sub/deep/repo/a.py" in found
        return
    if glob == "!**/repo/**":
        assert "/workspace/repo/a.py" not in found
        assert "/workspace/sub/repo/a.py" not in found and "/workspace/sub/deep/repo/a.py" not in found
        assert "/workspace/keep.py" in found
        return
    if glob == "!repo/":
        assert "/workspace/keep.py" in found
        assert "/workspace/repo/a.py" not in found and "/workspace/sub/repo/a.py" not in found
        return
    if glob == "!/repo/":
        assert "/workspace/repo/a.py" not in found and "/workspace/keep.py" in found
        # A leading slash anchors the rule to the search root.
        assert "/workspace/sub/repo/a.py" in found
        return
    assert found == expected


@pytest.mark.parametrize("path,glob,expected", [
    ("/workspace/src/b.py", "*.py", "/workspace/src/b.py"),
    ("/workspace/src/b.py", "*.txt", "No matches found"),
    ("/workspace/src/b.py", "!*.py", "No matches found"),
    ("/workspace/src/b.test.py", ["*.py", "!*.test.py"], "No matches found"),
    ("/workspace/src/b.test.py", ["!*.test.py", "*.py"], "/workspace/src/b.test.py"),
    ("/workspace/a.txt", "!*.py", "/workspace/a.txt"),
    ("/workspace/src", "*.py", "/workspace/src/b.py\n/workspace/src/b.test.py"),
    ("/workspace/src", ["*.py", "!*.test.py"], "/workspace/src/b.py"),
    ("/workspace/src", "/*.py", "/workspace/src/b.py\n/workspace/src/b.test.py"),
])
def test_globs_apply_to_files_and_directories(engine, tmp_path, path, glob, expected):
    work = tmp_path / "work"
    _filter_tree(work)
    backend = make_backend(engine, work, None)
    assert implementation.search(backend, "needle", path, glob, "files_with_matches", None, False) == expected


def test_glob_rules_match_across_engines(tmp_path):
    usable_bwrap()
    work = tmp_path / "work"
    _filter_tree(work)
    sandboxed = make_backend("rg", work, None)
    fallback = make_backend("python", work, None)
    cases = [None, "*.py", "!*.py", "!**/.git/**", "!*/.git/*", ["*.py", "!**/tests/**"],
             ["!**/tests/**", "*.py"], ["*.py", "!*.test.py"], ["!**/node_modules/**"],
             "!*/repo/*", "!**/repo/**", "!repo/", "!/repo/"]
    for glob in cases:
        for path in (None, "/workspace/src/b.test.py", "/workspace/src"):
            assert implementation.search(sandboxed, "needle", path, glob, "files_with_matches", None, False) == \
                implementation.search(fallback, "needle", path, glob, "files_with_matches", None, False)


@pytest.mark.parametrize("glob,message", [
    ("[a", "unclosed character class"),
    ("a{b", "unclosed alternate group"),
    ("a}b", "unopened alternate group"),
    ("a[z-a]", "invalid range"),
    ("x\\", "dangling"),
    ("#comment", "comment"),
    ("*.py ", "trailing whitespace"),
    ("", "empty pattern"),
    ("!", "empty pattern"),
    ("../*.py", "Path traversal"),
    ("foo/../../etc/*", "Path traversal"),
    (["*.py", "!../secret/**"], "Path traversal"),
    (["*.py", 1], "must be strings"),
])
def test_invalid_globs_are_rejected_before_search(engine, workspace, monkeypatch, glob, message):
    processes = _record_processes(monkeypatch)
    # Non-strings never reach the tool body: the schema rejects them first.
    result = implementation.search(workspace[0], "foo", None, glob, "files_with_matches", None, False)
    assert result.startswith("Error:") and message in result
    assert "No matches found" not in result
    assert processes == []


def test_too_many_globs_are_rejected(engine, workspace):
    assert "at most" in invoke(workspace, pattern="foo", glob=["*.py"] * 65)


def test_globs_are_separate_rg_arguments(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_text("needle\n")
    backend = make_backend("rg", work, None)
    processes = _record_processes(monkeypatch)
    assert implementation.search(backend, "needle", None, ["*.py", "!**/tests/**", "!*.test.py"],
                                 "files_with_matches", None, False) == "/workspace/a.py"
    argv = processes[0].args
    globs = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--glob"]
    assert globs == ["*.py", "!**/tests/**", "!*.test.py", "!.git"]
    assert ", " not in " ".join(globs)


def test_gitignore_hidden_and_override_rules(tmp_path, monkeypatch):
    """rg honors ignore files; the Python engine skips .git and hidden names only."""
    usable_bwrap()
    work = tmp_path / "work"
    _write(work, ".gitignore", "*.log\nbuild/\nnode_modules/\n")
    _write(work, ".ignore", "secret.txt\n")
    _write(work, ".rgignore", "rgonly.txt\n")
    _write(work, ".git/HEAD")
    _write(work, "src/app.py")
    _write(work, "tests/t.py")
    _write(work, ".venv/lib/x.py")
    _write(work, "node_modules/pkg/a.js")
    _write(work, "ignored.log")
    _write(work, "build/out.py")
    _write(work, "secret.txt")
    _write(work, "rgonly.txt")
    _write(work, ".hidden.py")
    sandboxed = make_backend("rg", work, None)
    fallback = make_backend("python", work, None)
    rg = _paths(work, sandboxed)
    python = _paths(work, fallback)
    for banned in ("/.git/", "/.venv/", "/node_modules/", "ignored.log", "/build/", "secret.txt",
                   "rgonly.txt", ".hidden.py"):
        assert not any(banned in path for path in rg)
    assert rg == {"/workspace/src/app.py", "/workspace/tests/t.py"}
    assert "/workspace/.git/HEAD" not in python and "/workspace/.venv/lib/x.py" not in python
    assert "/workspace/.hidden.py" not in python
    assert {"/workspace/node_modules/pkg/a.js", "/workspace/ignored.log", "/workspace/build/out.py",
            "/workspace/secret.txt", "/workspace/rgonly.txt"} <= python

    assert "/workspace/ignored.log" in _paths(work, sandboxed, glob="*.log")
    assert "/workspace/build/out.py" not in _paths(work, sandboxed, glob="*.py")
    assert "/workspace/src/app.py" in _paths(work, sandboxed, glob="*.py")
    assert "/workspace/.hidden.py" in _paths(work, sandboxed, glob=".hidden.py")
    assert "/workspace/build/out.py" in _paths(work, fallback, glob="*.py")
    assert "/workspace/node_modules/pkg/a.js" not in _paths(work, fallback, glob="!**/node_modules/**")
    assert "/workspace/.hidden.py" in _paths(work, fallback, glob=".hidden.py")

    config_dir = tmp_path / "home"
    config_dir.mkdir()
    (config_dir / ".ripgreprc").write_text("--glob=!*.py\n")
    (config_dir / ".config/git").mkdir(parents=True)
    (config_dir / ".config/git/ignore").write_text("*.py\n")
    monkeypatch.setenv("HOME", str(config_dir))
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config_dir / ".ripgreprc"))
    assert "/workspace/src/app.py" in _paths(work, sandboxed)


def test_explicit_git_directory_is_searchable_and_default_search_skips_it(engine, tmp_path):
    work = tmp_path / "work"
    _write(work, ".git/HEAD", "ref needle\n")
    _write(work, "a.py", "needle\n")
    backend = make_backend(engine, work, None)
    assert implementation.search(backend, "needle", None, None, "files_with_matches", None, False) == "/workspace/a.py"
    assert implementation.search(backend, "needle", "/workspace/.git", None, "files_with_matches", None, False) == \
        "/workspace/.git/HEAD"


def test_newline_in_filename_stays_one_result(engine, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    name = "strange\nname.py"
    (work / name).write_text("needle\nneedle\n")
    backend = make_backend(engine, work, None)
    path = f"/workspace/{name}"
    assert implementation.search(backend, "needle", None, None, "files_with_matches", None, False) == path
    assert implementation.search(backend, "needle", None, None, "count", None, False) == f"{path}: 2"
    assert implementation.search(backend, "needle", None, None, "content", None, False) == (
        f"{path}:\n  1: needle\n  2: needle"
    )
    assert implementation.search(backend, "needle", path, None, "count", None, False) == f"{path}: 2"


def test_count_is_complete_per_file_and_max_count_limits_files(engine, workspace):
    (workspace[1] / "many.py").write_text("hit\n" * 1500)
    assert invoke(workspace, pattern="hit", output_mode="count") == "/workspace/many.py: 1500"
    content = invoke(workspace, pattern="hit", output_mode="content")
    assert content.count("\n  ") == 1000 and implementation.TRUNCATED in content
    for index in range(5):
        (workspace[1] / f"m{index}.txt").write_text("hit\n" * 4)
    files = invoke(workspace, pattern="hit", output_mode="files_with_matches", max_count=3)
    listed = [line for line in files.splitlines() if line.startswith("/workspace/")]
    assert len(listed) == 3 and implementation.FILES_TRUNCATED in files
    counted = invoke(workspace, pattern="hit", output_mode="count", max_count=3)
    counts = [line for line in counted.splitlines() if line.startswith("/workspace/")]
    assert len(counts) == 3 and all(line.endswith(": 4") or line.endswith(": 1500") for line in counts)
    assert implementation.FILES_TRUNCATED in counted


def test_python_worker_does_not_inherit_secrets(workspace, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-key")
    seen = []
    original = subprocess.Popen

    def record(*args, **kwargs):
        seen.append(kwargs.get("env"))
        return original(*args, **kwargs)

    monkeypatch.setattr(implementation.subprocess, "Popen", record)
    assert "Error:" not in invoke(workspace, pattern="foo")
    assert seen and "OPENAI_API_KEY" not in seen[0]
    assert seen[0]["LANG"] == "C.UTF-8"


def test_repeated_searches_do_not_leak_descriptors(engine, workspace):
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(25):
        assert invoke(workspace, pattern="foo") == "\n".join([
            "/workspace/a.py", "/workspace/a.txt", "/workspace/src/nested.py",
        ])
    assert len(os.listdir("/proc/self/fd")) <= before + 2


def test_dynamic_system_rg_runs_in_the_search_sandbox(tmp_path, monkeypatch):
    import os as operating_system

    from agent.sandbox import _elf_interpreter

    if not operating_system.path.isfile("/usr/bin/rg") or _elf_interpreter("/usr/bin/rg") is None:
        pytest.skip("dynamically linked system rg is not installed")
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_text("dynneedle\n")
    backend = make_backend("rg", work, None)
    monkeypatch.setattr(implementation, "resolve_rg", lambda: "/usr/bin/rg")
    assert implementation.search(backend, "dynneedle", None, None, "files_with_matches", None, False) == \
        "/workspace/a.py"


def test_mount_fd_does_not_follow_a_swapped_root(engine, tmp_path, monkeypatch):
    """Replacing the workspace directory after it is opened must not retarget the search."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_text("safe-marker\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.py").write_text("secret-marker\n")
    saved = tmp_path / "work.real"
    backend = make_backend(engine, work, None)
    original = implementation.rules_engine.open_mount

    def swap_after_open(root):
        fd = original(root)
        if work.exists() and not work.is_symlink():
            os.rename(work, saved)
            os.symlink(outside, work)
        return fd

    monkeypatch.setattr(implementation.rules_engine, "open_mount", swap_after_open)
    try:
        result = implementation.search(backend, "marker", None, None, "content", None, False)
    finally:
        if work.is_symlink():
            work.unlink()
        if saved.exists() and not work.exists():
            os.rename(saved, work)
    assert "secret-marker" not in result and str(outside) not in result
    if engine == "rg":
        assert "safe-marker" in result
    else:
        # The worker re-opens the path and O_NOFOLLOW rejects the swapped symlink.
        assert result.startswith("Error:")


def test_symlink_swap_after_resolve_cannot_escape(engine, tmp_path, monkeypatch):
    work = tmp_path / "work"
    sub = work / "sub"
    sub.mkdir(parents=True)
    (sub / "a.py").write_text("safe-marker\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret-marker\n")
    backend = make_backend(engine, work, None)
    original = backend.regex_search_location

    def swap_after_resolve(path):
        found = original(path)
        if path == "/workspace/sub" and not sub.is_symlink():
            os.rename(sub, work / "sub.real")
            os.symlink(outside, sub)
        return found

    monkeypatch.setattr(backend, "regex_search_location", swap_after_resolve)
    try:
        result = implementation.search(backend, "marker", "/workspace/sub", None, "content", None, False)
    finally:
        if sub.is_symlink():
            sub.unlink()
            os.rename(work / "sub.real", sub)
    assert "secret-marker" not in result and str(outside) not in result
    assert "Error:" in result


def test_live_directory_symlink_race_stays_inside_the_mount(engine, tmp_path):
    work = tmp_path / "work"
    sub = work / "sub"
    sub.mkdir(parents=True)
    (sub / "a.py").write_text("safe-marker\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.py").write_text("secret-marker\n")
    real = work / "sub.real"
    stop = threading.Event()

    def swap() -> None:
        while not stop.is_set():
            try:
                os.rename(sub, real)
                os.symlink(outside, sub)
                sub.unlink()
                os.rename(real, sub)
            except OSError:
                continue

    backend = make_backend(engine, work, None)
    thread = threading.Thread(target=swap)
    thread.start()
    try:
        for _ in range(15):
            result = implementation.search(backend, "marker", "/workspace/sub", None,
                                           "files_with_matches", None, False)
            assert "secret-marker" not in result and str(outside) not in result
    finally:
        stop.set()
        thread.join(2)
        if sub.is_symlink():
            sub.unlink()
        if real.exists() and not sub.exists():
            os.rename(real, sub)


def test_fast_directory_search_starts_one_sandbox_process(tmp_path, monkeypatch):
    """Smoke guard for the single-rg architecture. G01-G04 live in examples/grep_benchmark.py."""
    work = tmp_path / "work"
    for index in range(1200):
        directory = work / f"p{index // 100:02d}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"f{index}.py").write_text("rare-needle\n" if index == 1199 else "plain\n")
    backend = make_backend("rg", work, None)
    processes = _record_processes(monkeypatch)
    started = time.monotonic()
    result = implementation.search(backend, "rare-needle", None, None, "files_with_matches", None, False)
    assert time.monotonic() - started < 2
    assert result == "/workspace/p11/f1199.py"
    assert len(processes) == 1 and processes[0].args[0] == backend.executor.executable
