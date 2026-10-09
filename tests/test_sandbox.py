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
    assert result["PATH"] == "/usr/local/bin:/usr/bin:/bin"
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


def test_sandbox_backend_adds_execute(tmp_path: Path) -> None:
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
        "glob",
        "grep",
        "write_file",
        "edit_file",
        "delete",
        "execute",
        "write_todos",
        "compact_conversation",
    }
    assert len(names) == 11
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


def test_sandbox_start_failure_is_reported_not_run_on_host(tmp_path: Path) -> None:
    backend = select_backend(replace(config_for(tmp_path), bwrap_path="/bin/false"), check=False).backend
    result = backend.execute("touch host-marker")
    assert result.exit_code == 1
    assert result.termination_reason == "spawn_error"
    assert "sandbox failed to start" in result.output
    assert not (tmp_path / "host-marker").exists()
