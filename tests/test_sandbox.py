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
    ExecutionMode,
    SandboxUnavailableError,
    UnsandboxedShellBackend,
    WorkspaceCompositeBackend,
    sandbox_environment,
    select_backend,
)
from tests.conftest import graph_tool_names, scripted_model


def config_for(workspace: Path, **kwargs) -> SandboxConfig:
    return SandboxConfig(workspace=workspace, bwrap_path="/missing/bwrap", **kwargs)


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
    assert result["PWD"] == "/workspace"
    assert result["TMPDIR"] == "/tmp"
    assert result["LANG"] == "zh_CN.UTF-8"
    assert result["LC_TIME"] == "C"
    assert result["JAVA_HOME"] == "/usr/lib/jvm/default-java"
    assert "OPENAI_API_KEY" not in result
    assert "GH_TOKEN" not in result
    assert "HTTP_PROXY" not in result


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
    assert f"--bind {workspace.resolve()} /workspace" in joined
    assert f"--ro-bind {skills.resolve()} /skills" in joined
    assert f"--ro-bind {skills.resolve()} /opt/skills" in joined
    assert "--tmpfs /home --dir /home/agent" in joined
    assert args[-3:] == ["/bin/sh", "-lc", "id -u"]


def test_network_capability_toggles_unshare_net(tmp_path: Path) -> None:
    config = replace(config_for(tmp_path), bwrap_path="/bin/true")
    backend = BubblewrapBackend(config, executable="/bin/true")
    assert "--unshare-net" in backend.command_args("true", network=False)
    net_args = backend.command_args("true", network=True)
    assert "--unshare-net" not in net_args
    joined = " ".join(net_args)
    assert any(marker in joined for marker in ("/etc/resolv.conf", "/etc/hosts", "/etc/ssl"))


def test_missing_bwrap_fails_closed_without_authorization(tmp_path: Path) -> None:
    with pytest.raises(SandboxUnavailableError, match="sandbox.allow_unsandboxed"):
        select_backend(config_for(tmp_path))


def test_missing_bwrap_explicitly_falls_back_and_warns(tmp_path: Path) -> None:
    config = config_for(tmp_path, allow_unsandboxed=True)
    with pytest.warns(RuntimeWarning, match="UNSANDBOXED MODE"):
        selected = select_backend(config)
    assert selected.mode is ExecutionMode.UNSANDBOXED
    assert "current user's permissions" in selected.warning
    result = selected.backend.execute("pwd")
    assert result.exit_code == 0
    assert str(tmp_path.resolve()) in result.output


def test_unsandboxed_fallback_reports_mode_and_limits_execution(tmp_path: Path) -> None:
    config = replace(
        config_for(tmp_path, allow_unsandboxed=True),
        max_output_bytes=4,
    )
    with pytest.warns(RuntimeWarning, match="UNSANDBOXED MODE"):
        prepared = create_agent(
            model=scripted_model([AIMessage(content="done")]),
            sandbox_config=config,
            skills=[],
        )
    assert prepared.execution_mode is ExecutionMode.UNSANDBOXED
    assert "UNSANDBOXED MODE" in prepared.security_warning
    truncated = prepared.backend.execute("printf 123456")
    assert truncated.truncated is True
    assert truncated.output.startswith("3456")
    assert f"Full output saved to: {tmp_path}/.deep-agent/logs/exec/" in truncated.output
    assert "Agent path: /workspace/.deep-agent/logs/exec/" in truncated.output
    timed_out = prepared.backend.execute("sleep 5", timeout=1)
    assert timed_out.exit_code == 124


def test_missing_required_mount_is_rejected(tmp_path: Path) -> None:
    config = replace(
        config_for(tmp_path, allow_unsandboxed=True),
        extra_read_only_mounts=(BindMount(tmp_path / "missing", "/opt/missing"),),
    )
    with pytest.raises(ValueError, match="mount source does not exist"):
        select_backend(config, check=False)


def test_config_errors_do_not_trigger_unsafe_fallback(tmp_path: Path) -> None:
    config = replace(
        config_for(tmp_path, allow_unsandboxed=True),
        extra_read_write_mounts=(BindMount(tmp_path, "/workspace"),),
    )
    with pytest.raises(ValueError, match="protected destination"):
        select_backend(config)


def test_skills_mount_cannot_be_replaced(tmp_path: Path) -> None:
    config = replace(config_for(tmp_path), extra_read_write_mounts=(BindMount(tmp_path, "/skills"),))
    with pytest.raises(ValueError, match="conflicts with skills"):
        select_backend(config, check=False)


def test_timeout_is_a_hard_cap_for_unsandboxed_execution(tmp_path: Path) -> None:
    config = replace(config_for(tmp_path, allow_unsandboxed=True), timeout_seconds=1)
    with pytest.warns(RuntimeWarning, match="UNSANDBOXED"):
        backend = select_backend(config).backend
    result = backend.execute("sleep 3", timeout=100)
    assert result.exit_code == 124


def test_default_execute_has_no_timeout_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepagents.backends.protocol import ExecuteResponse
    from agent import sandbox

    seen: list[int | None] = []

    def capture_execute(_command: str, *, timeout: int | None, **_kwargs: object) -> ExecuteResponse:
        seen.append(timeout)
        return ExecuteResponse("ok", 0, False)

    monkeypatch.setattr(sandbox, "_run_process", capture_execute)
    backend = sandbox.UnsandboxedShellBackend(config_for(tmp_path, allow_unsandboxed=True))
    backend.execute("true")
    backend.execute("true", timeout=3)
    assert seen == [None, 3]


def test_user_skills_are_read_only_for_file_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    result = selected.backend.write("/skills/blocked.txt", "bad")
    assert result.error and "Permission denied" in result.error
    assert not (skills / "blocked.txt").exists()
    assert selected.backend.delete("/skills/SKILL.md").error
    assert selected.backend.upload_files([("/skills/new", b"bad")])[0].error
    assert not (skills / "new").exists()
    outside = selected.backend.write("/outside.txt", "bad")
    assert outside.error and "under /workspace" in outside.error
    assert not (workspace / "outside.txt").exists()


def test_workspace_middleware_reports_executor_and_skips_shell_denial_prompt(
    tmp_path: Path,
) -> None:
    executor = UnsandboxedShellBackend(config_for(tmp_path, allow_unsandboxed=True))
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
        "name": "write_file", "args": {"file_path": "/workspace/report.md", "content": "text"},
    })

    def write(_request: object) -> ToolMessage:
        result = backend.write("/workspace/report.md", "text")
        assert result.error is None
        return ToolMessage(content="Updated file /workspace/report.md", tool_call_id="write-1", name="write_file")

    created = middleware.wrap_tool_call(request, write)
    overwritten = middleware.wrap_tool_call(request, write)
    assert created.artifact == {"operation": "create"}
    assert overwritten.artifact == {"operation": "overwrite"}

    empty_request = SimpleNamespace(tool_call={
        "name": "write_file", "args": {"file_path": "/workspace/empty.md", "content": ""},
    })

    def write_empty(_request: object) -> ToolMessage:
        result = backend.write("/workspace/empty.md", "")
        assert result.error is None
        return ToolMessage(content="Updated file /workspace/empty.md", tool_call_id="write-empty", name="write_file")

    assert middleware.wrap_tool_call(empty_request, write_empty).artifact == {"operation": "create"}

    def fail(_request: object) -> ToolMessage:
        return ToolMessage(content="failed", tool_call_id="write-2", name="write_file", status="error")

    assert middleware.wrap_tool_call(request, fail).artifact is None

    async def async_write(_request: object) -> ToolMessage:
        result = backend.write("/workspace/async.md", "text")
        assert result.error is None
        return ToolMessage(content="Updated file /workspace/async.md", tool_call_id="write-3", name="write_file")

    async_request = SimpleNamespace(tool_call={
        "name": "write_file", "args": {"file_path": "/workspace/async.md", "content": "text"},
    })
    async def check_async() -> None:
        first = await middleware.awrap_tool_call(async_request, async_write)
        second = await middleware.awrap_tool_call(async_request, async_write)
        assert first.artifact == {"operation": "create"}
        assert second.artifact == {"operation": "overwrite"}

    asyncio.run(check_async())


def test_write_operation_reaches_tool_completion_artifact(tmp_path: Path) -> None:
    backend = select_backend(replace(config_for(tmp_path), bwrap_path="/bin/true"), check=False).backend
    path = "/workspace/report.md"
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
    backend = select_backend(replace(config_for(workspace), bwrap_path="/bin/true"), check=False).backend
    assert _default_skill_sources(backend) is None
    (home / ".deep-agent" / "skills").mkdir(parents=True)
    backend = select_backend(replace(config_for(workspace), bwrap_path="/bin/true"), check=False).backend
    assert _default_skill_sources(backend) == ["/skills/"]


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
    }
    assert len(names) == 8
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
        "test \"$(pwd)\" = /workspace && "
        "test \"$(id -u)\" = \"$(( $(stat -c %u .) ))\" && "
        "printf ok > proof.txt && cat proof.txt && "
        "test \"$(cat /skills/SKILL.md)\" = protected && "
        "! (printf bad > /skills/SKILL.md) 2>/dev/null"
    )
    assert result.exit_code == 0, result.output
    assert (workspace / "proof.txt").read_text() == "ok"
    assert (skills / "SKILL.md").read_text() == "protected"


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

    assert backend.read("/workspace/note.txt").file_data["content"] == "from-workspace"
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
    assert {"/workspace/", "/app/", "/reference/"} <= root_paths
    aggregated = {entry["path"] for entry in backend.glob("**/*").matches or []}
    assert {"/workspace/note.txt", "/app/main.py", "/reference/guide.txt"} <= aggregated
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
    assert backend.read("/workspace/shared.txt").file_data["content"] == "two"
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
        extra_read_only_mounts=(BindMount(readonly_child, "/workspace/secret"),),
    )
    protected = select_backend(nested, check=False).backend
    assert protected.write("/workspace/secret/file.txt", "no").error
    assert protected.write("/workspace/link", "no").error
    assert protected.delete("/workspace/secret").error
    assert protected.delete("/workspace").error
    assert (readonly_child / "file.txt").read_text() == "safe"
    assert protected.write("/workspace/other.txt", "yes").error is None
    assert (workspace / "other.txt").read_text() == "yes"
    assert protected.delete("/workspace/other.txt").error is None
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
    assert backend.delete("/workspace").error == "Permission denied: cannot delete a sandbox mount point"
    assert project.is_dir() and workspace.is_dir()
    assert (project / "keep.txt").read_text() == "keep"
    assert backend.delete("/app/gone.txt").error is None
    assert not (project / "gone.txt").exists()
    assert backend.delete("/workspace/note.txt").error is None
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
    for path in ("/workspace", "/app", "/opt/input.txt", "/workspace/nested/input.txt", "/app/nested/input.txt", "/workspace/nested", "/app/nested"):
        assert backend.delete(path).error == "Permission denied: cannot delete a sandbox mount point"
    assert source.read_text() == "mounted"
    assert backend.delete("/workspace/other.txt").error is None
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
    assert backend.write("/workspace/shared.txt", "from-rw").error is None
    assert backend.read("/reference/shared.txt").file_data["content"] == "from-rw"
    assert backend.read("/workspace/shared.txt").file_data["content"] == "from-rw"

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
    assert backend.delete("/workspace/to-guide").error is None
    assert not link.exists() and not link.is_symlink()
    assert target.read_text() == "guide"
    assert backend.delete("/reference/inside").error
    assert readonly_link.is_symlink()
    assert backend.delete("/workspace/escape").error is None
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


def test_unsandboxed_mode_does_not_treat_virtual_mounts_as_host_permissions(tmp_path: Path) -> None:
    config, workspace, project, _reference = _mounted_config(tmp_path, allow_unsandboxed=True)
    (project / "main.py").write_text("app")
    (workspace / "note.txt").write_text("ws")
    with pytest.warns(RuntimeWarning, match="UNSANDBOXED MODE"):
        backend = select_backend(config).backend
    assert backend.ls("/app").error and "Permission denied" in backend.ls("/app").error
    assert backend.ls("/usr").error and "Permission denied" in backend.ls("/usr").error
    assert backend.ls("/etc").error and "Permission denied" in backend.ls("/etc").error
    assert backend.read("/workspace/note.txt").file_data["content"] == "ws"
    assert not (project / "created-by-file-tool.txt").exists()
    assert backend.write("/app/created-by-file-tool.txt", "no").error


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
        assert directory.delete("/workspace").error == "Permission denied: cannot delete a sandbox mount point"
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
        assert alias.write("/workspace/shared.txt", "from-rw").error is None
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
    (project / "alias_abs_virtual").symlink_to("/workspace/secret")
    (project / "to-file").symlink_to(Path("secret") / "file.txt")
    (project / "to-file-exec").symlink_to(Path("secret") / "file.txt")
    (project / "note.txt").write_text("note")
    config = SandboxConfig(
        workspace=project,
        bwrap_path=executable,
        extra_read_only_mounts=(BindMount(secret, "/workspace/secret"),),
    )
    backend = select_backend(config).backend
    try:
        baseline = _secret_signature(secret)
        direct = backend.write("/workspace/secret/file.txt", "pwn")
        assert direct.error and "mount is read-only" in direct.error
        executed = backend.execute("printf pwn > /workspace/secret/file.txt")
        assert executed.exit_code != 0 and "Read-only file system" in executed.output
        assert _secret_signature(secret) == baseline

        prefixes = ("alias", "alias2", "alias_abs_host")
        for prefix in prefixes:
            file_path = f"/workspace/{prefix}/file.txt"
            nested_path = f"/workspace/{prefix}/sub/file.txt"
            created_path = f"/workspace/{prefix}/new.txt"
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
                backend.delete(f"/workspace/{prefix}/sub"),
                backend.delete(f"/workspace/{prefix}/inner_link"),
            ):
                assert removed.error and "Permission denied" in removed.error
            command = backend.execute(f"printf pwn > /workspace/{prefix}/file.txt")
            assert command.exit_code != 0
            removed_by_exec = backend.execute(f"rm /workspace/{prefix}/file.txt")
            assert removed_by_exec.exit_code != 0
            assert _secret_signature(secret) == baseline
            assert (secret / "file.txt").read_text() == "orig"
            assert (nested / "file.txt").read_text() == "orig"
            assert not (secret / "new.txt").exists()

        assert "Read-only file system" in backend.execute("printf pwn > /workspace/alias/sub/file.txt").output
        assert "Read-only file system" in backend.execute("rm /workspace/alias2/sub/file.txt").output
        virtual = backend.write("/workspace/alias_abs_virtual/file.txt", "pwn")
        assert virtual.error and "Permission denied" in virtual.error
        assert backend.read("/workspace/alias/file.txt").file_data["content"] == "orig"
        assert _secret_signature(secret) == baseline

        assert backend.delete("/workspace/to-file").error is None
        assert not (project / "to-file").is_symlink()
        assert (secret / "file.txt").read_text() == "orig"
        removed_link = backend.execute("rm /workspace/to-file-exec")
        assert removed_link.exit_code == 0
        assert not (project / "to-file-exec").is_symlink()
        assert (secret / "file.txt").read_text() == "orig"
        assert backend.delete("/workspace/alias").error is None
        assert not (project / "alias").exists() and not (project / "alias").is_symlink()
        assert (secret / "file.txt").read_text() == "orig"
        assert (secret / "inner_link").is_symlink()
        assert backend.write("/workspace/note.txt", "yes").error is None
        assert (project / "note.txt").read_text() == "yes"
        assert "orig" in backend.execute("cat /workspace/secret/file.txt").output
    finally:
        backend.executor.pool.shutdown("intermediate symlink")


def test_bubblewrap_rg_is_on_path_in_both_sandboxes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        backend = select_backend(SandboxConfig(workspace=workspace, bwrap_path=executable)).backend
        try:
            assert backend.read("/workspace/visible.py").file_data["content"] == "TODO visible\n"
            assert backend.write("/workspace/written.txt", "from-tool").error is None
            written = backend.execute("cat /workspace/written.txt")
            assert written.exit_code == 0 and written.output.strip() == "from-tool"
            for network in (False, True):
                token = set_execute_network(network)
                try:
                    located = backend.execute("command -v rg")
                    version = backend.execute("rg --version")
                    content = backend.execute("rg -n 'TODO' /workspace")
                    defaults = backend.execute("rg --files /workspace")
                    discovered = backend.execute("rg --files -g '*.py' /workspace")
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


def test_example_config_path_resolves_rg_to_readonly_mount(tmp_path: Path) -> None:
    from importlib.resources import files

    import yaml
    from agent.config import _sandbox_from_mapping
    from agent.sandbox import SEARCH_BINARY_PATH

    executable = _bwrap_executable()
    loaded = yaml.safe_load(files("agent").joinpath("config.example.yaml").read_text(encoding="utf-8"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    section = dict(loaded["sandbox"])
    section["workspace"] = str(workspace)
    section["extra_read_only_mounts"] = []
    section["extra_read_write_mounts"] = []
    config = replace(
        _sandbox_from_mapping(section, base_dir=workspace),
        bwrap_path=executable,
        workspace=workspace,
    )
    backend = select_backend(config).backend
    try:
        located = backend.execute("command -v rg")
        named = backend.execute("rg --version")
        mounted = backend.execute(f"{SEARCH_BINARY_PATH} --version")
        assert located.exit_code == 0, located.output
        assert located.output.strip() == SEARCH_BINARY_PATH
        assert named.exit_code == 0, named.output
        assert mounted.exit_code == 0, mounted.output
        assert named.output.splitlines()[0] == mounted.output.splitlines()[0]
    finally:
        backend.executor.pool.shutdown("example rg path")
