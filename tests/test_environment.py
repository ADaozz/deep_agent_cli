"""Thread environment snapshots follow the active sandbox, not the host."""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.config import BindMount, SandboxConfig
from agent.environment import (
    EnvironmentCache,
    EnvironmentContextMiddleware,
    classify_runtime,
    collect_environment,
    parse_probe,
    probe_sandbox,
    render_environment_context,
    reset_environment_cache,
    set_environment_cache,
)
from agent.permission import PermissionMode
from tests.conftest import scripted_model


_PROBE = "\n".join([
    "os=Ubuntu 24.04",
    "kernel=6.8.0-generic",
    "arch=x86_64",
    "user=neo",
    "uid=1000",
    "gid=1000",
    "root=false",
    "shell=/bin/sh",
])


def test_partial_probe_marks_missing_fields_unknown() -> None:
    parsed = parse_probe("os=Ubuntu 24.04\nroot=maybe\nuid=nope\n")
    assert parsed["os"] == "Ubuntu 24.04"
    assert parsed["kernel"] == "unknown"
    assert parsed["root"] == "unknown"
    assert parsed["uid"] == "unknown"


def test_failed_probe_does_not_raise_and_keeps_mount_facts(tmp_path: Path) -> None:
    snapshot = collect_environment(
        SandboxConfig(workspace=tmp_path, mode="restricted"),
        PermissionMode.ASK,
        probe_output=None,
        runtime="Linux",
    )
    assert snapshot.os_name == "unknown"
    assert snapshot.home == "/home/agent"
    assert snapshot.home_storage == "persistent"
    assert snapshot.fs_system == "read-only"
    assert snapshot.agent_config == "not-mounted"
    assert snapshot.agent_install == ""
    text = render_environment_context(snapshot)
    assert len(text.splitlines()) == 6
    assert "命令执行环境" in text
    assert "文件工具的可见范围" in text
    assert "以下为" in text
    assert "未提供" not in text
    lowered = text.lower()
    for word in ("cpu", "memory", "meminfo", "loadavg", "package"):
        assert word not in lowered


def test_filesystem_view_follows_the_active_mode(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    restricted = collect_environment(
        SandboxConfig(
            workspace=tmp_path,
            mode="restricted",
            extra_read_only_mounts=(BindMount(source=tmp_path, destination="/opt/node"),),
        ),
        PermissionMode.ASK,
        probe_output=_PROBE,
        runtime="WSL2",
    )
    assert restricted.git_repository == "true"
    assert restricted.extra_mounts == "/opt/node:ro"
    text = render_environment_context(restricted)
    assert "disk_free=" not in text
    assert "system=read-only" in text
    assert "agent_config=not-mounted" in text
    assert "agent_install=" not in text

    opened = collect_environment(
        SandboxConfig(workspace=tmp_path, mode="full-access", env_set={"HOME": "/opt/custom"}),
        PermissionMode.ALLOW,
        probe_output=_PROBE,
        runtime="Linux",
    )
    assert opened.home == "/opt/custom"
    assert opened.home_storage == "unknown"
    assert opened.fs_system == "host-permissions"
    assert opened.agent_config == "hidden"
    assert opened.agent_install == "read-only"
    assert opened.network == "host-network"
    full = render_environment_context(opened)
    assert "agent_install=read-only" in full
    assert "permission=allow" in full


def test_extra_mounts_list_sandbox_destinations_and_access(tmp_path: Path) -> None:
    shown = collect_environment(
        SandboxConfig(
            workspace=tmp_path,
            extra_read_only_mounts=(BindMount(source=tmp_path, destination="/opt/node"),),
            extra_read_write_mounts=(BindMount(source=tmp_path, destination="/opt/cache"),),
        ),
        PermissionMode.ASK,
        probe_output=_PROBE,
        runtime="Linux",
    )
    assert shown.extra_mounts == "/opt/node:ro,/opt/cache:rw"
    assert str(tmp_path) not in shown.extra_mounts

    many = tuple(BindMount(source=tmp_path, destination=f"/opt/m{index}") for index in range(9))
    truncated = collect_environment(
        SandboxConfig(workspace=tmp_path, extra_read_only_mounts=many),
        PermissionMode.ASK,
        probe_output=_PROBE,
        runtime="Linux",
    )
    assert truncated.extra_mounts.endswith(",+1")
    assert truncated.extra_mounts.count(":ro") == 8


def test_permission_switch_does_not_probe_again(tmp_path: Path) -> None:
    calls = {"n": 0}

    def probe() -> str:
        calls["n"] += 1
        return _PROBE

    cache = EnvironmentCache()
    config = SandboxConfig(workspace=tmp_path, mode="restricted")
    first = cache.ensure("thread", config, PermissionMode.ASK, probe=probe, runtime="Linux")
    assert calls["n"] == 1
    assert first.permission == "ask"
    assert first.network == "per-command"

    second = cache.ensure("thread", config, PermissionMode.ALLOW, probe=probe, runtime="Linux")
    assert calls["n"] == 1
    assert second.permission == "allow"
    assert second.network == "host-network"
    assert second.home == first.home

    switched = cache.ensure(
        "thread", replace(config, mode="full-access"), PermissionMode.ALLOW, probe=probe, runtime="Linux",
    )
    assert calls["n"] == 2
    assert switched.sandbox_mode == "full-access"
    assert switched.home == str(Path.home())

    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    cache.ensure(
        "thread", replace(config, workspace=other_workspace), PermissionMode.ASK, probe=probe, runtime="Linux",
    )
    assert calls["n"] == 3
    cache.ensure("thread", replace(config, workspace=other_workspace), PermissionMode.ASK, probe=probe, runtime="Linux")
    assert calls["n"] == 3
    cache.ensure("other-thread", config, PermissionMode.ASK, probe=probe, runtime="Linux")
    assert calls["n"] == 4


def test_probe_failure_inside_cache_still_returns_a_snapshot(tmp_path: Path) -> None:
    def probe() -> str:
        raise OSError("bwrap missing")

    snapshot = EnvironmentCache().ensure(
        "thread", SandboxConfig(workspace=tmp_path), PermissionMode.ASK, probe=probe, runtime="Linux",
    )
    assert snapshot.os_name == "unknown"
    assert snapshot.sandbox_mode == "restricted"


def test_probe_uses_offline_command_and_survives_a_missing_executable() -> None:
    seen: list[bool] = []

    class Fake:
        def command_args(self, command: str, *, network: bool = False) -> list[str]:
            seen.append(network)
            assert "os-release" in command
            assert "git" not in command
            return ["definitely-missing-bwrap-probe"]

    assert probe_sandbox(Fake()) is None
    assert seen == [False]


def test_restricted_probe_reads_os_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.sandbox import BubblewrapBackend, _resolve_executable

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    executable = _resolve_executable("bwrap")
    if executable is None:
        pytest.skip("bubblewrap is not installed")
    workspace = tmp_path / "work"
    workspace.mkdir()
    backend = BubblewrapBackend(
        SandboxConfig(workspace=workspace, bwrap_path=executable), executable=executable,
    )
    output = probe_sandbox(backend)
    if output is None:
        pytest.skip("bubblewrap probe could not start")
    parsed = parse_probe(output)
    release = Path("/etc/os-release").read_text(encoding="utf-8")
    pretty = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in release.splitlines()
        if line.startswith("PRETTY_NAME=")
    )
    assert parsed["os"] == pretty
    assert parsed["shell"] == "/bin/sh"
    assert parsed["uid"] == str(os.getuid())


def test_runtime_classification_prefers_the_container() -> None:
    assert classify_runtime(environ={}, dockerenv=False, cgroup="", osrelease="") == "Linux"
    assert classify_runtime(
        environ={"WSL_DISTRO_NAME": "Ubuntu"}, dockerenv=False, cgroup="", osrelease="",
    ) == "WSL2"
    assert classify_runtime(
        environ={"WSL_DISTRO_NAME": "Ubuntu"}, dockerenv=True, cgroup="", osrelease="microsoft",
    ) == "container"


def test_restored_thread_rechecks_the_sandbox(tmp_path: Path) -> None:
    calls = {"n": 0}

    def probe() -> str:
        calls["n"] += 1
        return _PROBE

    cache = EnvironmentCache()
    config = SandboxConfig(workspace=tmp_path)
    cache.ensure("thread", config, PermissionMode.ASK, probe=probe, runtime="Linux")
    cache.ensure("thread", config, PermissionMode.ASK, probe=probe, runtime="Linux")
    assert calls["n"] == 1
    cache.invalidate("thread")
    cache.ensure("thread", config, PermissionMode.ASK, probe=probe, runtime="Linux")
    assert calls["n"] == 2


def test_middleware_appends_the_short_snapshot_on_every_call(tmp_path: Path) -> None:
    snapshot = collect_environment(
        SandboxConfig(workspace=tmp_path), PermissionMode.ASK, probe_output=_PROBE, runtime="Linux",
    )
    cache = EnvironmentCache()
    cache.snapshot = snapshot
    request = ModelRequest(
        model=scripted_model([AIMessage(content="unused")]),
        messages=[HumanMessage(content="hi")],
        system_message=SystemMessage(content="base"),
    )
    seen = []
    token = set_environment_cache(cache)
    try:
        middleware = EnvironmentContextMiddleware()
        middleware.wrap_model_call(request, lambda item: seen.append(item) or item)
        middleware.wrap_model_call(request, lambda item: seen.append(item) or item)
    finally:
        reset_environment_cache(token)
    assert seen[0].system_message.content.startswith("base\n\n环境信息\n")
    assert seen[1].system_message.content.startswith("base\n\n环境信息\n")
    assert len(seen[0].system_message.content.splitlines()) == 8
    assert request.system_message.content == "base"
    assert request.messages[0].content == "hi"
