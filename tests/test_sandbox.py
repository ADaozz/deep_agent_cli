from dataclasses import replace
from pathlib import Path

import pytest
from deepagents.backends.protocol import SandboxBackendProtocol
from langchain_core.messages import AIMessage

from agent.config import BindMount, SandboxConfig
from agent.factory import create_agent
from agent.sandbox import (
    BubblewrapBackend,
    ExecutionMode,
    SandboxUnavailableError,
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


def test_bubblewrap_command_has_isolation_mounts_and_no_user_remap(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    config = replace(
        config_for(tmp_path),
        bwrap_path="/bin/true",
        extra_read_only_mounts=(BindMount(tmp_path / "skills", "/opt/skills"),),
    )
    backend = BubblewrapBackend(config, executable="/bin/true")
    args = backend.command_args("id -u")
    joined = " ".join(args)
    assert "--unshare-net" in args
    assert "--unshare-pid" in args
    assert "--unshare-user" not in args
    assert f"--bind {tmp_path.resolve()} /workspace" in joined
    assert f"--ro-bind {skills.resolve()} /workspace/skills" in joined
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
    with pytest.raises(SandboxUnavailableError, match="SANDBOX_ALLOW_UNSANDBOXED"):
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
    assert truncated.output.startswith("1234")
    timed_out = prepared.backend.execute("sleep 5", timeout=1)
    assert timed_out.exit_code == 124


def test_config_errors_do_not_trigger_unsafe_fallback(tmp_path: Path) -> None:
    config = replace(
        config_for(tmp_path, allow_unsandboxed=True),
        extra_read_write_mounts=(BindMount(tmp_path, "/workspace"),),
    )
    with pytest.raises(ValueError, match="protected destination"):
        select_backend(config)


def test_protected_workspace_paths_are_read_only_for_file_backend(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "SKILL.md").write_text("instructions")
    config = replace(config_for(tmp_path), bwrap_path="/bin/true")
    selected = select_backend(config, check=False)
    read_result = selected.backend.read("/workspace/skills/SKILL.md")
    assert read_result.file_data and read_result.file_data["content"] == "instructions"
    result = selected.backend.write("/workspace/skills/blocked.txt", "bad")
    assert result.error and "Permission denied" in result.error
    assert not (skills / "blocked.txt").exists()
    outside = selected.backend.write("/outside.txt", "bad")
    assert outside.error and "under /workspace" in outside.error
    assert not (tmp_path / "outside.txt").exists()


def test_sandbox_backend_adds_execute_as_thirteenth_tool(tmp_path: Path) -> None:
    config = replace(config_for(tmp_path), bwrap_path="/bin/true", protected_workspace_paths=())
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
        "lookup_docs",
        "send_email",
        "handoff_to_human",
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
    }
    assert len(names) == 13
    assert "execute" not in prepared.filesystem_tools
    assert "execute" in names


def test_real_bubblewrap_workspace_isolation_when_available(tmp_path: Path) -> None:
    import shutil

    executable = shutil.which("bwrap")
    if executable is None:
        pytest.skip("bubblewrap is not installed")
    config = replace(config_for(tmp_path), bwrap_path=executable)
    try:
        selected = select_backend(config)
    except SandboxUnavailableError as exc:
        pytest.skip(str(exc))
    result = selected.backend.execute(
        "test \"$(pwd)\" = /workspace && "
        "test \"$(id -u)\" = \"$(( $(stat -c %u .) ))\" && "
        "printf ok > proof.txt && cat proof.txt"
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "proof.txt").read_text() == "ok"
