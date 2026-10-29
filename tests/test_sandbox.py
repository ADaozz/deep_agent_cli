from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import asyncio

import pytest
from deepagents.backends.protocol import SandboxBackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware, _route_host_path_prompt
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.config import BindMount, SandboxConfig
from agent.factory import DEFAULT_FS_TOOLS, create_agent
from agent.factory import _default_skill_sources
from agent.middleware.workspace_filesystem import WorkspaceFilesystemMiddleware
from agent.middleware.write_operation import WriteOperationMiddleware
from agent.runner import AgentRunner, RunEvent
from agent.sandbox import (
    BubblewrapBackend,
    SandboxUnavailableError,
    WorkspaceCompositeBackend,
    ensure_sandbox_state_dirs,
    sandbox_environment,
    select_backend,
)
from tests.conftest import graph_tool_names, scripted_model


def config_for(workspace: Path, **kwargs) -> SandboxConfig:
    return SandboxConfig(workspace=workspace, bwrap_path="/missing/bwrap", **kwargs)


def _root(path: Path) -> str:
    return path.resolve().as_posix()


def test_sandbox_environment_is_allowlist_based(tmp_path: Path) -> None:
    config = replace(config_for(tmp_path), env_allowlist=("JAVA_HOME",))
    environ = {
        "LANG": "zh_CN.UTF-8",
        "LC_TIME": "C",
        "TERM": "xterm",
        "JAVA_HOME": "/usr/lib/jvm/default-java",
        "OPENAI_API_KEY": "secret",
        "GH_TOKEN": "secret",
        "HTTP_PROXY": "http://proxy",
        "PATH": "/home/user/bin:/bin",
    }
    result = sandbox_environment(config, environ)
    assert result["PATH"] == "/run/deep-agent:/usr/local/bin:/usr/bin:/bin"
    assert result["HOME"] == "/home/agent"
    assert result["PWD"] == _root(tmp_path)
    assert result["TMPDIR"] == "/tmp"
    assert result["LANG"] == "zh_CN.UTF-8"
    assert result["LC_TIME"] == "C"
    assert result["JAVA_HOME"] == "/usr/lib/jvm/default-java"
    assert "OPENAI_API_KEY" not in result
    assert "GH_TOKEN" not in result
    assert "HTTP_PROXY" not in result


def test_host_path_comes_from_config_not_sandbox_mode(tmp_path: Path) -> None:
    host_path = "/home/neo/.espressif/python_env/idf5.4_py3.12_env/bin:/usr/bin"
    opened = SandboxConfig(workspace=tmp_path, mode="full-access")
    assert sandbox_environment(opened, {"PATH": host_path})["PATH"] == "/run/deep-agent:/usr/local/bin:/usr/bin:/bin"
    allowed = replace(opened, env_allowlist=("PATH",))
    assert sandbox_environment(allowed, {"PATH": host_path})["PATH"] == f"/run/deep-agent:{host_path}"
    overridden = sandbox_environment(
        replace(allowed, env_set={"PATH": "/opt/cargo/bin:/usr/bin"}),
        {"PATH": host_path},
    )
    assert overridden["PATH"] == "/run/deep-agent:/opt/cargo/bin:/usr/bin"


def test_bubblewrap_command_has_isolation_mounts_and_no_user_remap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    skills = home / ".deep-agent" / "skills"
    skills.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "work"
    workspace.mkdir()
    config = replace(
        config_for(workspace),
        bwrap_path="/bin/true",
        extra_read_only_mounts=(BindMount(skills, "/opt/skills"),),
    )
    backend = BubblewrapBackend(config, executable="/bin/true")
    args = backend.command_args("id -u")
    joined = " ".join(args)
    assert "--unshare-net" in args
    assert "--unshare-pid" in args
    assert "--unshare-user" not in args
    home_dir, tmp_dir = ensure_sandbox_state_dirs(backend.config)
    assert f"--bind {workspace.resolve()} {_root(workspace)}" in joined
    assert f"--bind {skills.resolve()} /skills" in joined
    assert f"--ro-bind {skills.resolve()} /opt/skills" in joined
    assert f"--bind {tmp_dir} /tmp" in joined
    assert f"--bind {home_dir} /home" in joined
    assert "--tmpfs /tmp" not in joined
    assert "--tmpfs /home" not in joined
    assert "--dir /home/agent" in joined
    assert "--dev /dev" in joined
    assert "--dev-bind /dev /dev" not in joined
    assert args[-3:] == ["/bin/sh", "-lc", "id -u"]


def test_network_capability_toggles_unshare_net(tmp_path: Path) -> None:
    config = replace(config_for(tmp_path), bwrap_path="/bin/true")
    backend = BubblewrapBackend(config, executable="/bin/true")
    assert "--unshare-net" in backend.command_args("true", network=False)
    net_args = backend.command_args("true", network=True)
    assert "--unshare-net" not in net_args
    joined = " ".join(net_args)
    assert any(marker in joined for marker in ("/etc/resolv.conf", "/etc/hosts", "/etc/ssl"))


def test_missing_bwrap_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(SandboxUnavailableError, match="Sandboxing is required"):
        select_backend(config_for(tmp_path))


def test_missing_required_mount_is_rejected(tmp_path: Path) -> None:
    config = replace(
        config_for(tmp_path),
        extra_read_only_mounts=(BindMount(tmp_path / "missing", "/opt/missing"),),
    )
    with pytest.raises(ValueError, match="mount source does not exist"):
        select_backend(config, check=False)


def test_config_errors_do_not_start_a_sandbox(tmp_path: Path) -> None:
    config = replace(
        config_for(tmp_path),
        extra_read_write_mounts=(BindMount(tmp_path, _root(tmp_path)),),
    )
    with pytest.raises(ValueError, match="protected destination"):
        select_backend(config)


def test_skills_mount_cannot_be_replaced(tmp_path: Path) -> None:
    config = replace(config_for(tmp_path), extra_read_write_mounts=(BindMount(tmp_path, "/skills"),))
    with pytest.raises(ValueError, match="conflicts with skills"):
        select_backend(config, check=False)


def test_timeout_is_a_hard_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "work"
    workspace.mkdir()
    executable = _bwrap_executable()
    config = replace(config_for(workspace), bwrap_path=executable, timeout_seconds=1)
    backend = select_backend(config).backend
    try:
        result = backend.execute("sleep 3", timeout=100)
    finally:
        backend.executor.pool.shutdown("timeout cap test finished")
    assert result.exit_code == 124


def test_default_execute_has_no_timeout_cap() -> None:
    from agent.sandbox import _effective_timeout

    assert _effective_timeout(None, None) is None
    assert _effective_timeout(3, None) == 3
    assert _effective_timeout(100, 1) == 1


def test_user_skills_are_writable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    skills = home / ".deep-agent" / "skills"
    skills.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    (skills / "SKILL.md").write_text("instructions")
    workspace = tmp_path / "work"
    workspace.mkdir()
    config = replace(config_for(workspace), bwrap_path="/bin/true")
    selected = select_backend(config, check=False)
    read_result = selected.backend.read("/skills/SKILL.md")
    assert read_result.file_data and read_result.file_data["content"] == "instructions"
    result = selected.backend.write("/skills/installed.txt", "skill")
    assert result.error is None
    assert (skills / "installed.txt").read_text() == "skill"
    outside = selected.backend.write("/outside.txt", "bad")
    assert outside.error and _root(workspace) in outside.error
    assert not (workspace / "outside.txt").exists()


def test_workspace_middleware_reports_executor_and_skips_shell_denial_prompt(
    tmp_path: Path,
) -> None:
    from tests.host_command import HostCommandBackend

    executor = HostCommandBackend(tmp_path)
    composite = WorkspaceCompositeBackend(executor, skills_dir=None)

    unsupported, execution_active, backend = FilesystemMiddleware(
        backend=composite, tools=list(DEFAULT_FS_TOOLS),
    )._unsupported_tools_and_execution_state({"execute", "ls"})
    assert unsupported == {"execute"}
    assert execution_active is False
    # The default route is neither a shell nor a sandbox, so upstream's route
    # prompt blames the shell for paths the execute tool can actually reach.
    assert "not accessible from the shell" in _route_host_path_prompt(composite)

    middleware = WorkspaceFilesystemMiddleware(backend=composite, tools=list(DEFAULT_FS_TOOLS))
    unsupported, execution_active, backend = middleware._unsupported_tools_and_execution_state({"execute", "ls"})
    assert unsupported == set()
    assert execution_active is True
    assert backend is executor
    assert _route_host_path_prompt(backend) == ""

    request = ModelRequest(
        model=scripted_model([AIMessage(content="unused")]),
        messages=[HumanMessage(content="list files")],
        system_message=SystemMessage(content="base"),
        tools=[{"name": "execute"}, {"name": "ls"}],
    )
    seen = []
    middleware.wrap_model_call(request, lambda item: seen.append(item) or item)
    filtered = seen[0]
    assert [tool["name"] for tool in filtered.tools] == ["execute", "ls"]
    assert filtered.system_message.content == "base"


def test_write_operation_uses_pre_write_file_state(tmp_path: Path) -> None:
    backend = select_backend(replace(config_for(tmp_path), bwrap_path="/bin/true"), check=False).backend
    middleware = WriteOperationMiddleware(backend)
    request = SimpleNamespace(tool_call={
        "name": "write_file", "args": {"file_path": f"{_root(tmp_path)}/report.md", "content": "text"},
    })

    def write(_request: object) -> ToolMessage:
        result = backend.write(f"{_root(tmp_path)}/report.md", "text")
        assert result.error is None
        return ToolMessage(content=f"Updated file {_root(tmp_path)}/report.md", tool_call_id="write-1", name="write_file")

    created = middleware.wrap_tool_call(request, write)
    overwritten = middleware.wrap_tool_call(request, write)
    assert created.artifact == {"operation": "create"}
    assert overwritten.artifact == {"operation": "overwrite"}

    empty_request = SimpleNamespace(tool_call={
        "name": "write_file", "args": {"file_path": f"{_root(tmp_path)}/empty.md", "content": ""},
    })

    def write_empty(_request: object) -> ToolMessage:
        result = backend.write(f"{_root(tmp_path)}/empty.md", "")
        assert result.error is None
        return ToolMessage(content=f"Updated file {_root(tmp_path)}/empty.md", tool_call_id="write-empty", name="write_file")

    assert middleware.wrap_tool_call(empty_request, write_empty).artifact == {"operation": "create"}

    def fail(_request: object) -> ToolMessage:
        return ToolMessage(content="failed", tool_call_id="write-2", name="write_file", status="error")

    assert middleware.wrap_tool_call(request, fail).artifact is None

    async def async_write(_request: object) -> ToolMessage:
        result = backend.write(f"{_root(tmp_path)}/async.md", "text")
        assert result.error is None
        return ToolMessage(content=f"Updated file {_root(tmp_path)}/async.md", tool_call_id="write-3", name="write_file")

    async_request = SimpleNamespace(tool_call={
        "name": "write_file", "args": {"file_path": f"{_root(tmp_path)}/async.md", "content": "text"},
    })
    async def check_async() -> None:
        first = await middleware.awrap_tool_call(async_request, async_write)
        second = await middleware.awrap_tool_call(async_request, async_write)
        assert first.artifact == {"operation": "create"}
        assert second.artifact == {"operation": "overwrite"}

    asyncio.run(check_async())


def test_write_operation_reaches_tool_completion_artifact(tmp_path: Path) -> None:
    backend = select_backend(replace(config_for(tmp_path), bwrap_path="/bin/true"), check=False).backend
    path = f"{_root(tmp_path)}/report.md"
    model = scripted_model([
        AIMessage(content="", tool_calls=[{
            "id": "write-new", "name": "write_file",
            "args": {"file_path": path, "content": "first"},
        }]),
        AIMessage(content="done"),
    ])
    runner = AgentRunner(prepared=create_agent(model=model, backend=backend, skills=[]), thread_id="write-operation")
    events: list[RunEvent] = []
    assert runner.invoke("write", on_event=events.append).status == "waiting_confirmation"
    assert runner.approve_tool("write-new", on_event=events.append).status == "completed"
    completed = [event for event in events if event.type == "tool_completed" and event.tool_call_id == "write-new"]
    assert len(completed) == 1
    assert completed[0].artifact == {"operation": "create"}
    runner.close()


def test_default_skills_ignore_project_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "skills").mkdir()
    (workspace / "skills" / "project.txt").write_text("project")
    backend = select_backend(replace(config_for(workspace), bwrap_path="/bin/true"), check=False).backend
    assert _default_skill_sources(backend) == ["/skills/"]
    assert (home / ".deep-agent" / "skills").is_dir()
    listing = backend.ls("/skills")
    assert listing.error is None
    assert all("project.txt" not in str(entry.get("path", "")) for entry in listing.entries or [])


def test_sandbox_backend_adds_execute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    config = replace(config_for(tmp_path), bwrap_path="/bin/true")
    selected = select_backend(config, check=False)
    assert isinstance(selected.backend, SandboxBackendProtocol)
    prepared = create_agent(
        model=scripted_model([AIMessage(content="done")]),
        backend=selected.backend,
        sandbox_config=config,
        skills=[],
    )
    names = graph_tool_names(prepared.graph)
    assert names == {
        "request_human_input",
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "execute",
        "compact_conversation",
        "task",
        "read_subagent_log",
    }
    assert len(names) == 10
    assert "execute" not in prepared.filesystem_tools
    assert "execute" in names


def test_workspace_execute_reaches_model_tool_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Graph registration alone cannot catch middleware filtering at request time."""
    config = replace(config_for(tmp_path), bwrap_path="/bin/true")
    selected = select_backend(config, check=False)
    model = scripted_model([AIMessage(content="done")])
    bound_names: list[set[str]] = []
    original_bind = type(model).bind_tools

    def record_bind(self, tools, **kwargs):  # type: ignore[no-untyped-def]
        bound_names.append({tool.name for tool in tools})
        return original_bind(self, tools, **kwargs)

    monkeypatch.setattr(type(model), "bind_tools", record_bind)
    prepared = create_agent(
        model=model, backend=selected.backend, sandbox_config=config, skills=[],
    )
    assert "execute" in prepared.exposed_tool_names
    prepared.graph.invoke(
        {"messages": [HumanMessage(content="Check available tools")]},
        config={"configurable": {"thread_id": "tool-schema-check"}},
    )
    assert bound_names and "execute" in bound_names[-1]


@pytest.mark.sandbox
def test_real_bubblewrap_workspace_isolation_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import shutil

    executable = shutil.which("bwrap")
    if executable is None:
        if os.environ.get("REQUIRE_BWRAP_TEST"):
            pytest.fail("bubblewrap is required in CI")
        pytest.skip("UNSUPPORTED SANDBOX ENVIRONMENT [not_installed]: bubblewrap is not installed")
    home = tmp_path / "home"
    skills = home / ".deep-agent" / "skills"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("protected")
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "work"
    workspace.mkdir()
    config = replace(config_for(workspace), bwrap_path=executable)
    try:
        selected = select_backend(config)
    except SandboxUnavailableError as exc:
        if os.environ.get("REQUIRE_BWRAP_TEST"):
            pytest.fail(str(exc))
        pytest.skip(f"UNSUPPORTED SANDBOX ENVIRONMENT [{exc.kind}]: {exc}")
    result = selected.backend.execute(
        f"test \"$(pwd)\" = {_root(workspace)} && "
        "test \"$(id -u)\" = \"$(( $(stat -c %u .) ))\" && "
        "printf ok > proof.txt && cat proof.txt && "
        "test \"$(cat /skills/SKILL.md)\" = protected && "
        "printf installed > /skills/installed.txt && "
        "printf keep > /tmp/keep && printf keep > /home/agent/keep && "
        "! (printf bad > /usr/deep-agent-sandbox-probe) 2>/dev/null"
    )
    assert result.exit_code == 0, result.output
    assert (workspace / "proof.txt").read_text() == "ok"
    assert (skills / "installed.txt").read_text() == "installed"
    home_state, tmp_state = ensure_sandbox_state_dirs(config)
    assert (tmp_state / "keep").read_text() == "keep"
    assert (home_state / "agent" / "keep").read_text() == "keep"


@pytest.mark.parametrize("output,kind,expected", [
    ("bwrap: Creating new namespace failed: Operation not permitted", "namespace_restricted", "AppArmor"),
    ("bwrap: No permissions to create new namespace", "namespace_restricted", "userns"),
    ("bwrap: loopback: Failed to create NETLINK_ROUTE socket: Operation not permitted", "namespace_restricted", "seccomp"),
    ("bwrap: Can't find source path /missing", "preflight_failed", "workspace/mount paths"),
])
def test_preflight_diagnostics_fail_closed(tmp_path, monkeypatch, output, kind, expected):
    monkeypatch.setattr(BubblewrapBackend, "execute", lambda *_args, **_kwargs: SimpleNamespace(exit_code=1, output=output))
    config = replace(config_for(tmp_path), bwrap_path="/bin/true")
    with pytest.raises(SandboxUnavailableError) as error:
        select_backend(config)
    assert error.value.kind == kind
    assert output in str(error.value)
    assert expected in str(error.value)
    if kind == "namespace_restricted":
        assert "sudo journalctl" in str(error.value)
        assert "sysctl" in str(error.value)
        assert "sudo apt install" not in str(error.value)


def test_missing_bwrap_has_installation_diagnostic(tmp_path):
    with pytest.raises(SandboxUnavailableError) as error:
        select_backend(config_for(tmp_path))
    assert error.value.kind == "not_installed"
    assert "sudo apt install bubblewrap" in str(error.value)


def test_worker_args_mount_worker_read_only_and_keep_isolation(tmp_path: Path) -> None:
    from agent.sandbox import WORKER_SANDBOX_PATH, WORKER_SOURCE_PATH

    backend = BubblewrapBackend(replace(config_for(tmp_path), bwrap_path="/bin/true"), executable="/bin/true")
    offline = backend.worker_args(network=False)
    joined = " ".join(offline)
    assert f"--ro-bind {WORKER_SOURCE_PATH} {WORKER_SANDBOX_PATH}" in joined
    assert offline[-3:] == ["-I", "-S", WORKER_SANDBOX_PATH]
    assert "--unshare-net" in offline and "--unshare-pid" in offline and "--unshare-ipc" in offline
    assert "--unshare-net" not in backend.worker_args(network=True)


def test_extra_mount_cannot_replace_worker(tmp_path: Path) -> None:
    config = replace(config_for(tmp_path), extra_read_only_mounts=(BindMount(tmp_path, "/run/deep-agent"),))
    with pytest.raises(ValueError, match="sandbox worker"):
        select_backend(config, check=False)


def test_worker_args_mount_environment_rg_read_only(tmp_path, monkeypatch):
    from agent.tools import grep
    from agent.sandbox import SEARCH_BINARY_PATH

    binary = tmp_path / "bin" / "rg"
    binary.parent.mkdir()
    binary.write_text("binary")
    binary.chmod(0o755)
    link = tmp_path / "rg-link"
    link.symlink_to(binary)
    monkeypatch.setattr(grep, "resolve_rg", lambda: str(link))
    backend = BubblewrapBackend(config_for(tmp_path), executable="/usr/bin/bwrap")
    for network in (False, True):
        args = backend.worker_args(network)
        index = args.index(SEARCH_BINARY_PATH)
        assert args[index - 2:index + 1] == ["--ro-bind", str(binary), SEARCH_BINARY_PATH]
        assert ("--unshare-net" in args) is not network


def test_missing_rg_does_not_change_worker_startup(tmp_path, monkeypatch, caplog):
    import logging
    from agent.tools import grep
    from agent.sandbox import SEARCH_BINARY_PATH

    monkeypatch.setattr(grep, "resolve_rg", lambda: None)
    backend = BubblewrapBackend(config_for(tmp_path), executable="/usr/bin/bwrap")
    with caplog.at_level(logging.WARNING, logger="agent.sandbox"):
        args = backend.worker_args()
    assert SEARCH_BINARY_PATH not in args
    assert any("code search is unavailable" in record.message for record in caplog.records)


def test_sandbox_start_failure_is_reported_not_run_on_host(tmp_path: Path) -> None:
    backend = select_backend(replace(config_for(tmp_path), bwrap_path="/bin/false"), check=False).backend
    result = backend.execute("touch host-marker")
    assert result.exit_code == 1
    assert result.termination_reason == "spawn_error"
    assert "sandbox failed to start" in result.output
    assert not (tmp_path / "host-marker").exists()


def _mounted_config(tmp_path: Path, **kwargs) -> tuple[SandboxConfig, Path, Path, Path]:
    workspace = tmp_path / "workspace"
    project = tmp_path / "project"
    reference = tmp_path / "reference"
    workspace.mkdir()
    project.mkdir()
    reference.mkdir()
    config = replace(
        config_for(workspace),
        bwrap_path="/bin/true",
        extra_read_write_mounts=(BindMount(project, "/app"),),
        extra_read_only_mounts=(BindMount(reference, "/reference"),),
        **kwargs,
    )
    return config, workspace, project, reference


def _mounted_backend(tmp_path: Path, **kwargs):
    config, workspace, project, reference = _mounted_config(tmp_path, **kwargs)
    backend = select_backend(config, check=False).backend
    return backend, workspace, project, reference


def test_declared_mounts_are_readable_and_honor_read_only(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    project = tmp_path / "project"
    reference = tmp_path / "reference"
    workspace = tmp_path / "workspace"
    for path in (empty, project, reference, workspace):
        path.mkdir()
    backend = select_backend(replace(
        config_for(workspace),
        bwrap_path="/bin/true",
        extra_read_write_mounts=(BindMount(project, "/app"), BindMount(empty, "/empty")),
        extra_read_only_mounts=(BindMount(reference, "/reference"),),
    ), check=False).backend
    (workspace / "note.txt").write_text("from-workspace")
    (project / "main.py").write_text("print('app')\n")
    (reference / "guide.txt").write_text("guide")

    assert backend.read(f"{_root(workspace)}/note.txt").file_data["content"] == "from-workspace"
    assert backend.read("/app/main.py").file_data["content"] == "print('app')\n"
    listed = {entry["path"] for entry in backend.ls("/app").entries or []}
    assert listed == {"/app/main.py"}
    assert backend.ls("/empty").error is None
    assert backend.ls("/empty").entries == []
    assert [entry["path"] for entry in backend.glob("*.py", "/app").matches or []] == ["/app/main.py"]
    assert any(entry["path"] == "/reference/guide.txt" for entry in backend.glob("**/*").matches or [])

    written = backend.write("/app/created.txt", "created")
    assert written.error is None
    assert (project / "created.txt").read_text() == "created"
    edited = backend.edit("/app/created.txt", "created", "edited")
    assert edited.error is None and (project / "created.txt").read_text() == "edited"
    uploaded = backend.upload_files([("/app/uploaded.txt", b"bytes")])[0]
    assert uploaded.error is None and (project / "uploaded.txt").read_bytes() == b"bytes"
    assert backend.delete("/app/uploaded.txt").error is None
    assert not (project / "uploaded.txt").exists()

    assert backend.read("/reference/guide.txt").file_data["content"] == "guide"
    assert backend.download_files(["/reference/guide.txt"])[0].content == b"guide"
    for denied in (
        backend.write("/reference/new.txt", "no"),
        backend.edit("/reference/guide.txt", "guide", "changed"),
        backend.delete("/reference/guide.txt"),
    ):
        assert denied.error and "Permission denied" in denied.error
    assert backend.upload_files([("/reference/new.txt", b"no")])[0].error
    assert (reference / "guide.txt").read_text() == "guide"
    assert not (reference / "new.txt").exists()
    assert backend.routes["/app/"].max_file_size_bytes == 10 * 1024 * 1024
    assert backend.routes["/reference/"].max_file_size_bytes == 10 * 1024 * 1024


def test_root_listing_and_unscoped_glob_aggregate_without_hiding_denials(tmp_path: Path) -> None:
    backend, workspace, project, reference = _mounted_backend(tmp_path)
    (workspace / "note.txt").write_text("ws")
    (project / "main.py").write_text("app")
    (reference / "guide.txt").write_text("ref")
    root_paths = {entry["path"] for entry in backend.ls("/").entries or []}
    assert {f"{_root(workspace)}/", "/app/", "/reference/"} <= root_paths
    aggregated = {entry["path"] for entry in backend.glob("**/*").matches or []}
    assert {f"{_root(workspace)}/note.txt", "/app/main.py", "/reference/guide.txt"} <= aggregated
    for path in ("/etc", "/usr", "/workspace2", "/app2"):
        assert backend.ls(path).error and "Permission denied" in backend.ls(path).error
        assert backend.ls(path).entries is None
    assert backend.glob("/etc/**").error and "Permission denied" in backend.glob("/etc/**").error
    assert backend.glob("*.py", "/etc").error and "Permission denied" in backend.glob("*.py", "/etc").error
    assert "/usr/" not in backend.routes
    assert "/etc/" not in backend.routes


def test_mount_routes_reject_traversal_symlink_escape_and_prefix_confusion(tmp_path: Path) -> None:
    backend, _workspace, project, _reference = _mounted_backend(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    (project / "escape").symlink_to(secret)
    (project / "inside.txt").write_text("inside")
    escaped = backend.read("/app/escape")
    assert escaped.error and "Permission denied" in escaped.error
    assert str(secret) not in escaped.error
    write = backend.write("/app/escape", "pwned")
    assert write.error and "Permission denied" in write.error
    assert secret.read_text() == "secret"
    traversal = backend.ls("/app/../../etc")
    assert traversal.error and "Permission denied" in traversal.error
    assert str(tmp_path) not in traversal.error
    assert backend.ls("/workspace2").error and "Permission denied" in backend.ls("/workspace2").error
    sibling = tmp_path / "workspace2"
    sibling.mkdir()
    (sibling / "hidden.txt").write_text("hidden")
    assert backend.read("/workspace2/hidden.txt").error
    assert "hidden" not in (backend.read("/workspace2/hidden.txt").file_data or {}).get("content", "")


def test_same_directory_alias_is_allowed_and_runtime_routes_stay_fixed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "shared.txt").write_text("one")
    config = replace(
        config_for(workspace),
        bwrap_path="/bin/true",
        extra_read_write_mounts=(BindMount(workspace, "/app"),),
    )
    backend = select_backend(config, check=False).backend
    assert backend.write("/app/shared.txt", "two").error is None
    assert (workspace / "shared.txt").read_text() == "two"
    assert backend.read(f"{_root(workspace)}/shared.txt").file_data["content"] == "two"
    routes = set(backend.routes)
    backend.executor.config = replace(
        backend.executor.config,
        extra_read_write_mounts=(*backend.executor.config.extra_read_write_mounts, BindMount(workspace, "/elsewhere")),
    )
    assert set(backend.routes) == routes
    assert backend.ls("/elsewhere").error and "Permission denied" in backend.ls("/elsewhere").error


def test_overlapping_and_nested_mounts_fail_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    parent = tmp_path / "parent"
    parent.mkdir()
    child = parent / "child"
    child.mkdir()
    conflicts = [
        replace(
            config_for(workspace),
            extra_read_only_mounts=(BindMount(parent, "/data"),),
            extra_read_write_mounts=(BindMount(other, "/data/child"),),
        ),
        replace(
            config_for(workspace),
            extra_read_only_mounts=(BindMount(child, "/data/child"),),
            extra_read_write_mounts=(BindMount(parent, "/data"),),
        ),
        replace(
            config_for(workspace),
            extra_read_only_mounts=(BindMount(parent, "/app"),),
            extra_read_write_mounts=(BindMount(parent, "/app"),),
        ),
    ]
    for config in conflicts:
        with pytest.raises(ValueError):
            select_backend(replace(config, bwrap_path="/bin/true"), check=False)


def test_aligned_nested_mount_keeps_the_narrower_permission(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = tmp_path / "data"
    child = parent / "sub"
    child.mkdir(parents=True)
    (parent / "root.txt").write_text("root")
    (child / "leaf.txt").write_text("leaf")
    (parent / "link").symlink_to(child / "leaf.txt")
    config = replace(
        config_for(workspace),
        bwrap_path="/bin/true",
        extra_read_only_mounts=(BindMount(parent, "/data"),),
        extra_read_write_mounts=(BindMount(child, "/data/sub"),),
    )
    backend = select_backend(config, check=False).backend
    assert backend.write("/data/root.txt", "changed").error
    assert (parent / "root.txt").read_text() == "root"
    assert backend.write("/data/sub/leaf.txt", "changed").error is None
    assert (child / "leaf.txt").read_text() == "changed"
    (child / "leaf.txt").write_text("leaf")
    assert backend.write("/data/link", "pwn").error is None
    assert (child / "leaf.txt").read_text() == "pwn"

    readonly_child = workspace / "secret"
    readonly_child.mkdir()
    (readonly_child / "file.txt").write_text("safe")
    (workspace / "link").symlink_to(readonly_child / "file.txt")
    nested = replace(
        config_for(workspace),
        bwrap_path="/bin/true",
        extra_read_only_mounts=(BindMount(readonly_child, f"{_root(workspace)}/secret"),),
    )
    protected = select_backend(nested, check=False).backend
    root = _root(workspace)
    assert protected.write(f"{root}/secret/file.txt", "no").error
    assert protected.write(f"{root}/link", "no").error
    assert protected.delete(f"{root}/secret").error
    assert protected.delete(root).error
    assert (readonly_child / "file.txt").read_text() == "safe"
    assert protected.write(f"{root}/other.txt", "yes").error is None
    assert (workspace / "other.txt").read_text() == "yes"
    assert protected.delete(f"{root}/other.txt").error is None
    assert not (workspace / "other.txt").exists()
    assert (readonly_child / "file.txt").read_text() == "safe"


def test_single_file_mount_cannot_be_deleted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "input.txt"
    source.write_text("hello")
    config = replace(
        config_for(workspace),
        bwrap_path="/bin/true",
        extra_read_write_mounts=(BindMount(source, "/app/input.txt"),),
    )
    backend = select_backend(config, check=False).backend
    before = source.stat()
    assert backend.delete("/app/input.txt").error == "Permission denied: cannot delete a sandbox mount point"
    assert source.read_text() == "hello"
    assert source.stat().st_ino == before.st_ino
    assert backend.read("/app/input.txt").file_data["content"] == "hello"
    assert backend.write("/app/input.txt", "rewritten").error is None
    assert source.read_text() == "rewritten"
    assert source.stat().st_ino == before.st_ino
    again = select_backend(config, check=False).backend
    assert again.read("/app/input.txt").file_data["content"] == "rewritten"


def test_directory_mount_roots_cannot_be_deleted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = tmp_path / "project"
    workspace.mkdir()
    project.mkdir()
    (project / "keep.txt").write_text("keep")
    (project / "gone.txt").write_text("gone")
    (workspace / "note.txt").write_text("note")
    config = replace(
        config_for(workspace),
        bwrap_path="/bin/true",
        extra_read_write_mounts=(BindMount(project, "/app"),),
    )
    backend = select_backend(config, check=False).backend
    assert backend.delete("/app").error == "Permission denied: cannot delete a sandbox mount point"
    assert backend.delete(_root(workspace)).error == "Permission denied: cannot delete a sandbox mount point"
    assert project.is_dir() and workspace.is_dir()
    assert (project / "keep.txt").read_text() == "keep"
    assert backend.delete("/app/gone.txt").error is None
    assert not (project / "gone.txt").exists()
    assert backend.delete(f"{_root(workspace)}/note.txt").error is None
    assert not (workspace / "note.txt").exists()
    assert backend.write("/app/keep.txt", "still").error is None
    assert (project / "keep.txt").read_text() == "still"


def test_alias_and_parent_cannot_delete_a_mount_source(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    source = nested / "input.txt"
    source.write_text("mounted")
    (workspace / "other.txt").write_text("other")
    config = replace(
        config_for(workspace),
        bwrap_path="/bin/true",
        extra_read_write_mounts=(
            BindMount(workspace, "/app"),
            BindMount(source, "/opt/input.txt"),
        ),
    )
    backend = select_backend(config, check=False).backend
    root = _root(workspace)
    for path in (root, "/app", "/opt/input.txt", f"{root}/nested/input.txt", "/app/nested/input.txt", f"{root}/nested", "/app/nested"):
        assert backend.delete(path).error == "Permission denied: cannot delete a sandbox mount point"
    assert source.read_text() == "mounted"
    assert backend.delete(f"{root}/other.txt").error is None
    assert not (workspace / "other.txt").exists()
    assert backend.write("/opt/input.txt", "edited").error is None
    assert source.read_text() == "edited"


def test_readonly_and_read_write_aliases_follow_the_virtual_path(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "shared.txt").write_text("original")
    alias = replace(
        config_for(project),
        bwrap_path="/bin/true",
        extra_read_only_mounts=(BindMount(project, "/reference"),),
    )
    backend = select_backend(alias, check=False).backend
    assert backend.write("/reference/shared.txt", "no").error
    assert backend.edit("/reference/shared.txt", "original", "no").error
    assert backend.delete("/reference/shared.txt").error
    assert project.joinpath("shared.txt").read_text() == "original"
    assert backend.write(f"{_root(project)}/shared.txt", "from-rw").error is None
    assert backend.read("/reference/shared.txt").file_data["content"] == "from-rw"
    assert backend.read(f"{_root(project)}/shared.txt").file_data["content"] == "from-rw"

    parent = tmp_path / "parent"
    child = parent / "sub"
    child.mkdir(parents=True)
    (child / "leaf.txt").write_text("leaf")
    separate = tmp_path / "workspace"
    separate.mkdir()
    overlap = replace(
        config_for(separate),
        bwrap_path="/bin/true",
        extra_read_only_mounts=(BindMount(child, "/ro"),),
        extra_read_write_mounts=(BindMount(parent, "/rw"),),
    )
    mounted = select_backend(overlap, check=False).backend
    assert mounted.write("/ro/leaf.txt", "no").error
    assert (child / "leaf.txt").read_text() == "leaf"
    assert mounted.write("/rw/sub/leaf.txt", "from-rw").error is None
    assert mounted.read("/ro/leaf.txt").file_data["content"] == "from-rw"


def test_symlink_delete_removes_the_link_not_its_target(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    reference = tmp_path / "reference"
    workspace.mkdir()
    reference.mkdir()
    target = reference / "guide.txt"
    target.write_text("guide")
    link = workspace / "to-guide"
    link.symlink_to(target)
    readonly_link = reference / "inside"
    readonly_link.symlink_to(target)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    escape = workspace / "escape"
    escape.symlink_to(secret)
    config = replace(
        config_for(workspace),
        bwrap_path="/bin/true",
        extra_read_only_mounts=(BindMount(reference, "/reference"),),
    )
    backend = select_backend(config, check=False).backend
    assert backend.delete(f"{_root(workspace)}/to-guide").error is None
    assert not link.exists() and not link.is_symlink()
    assert target.read_text() == "guide"
    assert backend.delete("/reference/inside").error
    assert readonly_link.is_symlink()
    assert backend.delete(f"{_root(workspace)}/escape").error is None
    assert not escape.is_symlink()
    assert secret.read_text() == "secret"


def test_exact_file_mount_does_not_expose_its_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "only.txt"
    source.write_text("only")
    (tmp_path / "neighbor.txt").write_text("neighbor")
    config = replace(
        config_for(workspace),
        bwrap_path="/bin/true",
        extra_read_only_mounts=(BindMount(source, "/opt/config.yaml"),),
    )
    backend = select_backend(config, check=False).backend
    assert backend.read("/opt/config.yaml").file_data["content"] == "only"
    assert backend.download_files(["/opt/config.yaml"])[0].content == b"only"
    assert backend.ls("/opt").error and "Permission denied" in backend.ls("/opt").error
    assert backend.read("/opt/neighbor.txt").error
    assert backend.write("/opt/config.yaml", "changed").error
    assert source.read_text() == "only"
    root = {entry["path"]: entry.get("is_dir") for entry in backend.ls("/").entries or []}
    assert root["/opt/config.yaml"] is False
    assert "/opt/" not in root
    assert [entry["path"] for entry in backend.glob("**/config.yaml").matches or []] == ["/opt/config.yaml"]
    assert backend.glob("/opt/**").error and "Permission denied" in backend.glob("/opt/**").error


def test_optional_missing_mount_is_not_authorized(tmp_path: Path) -> None:
    from agent.config import Settings

    workspace = tmp_path / "workspace"
    present = tmp_path / "present"
    workspace.mkdir()
    present.mkdir()
    (present / "here.txt").write_text("here")
    config = tmp_path / "config.yaml"
    config.write_text(
        "llm:\n  default: local/test\n  models:\n    local:\n      models:\n        test: {}\n"
        "sandbox:\n"
        "  extra_read_only_mounts:\n"
        f"    - source: {present}\n      destination: /opt/present\n      optional: true\n"
        f"    - source: {tmp_path / 'missing'}\n      destination: /opt/missing\n      optional: true\n"
    )
    settings = Settings.load(config, workspace=workspace)
    assert [mount.destination for mount in settings.sandbox.extra_read_only_mounts] == ["/opt/present"]
    backend = select_backend(replace(settings.sandbox, bwrap_path="/bin/true"), check=False).backend
    assert backend.read("/opt/present/here.txt").file_data["content"] == "here"
    assert backend.ls("/opt/missing").error and "Permission denied" in backend.ls("/opt/missing").error


def test_restricted_file_tools_do_not_open_system_directories(tmp_path: Path) -> None:
    config, workspace, project, _reference = _mounted_config(tmp_path)
    (project / "main.py").write_text("app")
    (workspace / "note.txt").write_text("ws")
    backend = select_backend(config, check=False).backend
    assert backend.read(f"{_root(workspace)}/note.txt").file_data["content"] == "ws"
    assert backend.write("/app/created-by-file-tool.txt", "yes").error is None
    assert (project / "created-by-file-tool.txt").read_text() == "yes"
    assert backend.ls("/usr").error and "Permission denied" in backend.ls("/usr").error
    assert backend.read("/etc/passwd").error


def test_bubblewrap_execute_and_file_tools_share_declared_mounts(tmp_path: Path) -> None:
    import shutil
    import subprocess

    from agent.tools.grep import search

    executable = shutil.which("bwrap")
    if not executable:
        pytest.skip("UNSUPPORTED SANDBOX ENVIRONMENT: bubblewrap is not installed")
    probe = subprocess.run(
        [executable, "--unshare-all", "--ro-bind", "/", "/", "true"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        pytest.skip(f"UNSUPPORTED SANDBOX ENVIRONMENT: {probe.stderr.strip()}")

    workspace = tmp_path / "workspace"
    project = tmp_path / "project"
    reference = tmp_path / "reference"
    workspace.mkdir()
    project.mkdir()
    reference.mkdir()
    (reference / "guide.txt").write_text("guide-marker\n")
    (project / "code.py").write_text("mount-marker\n")
    config = SandboxConfig(
        workspace=workspace,
        bwrap_path=executable,
        extra_read_write_mounts=(BindMount(project, "/app"),),
        extra_read_only_mounts=(BindMount(reference, "/reference"),),
    )
    backend = select_backend(config).backend
    try:
        assert "--unshare-net" in backend.executor.command_args("true", network=False)
        assert "--unshare-net" not in backend.executor.command_args("true", network=True)
        joined = " ".join(backend.executor.sandbox_args())
        assert f"--bind {project.resolve()} /app" in joined
        assert f"--ro-bind {reference.resolve()} /reference" in joined

        created = backend.execute("printf from-exec > /app/test.txt")
        assert created.exit_code == 0
        assert (project / "test.txt").read_text() == "from-exec"
        assert backend.read("/app/test.txt").file_data["content"] == "from-exec"
        assert backend.write("/app/test.txt", "from-file").error is None
        assert (project / "test.txt").read_text() == "from-file"
        echoed = backend.execute("cat /app/test.txt")
        assert echoed.exit_code == 0 and "from-file" in echoed.output
        first_worker = backend.executor.pool.sandbox(False)
        assert backend.execute("true").exit_code == 0
        assert backend.executor.pool.sandbox(False) is first_worker

        readonly = backend.execute("printf nope > /reference/guide.txt")
        assert readonly.exit_code != 0
        assert (reference / "guide.txt").read_text() == "guide-marker\n"
        assert backend.write("/reference/new.txt", "no").error
        assert not (reference / "new.txt").exists()
        assert "mount-marker" in search(backend, "mount-marker", "/app", None, "content", None, False)
        assert "guide-marker" in search(backend, "guide-marker", "/reference", None, "content", None, False)
        assert search(backend, "marker", "/etc", None, "content", None, False).startswith("Error:")

        pool_id = id(backend.executor.pool)
        backend.executor.pool.shutdown("mount integration finished")
        replacement = select_backend(config).backend
        try:
            assert id(replacement.executor.pool) != pool_id
            assert (project / "test.txt").read_text() == "from-file"
            assert replacement.read("/app/test.txt").file_data["content"] == "from-file"
            reread = replacement.execute("cat /app/test.txt")
            assert reread.exit_code == 0 and "from-file" in reread.output
        finally:
            replacement.executor.pool.shutdown("replacement finished")
    finally:
        backend.executor.pool.shutdown("mount integration finished")


def _bwrap_executable() -> str:
    import shutil
    import subprocess

    executable = shutil.which("bwrap")
    if not executable:
        pytest.skip("UNSUPPORTED SANDBOX ENVIRONMENT: bubblewrap is not installed")
    probe = subprocess.run(
        [executable, "--unshare-all", "--ro-bind", "/", "/", "true"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        pytest.skip(f"UNSUPPORTED SANDBOX ENVIRONMENT: {probe.stderr.strip()}")
    return executable


def test_bubblewrap_delete_and_aliases_match_execute(tmp_path: Path) -> None:
    executable = _bwrap_executable()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "input.txt"
    source.write_text("hello")
    file_config = SandboxConfig(
        workspace=workspace,
        bwrap_path=executable,
        extra_read_write_mounts=(BindMount(source, "/app/input.txt"),),
    )
    file_backend = select_backend(file_config).backend
    try:
        inode = source.stat().st_ino
        assert "hello" in file_backend.execute("cat /app/input.txt").output
        assert file_backend.delete("/app/input.txt").error == "Permission denied: cannot delete a sandbox mount point"
        assert source.read_text() == "hello" and source.stat().st_ino == inode
        assert file_backend.read("/app/input.txt").file_data["content"] == "hello"
        assert "hello" in file_backend.execute("cat /app/input.txt").output
    finally:
        file_backend.executor.pool.shutdown("file mount delete")
    restarted = select_backend(file_config).backend
    try:
        assert "hello" in restarted.execute("cat /app/input.txt").output
        assert restarted.read("/app/input.txt").file_data["content"] == "hello"
    finally:
        restarted.executor.pool.shutdown("file mount restarted")

    project = tmp_path / "project"
    project.mkdir()
    (project / "keep.txt").write_text("keep")
    directory_config = SandboxConfig(
        workspace=workspace,
        bwrap_path=executable,
        extra_read_write_mounts=(BindMount(project, "/app"),),
    )
    directory = select_backend(directory_config).backend
    try:
        assert directory.delete("/app").error == "Permission denied: cannot delete a sandbox mount point"
        assert directory.delete(_root(workspace)).error == "Permission denied: cannot delete a sandbox mount point"
        assert project.is_dir() and workspace.is_dir()
        created = directory.execute("printf from-exec > /app/new.txt")
        assert created.exit_code == 0
        assert (project / "new.txt").read_text() == "from-exec"
        assert directory.read("/app/new.txt").file_data["content"] == "from-exec"
        assert directory.execute("true").exit_code == 0
    finally:
        directory.executor.pool.shutdown("directory mount delete")

    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "shared.txt").write_text("original")
    alias_config = SandboxConfig(
        workspace=shared,
        bwrap_path=executable,
        extra_read_only_mounts=(BindMount(shared, "/reference"),),
    )
    alias = select_backend(alias_config).backend
    try:
        assert alias.execute("printf nope > /reference/shared.txt").exit_code != 0
        assert alias.write("/reference/shared.txt", "no").error
        assert shared.joinpath("shared.txt").read_text() == "original"
        assert alias.write(f"{_root(shared)}/shared.txt", "from-rw").error is None
        visible = alias.execute("cat /reference/shared.txt")
        assert visible.exit_code == 0 and "from-rw" in visible.output
        assert alias.read("/reference/shared.txt").file_data["content"] == "from-rw"
    finally:
        alias.executor.pool.shutdown("alias permissions")

    parent = tmp_path / "parent"
    child = parent / "sub"
    child.mkdir(parents=True)
    (child / "leaf.txt").write_text("leaf")
    separate = tmp_path / "separate"
    separate.mkdir()
    overlap_config = SandboxConfig(
        workspace=separate,
        bwrap_path=executable,
        extra_read_only_mounts=(BindMount(child, "/ro"),),
        extra_read_write_mounts=(BindMount(parent, "/rw"),),
    )
    overlap = select_backend(overlap_config).backend
    try:
        assert overlap.execute("printf nope > /ro/leaf.txt").exit_code != 0
        assert overlap.write("/ro/leaf.txt", "no").error
        assert (child / "leaf.txt").read_text() == "leaf"
        assert overlap.execute("printf from-exec > /rw/sub/from-exec.txt").exit_code == 0
        assert overlap.write("/rw/sub/leaf.txt", "from-file").error is None
        assert overlap.execute("cat /ro/leaf.txt").output.strip() == "from-file"
        assert (child / "from-exec.txt").read_text() == "from-exec"
    finally:
        overlap.executor.pool.shutdown("non-nested overlap")

    reference = tmp_path / "reference"
    reference.mkdir()
    guide = reference / "guide.txt"
    guide.write_text("guide")
    link = project / "to-guide"
    link.symlink_to(guide)
    link_for_execute = project / "to-guide-exec"
    link_for_execute.symlink_to(guide)
    link_config = SandboxConfig(
        workspace=workspace,
        bwrap_path=executable,
        extra_read_write_mounts=(BindMount(project, "/app"),),
        extra_read_only_mounts=(BindMount(reference, "/reference"),),
    )
    links = select_backend(link_config).backend
    try:
        assert links.delete("/app/to-guide").error is None
        assert not link.is_symlink()
        assert guide.read_text() == "guide"
        removed = links.execute("rm /app/to-guide-exec")
        assert removed.exit_code == 0
        assert not link_for_execute.is_symlink()
        assert guide.read_text() == "guide"
        assert "guide" in links.execute("cat /reference/guide.txt").output
        assert links.write("/reference/guide.txt", "no").error
        assert guide.read_text() == "guide"
    finally:
        links.executor.pool.shutdown("symlink delete")


def _secret_signature(root: Path) -> tuple[tuple[str, str, int], ...]:
    import hashlib

    rows: list[tuple[str, str, int]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            rows.append((relative, f"link:{path.readlink().as_posix()}", path.lstat().st_ino))
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append((relative, digest, path.stat().st_ino))
        else:
            rows.append((relative, "dir", path.stat().st_ino))
    return tuple(rows)


def test_intermediate_symlink_cannot_mutate_read_only_submount(tmp_path: Path) -> None:
    executable = _bwrap_executable()
    project = tmp_path / "project"
    secret = project / "secret"
    nested = secret / "sub"
    nested.mkdir(parents=True)
    (secret / "file.txt").write_text("orig")
    (nested / "file.txt").write_text("orig")
    (secret / "inner_link").symlink_to("file.txt")
    (project / "alias").symlink_to(Path("secret"))
    (project / "alias2").symlink_to(Path("alias"))
    (project / "alias_abs_host").symlink_to(secret)
    root = _root(project)
    (project / "alias_abs_virtual").symlink_to(f"{root}/secret")
    (project / "to-file").symlink_to(Path("secret") / "file.txt")
    (project / "to-file-exec").symlink_to(Path("secret") / "file.txt")
    (project / "note.txt").write_text("note")
    config = SandboxConfig(
        workspace=project,
        bwrap_path=executable,
        extra_read_only_mounts=(BindMount(secret, f"{root}/secret"),),
    )
    backend = select_backend(config).backend
    try:
        baseline = _secret_signature(secret)
        direct = backend.write(f"{root}/secret/file.txt", "pwn")
        assert direct.error and "mount is read-only" in direct.error
        executed = backend.execute(f"printf pwn > {root}/secret/file.txt")
        assert executed.exit_code != 0 and "Read-only file system" in executed.output
        assert _secret_signature(secret) == baseline

        prefixes = ("alias", "alias2", "alias_abs_host")
        for prefix in prefixes:
            file_path = f"{root}/{prefix}/file.txt"
            nested_path = f"{root}/{prefix}/sub/file.txt"
            created_path = f"{root}/{prefix}/new.txt"
            for denied in (
                backend.write(file_path, "pwn"),
                backend.write(nested_path, "pwn"),
                backend.write(created_path, "pwn"),
                backend.edit(file_path, "orig", "pwn"),
                backend.edit(nested_path, "orig", "pwn"),
            ):
                assert denied.error and "Permission denied" in denied.error
            for uploaded in (
                backend.upload_files([(created_path, b"pwn")])[0],
                backend.upload_files([(file_path, b"pwn")])[0],
                backend.upload_files([(nested_path, b"pwn")])[0],
            ):
                assert uploaded.error and "Permission denied" in uploaded.error
            for removed in (
                backend.delete(file_path),
                backend.delete(nested_path),
                backend.delete(f"{root}/{prefix}/sub"),
                backend.delete(f"{root}/{prefix}/inner_link"),
            ):
                assert removed.error and "Permission denied" in removed.error
            command = backend.execute(f"printf pwn > {root}/{prefix}/file.txt")
            assert command.exit_code != 0
            removed_by_exec = backend.execute(f"rm {root}/{prefix}/file.txt")
            assert removed_by_exec.exit_code != 0
            assert _secret_signature(secret) == baseline
            assert (secret / "file.txt").read_text() == "orig"
            assert (nested / "file.txt").read_text() == "orig"
            assert not (secret / "new.txt").exists()

        assert "Read-only file system" in backend.execute(f"printf pwn > {root}/alias/sub/file.txt").output
        assert "Read-only file system" in backend.execute(f"rm {root}/alias2/sub/file.txt").output
        virtual = backend.write(f"{root}/alias_abs_virtual/file.txt", "pwn")
        assert virtual.error and "Permission denied" in virtual.error
        assert backend.read(f"{root}/alias/file.txt").file_data["content"] == "orig"
        assert _secret_signature(secret) == baseline

        assert backend.delete(f"{root}/to-file").error is None
        assert not (project / "to-file").is_symlink()
        assert (secret / "file.txt").read_text() == "orig"
        removed_link = backend.execute(f"rm {root}/to-file-exec")
        assert removed_link.exit_code == 0
        assert not (project / "to-file-exec").is_symlink()
        assert (secret / "file.txt").read_text() == "orig"
        assert backend.delete(f"{root}/alias").error is None
        assert not (project / "alias").exists() and not (project / "alias").is_symlink()
        assert (secret / "file.txt").read_text() == "orig"
        assert (secret / "inner_link").is_symlink()
        assert backend.write(f"{root}/note.txt", "yes").error is None
        assert (project / "note.txt").read_text() == "yes"
        assert "orig" in backend.execute(f"cat {root}/secret/file.txt").output
    finally:
        backend.executor.pool.shutdown("intermediate symlink")


@pytest.mark.parametrize("custom_path", [False, True])
def test_bubblewrap_rg_is_on_path_in_both_sandboxes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, custom_path) -> None:
    import os
    import shutil
    import subprocess

    from agent.network import reset_execute_network, set_execute_network
    from agent.tools.grep import resolve_rg

    executable = _bwrap_executable()
    host_rg = resolve_rg()
    if not host_rg:
        pytest.skip("rg is not installed on the host")
    real = Path(host_rg).resolve()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "visible.py").write_text("TODO visible\n")
    (workspace / ".hidden.py").write_text("TODO hidden\n")
    gitignore_active = False
    if shutil.which("git"):
        init = subprocess.run(["git", "init"], cwd=workspace, capture_output=True, text=True)
        if init.returncode == 0:
            (workspace / ".gitignore").write_text("secret.py\n")
            (workspace / "secret.py").write_text("TODO secret\n")
            gitignore_active = True

    sources = [str(real)]
    system = Path("/usr/bin/rg")
    if system.is_file() and os.access(system, os.X_OK) and system.resolve() != real:
        sources.append(str(system))
    copied = tmp_path / "usr-bin" / "rg"
    copied.parent.mkdir()
    shutil.copy2(real, copied)
    copied.chmod(0o755)
    sources.append(str(copied))
    link = tmp_path / "venv-bin" / "rg"
    link.parent.mkdir()
    link.symlink_to(real)
    sources.append(str(link))

    def check(source: str) -> None:
        monkeypatch.setattr("agent.tools.grep.resolve_rg", lambda: source)
        env_set = {"PATH": "/opt/node/bin:/opt/cargo/bin:/usr/local/bin:/usr/bin:/bin"} if custom_path else {}
        backend = select_backend(SandboxConfig(workspace=workspace, bwrap_path=executable, env_set=env_set)).backend
        try:
            root = _root(workspace)
            assert backend.read(f"{root}/visible.py").file_data["content"] == "TODO visible\n"
            assert backend.write(f"{root}/written.txt", "from-tool").error is None
            written = backend.execute(f"cat {root}/written.txt")
            assert written.exit_code == 0 and written.output.strip() == "from-tool"
            for network in (False, True):
                token = set_execute_network(network)
                try:
                    located = backend.execute("command -v rg")
                    version = backend.execute("rg --version")
                    content = backend.execute(f"rg -n 'TODO' {root}")
                    defaults = backend.execute(f"rg --files {root}")
                    discovered = backend.execute(f"rg --files -g '*.py' {root}")
                    libraries = backend.execute(
                        "if command -v ldd >/dev/null; then ldd /run/deep-agent/rg; else true; fi"
                    )
                    mutable = backend.execute("printf bad > /run/deep-agent/rg")
                    linked = backend.execute("test -x /run/deep-agent/rg && test ! -L /run/deep-agent/rg")
                finally:
                    reset_execute_network(token)
                assert located.exit_code == 0, located.output
                assert located.output.strip() == "/run/deep-agent/rg"
                assert version.exit_code == 0 and "ripgrep" in version.output
                assert content.exit_code == 0, content.output
                assert "visible.py" in content.output and "TODO visible" in content.output
                assert ".hidden.py" not in content.output
                if gitignore_active:
                    assert "secret.py" not in content.output and "TODO secret" not in content.output
                assert defaults.exit_code == 0, defaults.output
                assert "visible.py" in defaults.output
                assert ".hidden.py" not in defaults.output
                if gitignore_active:
                    assert "secret.py" not in defaults.output
                assert discovered.exit_code == 0, discovered.output
                assert "visible.py" in discovered.output
                assert "not found" not in libraries.output
                assert mutable.exit_code != 0
                assert linked.exit_code == 0
        finally:
            backend.executor.pool.shutdown("rg path")

    for source in sources:
        check(source)


@pytest.mark.parametrize("name", ["deep-agent", "another project", "中文项目", "usr", "skills"])
def test_workspace_uses_its_real_absolute_path(tmp_path, monkeypatch, name):
    from agent.tools.execute import build_execute_tool

    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    project = tmp_path / name
    project.mkdir()
    root = _root(project)
    (project / "note.txt").write_text("project contents")
    config = SandboxConfig(workspace=project, max_output_bytes=4)
    executor = BubblewrapBackend(config, executable="/bin/true")
    backend = WorkspaceCompositeBackend(executor, skills_dir=None)
    args = executor.command_args("pwd")
    binds = list(zip(args, args[1:], args[2:]))
    assert ("--bind", str(project.resolve()), root) in binds
    assert args[args.index("--chdir") + 1] == root
    assert sandbox_environment(config, {})["PWD"] == root
    assert root in build_execute_tool(backend).description
    assert "{workspace_root}" not in build_execute_tool(backend).description
    assert backend.read(f"{root}/note.txt").file_data["content"] == "project contents"
    assert backend.write(f"{root}/created.txt", "new").error is None
    assert backend.read("/workspace/note.txt").error
    assert backend.read(f"{root}2/note.txt").error
    assert backend.delete(root).error == "Permission denied: cannot delete a sandbox mount point"
    assert backend.regex_search_location(None)[2] == root
    assert backend.artifacts_root == f"{root}/.deepagents"
    with pytest.raises(ValueError, match="protected destination"):
        BubblewrapBackend(replace(config, extra_read_write_mounts=(BindMount(project, root),)), executable="/bin/true")
    from tests.host_command import HostCommandBackend

    host = HostCommandBackend(project, max_output_bytes=4)
    output = host.execute("printf 123456")
    assert output.agent_log_path.startswith(f"{root}/.deep-agent/logs/exec/")
    assert backend.read(output.agent_log_path).file_data["content"] == "123456"


@pytest.mark.parametrize("source", ["env_set", "allowlist"])
@pytest.mark.parametrize("path", [
    "/opt/node/bin:/opt/cargo/bin:/usr/bin:/bin",
    "/opt/tools:/run/deep-agent:/usr/bin:/run/deep-agent",
])
def test_custom_path_keeps_builtin_tools_first(tmp_path, source, path):
    config = SandboxConfig(
        workspace=tmp_path,
        env_set={"PATH": path} if source == "env_set" else {},
        env_allowlist=("PATH",) if source == "allowlist" else (),
    )
    environment = sandbox_environment(config, {"PATH": path})
    assert environment["PATH"].split(":") == [
        "/run/deep-agent", *[part for part in path.split(":") if part != "/run/deep-agent"],
    ]


@pytest.mark.parametrize("network", [False, True])
def test_one_shot_sandbox_has_builtin_rg_with_custom_path(tmp_path, network):
    import subprocess

    executable = _bwrap_executable()
    backend = BubblewrapBackend(SandboxConfig(
        workspace=tmp_path,
        env_set={"PATH": "/opt/node/bin:/usr/bin:/bin"},
    ), executable=executable)
    result = subprocess.run(
        backend.command_args("command -v rg && rg --version", network=network),
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "/run/deep-agent/rg"
    assert "ripgrep" in result.stdout


def test_full_access_mounts_the_host_and_masks_agent_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.sandbox import agent_install_paths

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "work"
    workspace.mkdir()
    config_dir = home / ".deep-agent"
    (config_dir / "sandbox").mkdir(parents=True)
    secret = config_dir / "config.yaml"
    secret.write_text("api-key")
    outside = tmp_path / "outside"
    outside.mkdir()
    config = replace(config_for(workspace), bwrap_path="/bin/true", mode="full-access")
    selected = select_backend(config, check=False)
    backend = selected.backend
    args = backend.executor.sandbox_args()
    joined = " ".join(args)
    binds = list(zip(args, args[1:], args[2:]))
    flags = list(zip(args, args[1:]))
    assert ("--bind", "/", "/") not in binds
    assert ("--bind", "/usr", "/usr") in binds
    assert ("--bind", _root(workspace), _root(workspace)) in binds
    assert ("--chdir", _root(workspace)) in flags
    assert ("--proc", "/proc") in flags
    assert ("--dev", "/dev") in flags
    assert ("--dev-bind", "/dev", "/dev") not in binds
    assert ("--tmpfs", "/tmp") not in flags and ("--tmpfs", "/home") not in flags
    stable_tmp = str((config_dir / "sandbox" / "tmp").resolve())
    stable_home = str((config_dir / "sandbox" / "home").resolve())
    assert ("--bind", stable_tmp, "/tmp") not in binds
    assert ("--bind", stable_home, "/home") not in binds
    install = agent_install_paths()[0]
    assert f"--ro-bind {install} {install}" in joined
    assert f"--perms 000 --tmpfs {config_dir.resolve()}" in joined
    assert f"--bind {(config_dir / 'skills').resolve()} /skills" in joined
    written = backend.write(str(outside / "marker.txt"), "real")
    assert written.error is None
    assert (outside / "marker.txt").read_text() == "real"
    denied = backend.read(str(secret))
    assert denied.error and "not accessible" in denied.error
    link = outside / "secret-link"
    link.symlink_to(secret)
    linked = backend.read(str(link))
    assert linked.error and "not accessible" in linked.error
    probe = install / "_sandbox_mode_probe.py"
    readonly = backend.write(str(probe), "nope")
    assert readonly.error and "read-only" in readonly.error
    assert not probe.exists()
    listing = backend.ls("/")
    assert listing.error is None
    names = {entry["path"] for entry in listing.entries or []}
    assert "/skills" in names
    assert str(config_dir) not in names


def test_restricted_keeps_a_private_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "work"
    workspace.mkdir()
    config = replace(config_for(workspace), bwrap_path="/bin/true", mode="restricted")
    args = select_backend(config, check=False).backend.executor.sandbox_args()
    binds = list(zip(args, args[1:], args[2:]))
    flags = list(zip(args, args[1:]))
    assert ("--dev", "/dev") in flags
    assert ("--dev-bind", "/dev", "/dev") not in binds


def test_dev_bind_extra_mount_overlays_sandbox_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "work"
    workspace.mkdir()
    (home / ".deep-agent" / "sandbox").mkdir(parents=True)
    config = replace(
        config_for(workspace),
        bwrap_path="/bin/true",
        mode="full-access",
        extra_read_write_mounts=(BindMount(Path("/dev"), "/dev", dev_bind=True),),
    )
    args = select_backend(config, check=False).backend.executor.sandbox_args()
    binds = list(zip(args, args[1:], args[2:]))
    flags = list(zip(args, args[1:]))
    assert ("--dev", "/dev") in flags
    assert ("--dev-bind", "/dev", "/dev") in binds
    assert args.index("--dev") < args.index("--dev-bind")


def test_mode_scoped_dev_bind_is_absent_from_restricted_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "work"
    workspace.mkdir()
    scoped = BindMount(Path("/dev"), "/dev", dev_bind=True, modes=("full-access",))
    restricted = replace(config_for(workspace), bwrap_path="/bin/true", mode="restricted", extra_read_write_mounts=(scoped,))
    opened = replace(restricted, mode="full-access")
    legacy = replace(restricted, extra_read_write_mounts=(BindMount(Path("/dev"), "/dev", dev_bind=True),))
    restricted_args = select_backend(restricted, check=False).backend.executor.sandbox_args()
    opened_args = select_backend(opened, check=False).backend.executor.sandbox_args()
    legacy_args = select_backend(legacy, check=False).backend.executor.sandbox_args()
    assert ("--dev-bind", "/dev", "/dev") not in list(zip(restricted_args, restricted_args[1:], restricted_args[2:]))
    assert ("--dev-bind", "/dev", "/dev") in list(zip(opened_args, opened_args[1:], opened_args[2:]))
    assert ("--dev-bind", "/dev", "/dev") in list(zip(legacy_args, legacy_args[1:], legacy_args[2:]))


def test_launch_manifest_names_policy_not_a_live_mount_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.sandbox_mounts import render_launch_manifest

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "work"
    workspace.mkdir()
    config = replace(
        config_for(workspace),
        mode="restricted",
        extra_read_write_mounts=(BindMount(Path("/dev"), "/dev", dev_bind=True, modes=("full-access",)),),
    )
    text = render_launch_manifest(config, skills_dir=None, pending_mode="full-access", rg_path=None)
    assert "Sandbox filesystem: restricted" in text
    assert "Pending mode: full-access" in text
    assert "effective launch policy" in text
    assert "[private]" in text
    assert "host dev-bind" not in text
    full = render_launch_manifest(replace(config, mode="full-access"), skills_dir=None, rg_path=None)
    assert "Sandbox filesystem: full-access" in full
    assert "host dev-bind" in full
    assert "Pending mode: none" in full


def test_mode_switch_does_not_publish_when_preparation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "work"
    workspace.mkdir()
    composite = select_backend(replace(config_for(workspace), bwrap_path="/bin/true"), check=False).backend
    assert isinstance(composite, WorkspaceCompositeBackend)
    original = composite.executor.config.mode
    original_route = composite._route_state

    def explode(*_args, **_kwargs):
        raise RuntimeError("route failed")

    monkeypatch.setattr(composite, "_route_state", explode)
    with pytest.raises(RuntimeError, match="route failed"):
        composite.apply_sandbox_mode("full-access")
    assert composite.executor.config.mode == original

    def stuck(_reason: str) -> None:
        raise RuntimeError("pool stuck")

    monkeypatch.setattr(composite, "_route_state", original_route)
    monkeypatch.setattr(composite.executor.pool, "shutdown", stuck)
    with pytest.raises(RuntimeError, match="did not stop"):
        composite.apply_sandbox_mode("full-access")
    assert composite.executor.config.mode == original


def _full_access_composite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[WorkspaceCompositeBackend, Path, Path, Path]:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "work"
    workspace.mkdir()
    config_dir = home / ".deep-agent"
    (config_dir / "sandbox").mkdir(parents=True)
    secret = config_dir / "config.yaml"
    secret.write_text("api-key")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = outside / "secret-link"
    link.symlink_to(secret)
    escaped = workspace / "secret-link"
    escaped.symlink_to(secret)
    config = replace(config_for(workspace), bwrap_path="/bin/true", mode="full-access")
    composite = select_backend(config, check=False).backend
    assert isinstance(composite, WorkspaceCompositeBackend)
    return composite, secret, link, escaped


def test_failed_switch_to_restricted_keeps_full_access_protections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    composite, secret, link, escaped = _full_access_composite(tmp_path, monkeypatch)
    assert "not accessible" in (composite.read(str(secret)).error or "")
    assert "not accessible" in (composite.read(str(link)).error or "")
    assert "escapes" in (composite.read(str(escaped)).error or "")
    install = composite._install_roots[0]
    probe = install / "_sandbox_mode_probe.py"
    try:
        assert composite.write(str(probe), "nope").error
        assert not probe.exists()
        routes, default = composite.routes, composite.default
        hidden, installs = composite._hidden_roots, composite._install_roots
        assert hidden and installs

        def stuck(_reason: str) -> None:
            raise RuntimeError("pool stuck")

        monkeypatch.setattr(composite.executor.pool, "shutdown", stuck)
        with pytest.raises(RuntimeError, match="did not stop"):
            composite.apply_sandbox_mode("restricted")
        assert composite.executor.config.mode == "full-access"
        assert composite.routes is routes and composite.default is default
        assert composite._hidden_roots == hidden and composite._install_roots == installs
        assert "not accessible" in (composite.read(str(secret)).error or "")
        assert "not accessible" in (composite.read(str(link)).error or "")
        assert "escapes" in (composite.read(str(escaped)).error or "")
        assert composite.write(str(probe), "nope").error
        assert not probe.exists()
    finally:
        probe.unlink(missing_ok=True)


def test_route_build_failure_leaves_protections_and_does_not_stop_the_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    composite, secret, _link, _escaped = _full_access_composite(tmp_path, monkeypatch)
    hidden, installs = composite._hidden_roots, composite._install_roots
    routes = composite.routes
    stops: list[str] = []

    def mid(_point, _executor):  # type: ignore[no-untyped-def]
        raise RuntimeError("route failed mid-build")

    def stopped(reason: str) -> None:
        stops.append(reason)

    monkeypatch.setattr("agent.sandbox._directory_backend", mid)
    monkeypatch.setattr(composite.executor.pool, "shutdown", stopped)
    with pytest.raises(RuntimeError, match="route failed mid-build"):
        composite.apply_sandbox_mode("restricted")
    assert stops == []
    assert composite.executor.config.mode == "full-access"
    assert composite.routes is routes
    assert composite._hidden_roots == hidden and composite._install_roots == installs
    assert composite.read(str(secret)).error


def test_failed_switch_to_full_access_does_not_open_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.sandbox_mounts import render_launch_manifest

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "work"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    composite = select_backend(
        replace(config_for(workspace), bwrap_path="/bin/true", mode="restricted"), check=False,
    ).backend
    assert isinstance(composite, WorkspaceCompositeBackend)
    assert composite.write(str(outside / "marker.txt"), "no").error
    routes = composite.routes
    assert composite._hidden_roots == () and composite._install_roots == ()

    def stuck(_reason: str) -> None:
        raise RuntimeError("pool stuck")

    monkeypatch.setattr(composite.executor.pool, "shutdown", stuck)
    with pytest.raises(RuntimeError, match="did not stop"):
        composite.apply_sandbox_mode("full-access")
    assert composite.executor.config.mode == "restricted"
    assert composite.routes is routes
    assert composite._hidden_roots == () and composite._install_roots == ()
    assert composite.write(str(outside / "marker.txt"), "no").error
    manifest = render_launch_manifest(composite.executor.config, skills_dir=composite.skills_dir, rg_path=None)
    assert "Sandbox filesystem: restricted" in manifest
    assert not (outside / "marker.txt").exists()


def test_successful_mode_round_trip_publishes_matching_protections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.sandbox_mounts import render_launch_manifest

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "work"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = home / ".deep-agent" / "config.yaml"
    secret.parent.mkdir(parents=True)
    secret.write_text("api-key")
    composite = select_backend(
        replace(config_for(workspace), bwrap_path="/bin/true", mode="restricted"), check=False,
    ).backend
    assert isinstance(composite, WorkspaceCompositeBackend)
    composite.apply_sandbox_mode("full-access")
    assert composite.executor.config.mode == "full-access"
    assert composite._hidden_roots and composite._install_roots
    assert composite.write(str(outside / "marker.txt"), "yes").error is None
    assert composite.read(str(secret)).error
    full = render_launch_manifest(composite.executor.config, skills_dir=composite.skills_dir, rg_path=None)
    assert "Sandbox filesystem: full-access" in full
    composite.apply_sandbox_mode("restricted")
    assert composite.executor.config.mode == "restricted"
    assert composite._hidden_roots == () and composite._install_roots == ()
    assert composite.write(str(outside / "again.txt"), "no").error
    restricted = render_launch_manifest(composite.executor.config, skills_dir=composite.skills_dir, rg_path=None)
    assert "Sandbox filesystem: restricted" in restricted
    assert not (outside / "again.txt").exists()


def test_runner_reports_sandbox_switch_failure_without_applying_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langchain_core.messages import AIMessage

    from agent.control import RunController
    from agent.factory import AgentSpec, build_agent
    from agent.permission import PermissionMode
    from tests.conftest import scripted_model

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "work"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    config = replace(config_for(workspace), bwrap_path="/bin/true")
    prepared = build_agent(
        AgentSpec(sandbox=config, skills=()),
        scripted_model([AIMessage(content="done")]),
        PermissionMode.ASK,
        None,
        RunController(),
        lambda: False,
        backend_selection=select_backend(config, check=False),
    )
    runner = AgentRunner(prepared=prepared, thread_id="sandbox-switch-fail")
    events: list[RunEvent] = []

    def stuck(_reason: str) -> None:
        raise RuntimeError("pool stuck")

    monkeypatch.setattr(runner.prepared.backend.executor.pool, "shutdown", stuck)
    runner.request_sandbox_mode("full-access")
    runner._apply_pending_runtime_config(events.append)
    assert runner.sandbox_mode() == "restricted"
    assert runner.pending_sandbox_mode() is None
    assert [event.type for event in events] == ["runtime_config_failed"]
    assert "did not stop" in events[0].content
    assert runner.prepared.backend.write(str(outside), "opened").error
    assert not outside.exists()


def test_sandbox_mode_publish_stays_on_the_runner_operation() -> None:
    """File tools and route publish are not overlapped by the runner.

    ``_apply_sandbox_mode`` runs inside ``_apply_pending_runtime_config`` on the
    graph thread between steps, or while the idle operation lock is held, and
    inside session restore on that same operation. Publishing the route tuple
    is therefore not concurrent with a file-tool call from this runner.
    """
    import inspect

    from agent.runner import AgentRunner as Runner

    assert "_apply_sandbox_mode" in inspect.getsource(Runner._apply_pending_runtime_config)
    assert "apply_sandbox_mode" in inspect.getsource(Runner._restore_configured_sandbox_mode)


def test_dev_bind_rejected_on_read_only_mounts(tmp_path: Path) -> None:
    config = replace(
        config_for(tmp_path),
        extra_read_only_mounts=(BindMount(Path("/dev"), "/dev", dev_bind=True),),
    )
    with pytest.raises(ValueError, match="dev_bind cannot be used on extra_read_only_mounts"):
        select_backend(config, check=False)


def test_full_access_rejects_mounts_that_expose_protected_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "work"
    workspace.mkdir()
    config_dir = home / ".deep-agent"
    config_dir.mkdir(parents=True)
    secret = config_dir / "config.yaml"
    secret.write_text("api-key")
    base = replace(config_for(workspace), bwrap_path="/bin/true", mode="full-access")
    with pytest.raises(ValueError, match="configuration directory"):
        select_backend(replace(base, extra_read_only_mounts=(BindMount(secret, "/opt/secret"),)), check=False)
    install = Path(__import__("agent").__file__).resolve().parent
    with pytest.raises(ValueError, match="installation"):
        select_backend(replace(base, extra_read_write_mounts=(BindMount(install, "/opt/agent"),)), check=False)


def test_sandbox_mode_switch_keeps_permission_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langchain_core.messages import AIMessage

    from agent.control import RunController
    from agent.factory import AgentSpec, build_agent
    from agent.permission import PermissionMode
    from tests.conftest import scripted_model

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "work"
    workspace.mkdir()
    config = replace(config_for(workspace), bwrap_path="/bin/true")
    prepared = build_agent(
        AgentSpec(sandbox=config, skills=()),
        scripted_model([AIMessage(content="done")]),
        PermissionMode.ASK,
        None,
        RunController(),
        lambda: False,
        backend_selection=select_backend(config, check=False),
    )
    runner = AgentRunner(prepared=prepared, thread_id="sandbox-mode")
    assert runner.sandbox_mode() == "restricted"
    assert runner.permission_mode() is PermissionMode.ASK
    runner.request_sandbox_mode("full-access")
    assert runner.pending_sandbox_mode() == "full-access"
    assert runner.sandbox_mode() == "restricted"
    runner._apply_pending_runtime_config()
    assert runner.sandbox_mode() == "full-access"
    assert runner.pending_sandbox_mode() is None
    assert runner.permission_mode() is PermissionMode.ASK
    mounted = runner.prepared.backend.executor.sandbox_args()
    mounted_binds = list(zip(mounted, mounted[1:], mounted[2:]))
    assert ("--bind", "/usr", "/usr") in mounted_binds
    assert ("--dev", "/dev") in list(zip(mounted, mounted[1:]))
    assert ("--dev-bind", "/dev", "/dev") not in mounted_binds
    runner._restore_configured_sandbox_mode()
    assert runner.sandbox_mode() == "restricted"
    joined = " ".join(runner.prepared.backend.executor.sandbox_args())
    assert "--bind / /" not in joined
    assert "--tmpfs /tmp" not in joined


def test_both_modes_keep_the_real_workspace_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "deep-agent-sandbox-real-root"
    workspace.mkdir()
    root = _root(workspace)
    executable = _bwrap_executable()
    for mode in ("restricted", "full-access"):
        config = replace(config_for(workspace), bwrap_path=executable, mode=mode)
        backend = select_backend(config).backend
        try:
            result = backend.execute(
                "pwd && printf ok > mode-proof.txt && test -d /usr && test -d /home"
            )
        finally:
            backend.executor.pool.shutdown(f"{mode} real workspace path")
        assert result.exit_code == 0, result.output
        assert result.output.splitlines()[0] == root
        assert (workspace / "mode-proof.txt").read_text() == "ok"
        (workspace / "mode-proof.txt").unlink()
    switched = select_backend(replace(config_for(workspace), bwrap_path=executable, mode="restricted"), check=False)
    switched.backend.apply_sandbox_mode("full-access")
    assert switched.backend.workspace_root == root
    assert switched.backend.executor.workspace_root == root
    full_args = switched.backend.executor.sandbox_args()
    assert full_args[full_args.index("--chdir") + 1] == root
    switched.backend.apply_sandbox_mode("restricted")
    restricted_args = switched.backend.executor.sandbox_args()
    assert restricted_args[restricted_args.index("--chdir") + 1] == root


@pytest.mark.sandbox
def test_full_access_uses_real_host_home_and_tmp(tmp_path):
    import os
    import shlex
    import shutil
    import uuid

    executable = shutil.which("bwrap")
    if executable is None:
        pytest.skip("bubblewrap is not installed")
    workspace = tmp_path / "work"
    workspace.mkdir()
    home = Path.home()
    marker = ".deep-agent-regression-" + uuid.uuid4().hex
    home_marker, tmp_marker = home / marker, Path("/tmp") / marker
    config = replace(config_for(workspace), bwrap_path=executable, mode="full-access")
    try:
        selected = select_backend(config)
    except SandboxUnavailableError as exc:
        if os.environ.get("REQUIRE_BWRAP_TEST"):
            pytest.fail(str(exc))
        pytest.skip(str(exc))
    try:
        command = (
            f'test "$HOME" = {shlex.quote(str(home))} && '
            f'printf real-home > "$HOME"/{marker} && '
            f'printf real-tmp > /tmp/{marker}'
        )
        result = selected.backend.execute(command)
        assert result.exit_code == 0, result.output
        assert home_marker.read_text() == "real-home"
        assert tmp_marker.read_text() == "real-tmp"
    finally:
        home_marker.unlink(missing_ok=True)
        tmp_marker.unlink(missing_ok=True)


def test_single_device_can_be_configured_without_exposing_host_dev(tmp_path):
    config = replace(
        config_for(tmp_path), bwrap_path="/bin/true", mode="full-access",
        extra_read_write_mounts=(BindMount(Path("/dev/null"), "/dev/ttyUSB0", dev_bind=True),),
    )
    args = select_backend(config, check=False).backend.executor.sandbox_args()
    binds = list(zip(args, args[1:], args[2:]))
    assert ("--dev-bind", "/dev/null", "/dev/ttyUSB0") in binds
    assert ("--dev-bind", "/dev", "/dev") not in binds


def test_skills_mount_can_be_disabled_without_creating_global_directory(tmp_path, monkeypatch):
    global_skills = tmp_path / "global-skills"
    monkeypatch.setattr("agent.sandbox.default_skills_dir", lambda: global_skills)
    work = tmp_path / "work"
    work.mkdir()
    backend = BubblewrapBackend(SandboxConfig(workspace=work, mount_skills=False), executable="/bin/true")
    routed = WorkspaceCompositeBackend(backend, skills_dir=backend.skills_dir)
    assert backend.skills_dir is None
    assert "/skills" not in backend.command_args("true")
    assert not global_skills.exists()
    routed.apply_sandbox_mode("full-access")
    assert backend.skills_dir is None
    assert "/skills" not in backend.command_args("true")
    assert routed.read("/skills/private.txt").error


def test_skills_mount_can_use_a_private_directory(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    private = tmp_path / "private-skills"
    global_skills = tmp_path / "global-skills"
    monkeypatch.setattr("agent.sandbox.default_skills_dir", lambda: global_skills)
    config = SandboxConfig(workspace=work, skills_dir=private)
    backend = BubblewrapBackend(config, executable="/bin/true")
    assert backend.skills_dir == private.resolve()
    assert str(private) in backend.command_args("true")
    assert not global_skills.exists()
    with pytest.raises(ValueError):
        BubblewrapBackend(replace(config, skills_dir=work / "nested"), executable="/bin/true")
    link = tmp_path / "link"
    link.symlink_to(private)
    with pytest.raises(ValueError):
        BubblewrapBackend(replace(config, skills_dir=link), executable="/bin/true")
