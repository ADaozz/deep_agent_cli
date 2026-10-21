"""Thread environment snapshot for the sandbox the next execute will enter.

The snapshot is collected from the active sandbox mounts plus one offline,
read-only probe. It is not a host inventory: CPU load, memory, process lists,
and installed packages are out of scope. Fields that cannot be read become
``unknown`` and never fail the thread.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
import logging
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

from agent.config import SandboxConfig
from agent.permission import PermissionMode
from agent.sandbox import BubblewrapBackend, sandbox_environment, sandbox_workspace_root


LOG = logging.getLogger(__name__)
PROBE_TIMEOUT_SECONDS = 5.0
_UNKNOWN = "unknown"
_OS_NAME = re.compile(r"^[\w .()+/-]{1,80}$")
_TOKEN = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
_USER = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_NUMBER = re.compile(r"^[0-9]{1,12}$")
_MAX_LISTED_MOUNTS = 8

PROBE_SCRIPT = r"""
os_name=unknown
if [ -r /etc/os-release ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in
      PRETTY_NAME=*)
        os_name=${line#PRETTY_NAME=}
        os_name=${os_name#\"}
        os_name=${os_name%\"}
        ;;
    esac
  done < /etc/os-release
fi
[ -n "$os_name" ] || os_name=unknown
kernel=$(uname -r 2>/dev/null) || kernel=unknown
[ -n "$kernel" ] || kernel=unknown
arch=$(uname -m 2>/dev/null) || arch=unknown
[ -n "$arch" ] || arch=unknown
user=$(id -un 2>/dev/null) || user=unknown
[ -n "$user" ] || user=unknown
uid=$(id -u 2>/dev/null) || uid=unknown
[ -n "$uid" ] || uid=unknown
gid=$(id -g 2>/dev/null) || gid=unknown
[ -n "$gid" ] || gid=unknown
if [ "$uid" = 0 ]; then
  root=true
elif [ "$uid" = unknown ]; then
  root=unknown
else
  root=false
fi
if [ -x /bin/sh ]; then
  shell=/bin/sh
else
  shell=unknown
fi
printf '%s\n' \
  "os=$os_name" \
  "kernel=$kernel" \
  "arch=$arch" \
  "user=$user" \
  "uid=$uid" \
  "gid=$gid" \
  "root=$root" \
  "shell=$shell"
"""


def classify_runtime(
    *,
    environ: Mapping[str, str],
    dockerenv: bool,
    cgroup: str,
    osrelease: str,
) -> str:
    """Host-process runtime: container, WSL2, or native Linux."""
    lowered = cgroup.lower()
    if dockerenv or any(token in lowered for token in ("docker", "containerd", "kubepods", "libpod")):
        return "container"
    if environ.get("WSL_DISTRO_NAME") or environ.get("WSL_INTEROP") or "microsoft" in osrelease.lower():
        return "WSL2"
    return "Linux"


def detect_runtime_environment() -> str:
    return classify_runtime(
        environ=os.environ,
        dockerenv=Path("/.dockerenv").exists(),
        cgroup=_read_text("/proc/1/cgroup"),
        osrelease=_read_text("/proc/sys/kernel/osrelease"),
    )


@dataclass(frozen=True)
class EnvironmentFingerprint:
    mode: str
    workspace: str
    home: str
    extra_mounts: tuple[tuple[str, str, str], ...]


def environment_fingerprint(config: SandboxConfig) -> EnvironmentFingerprint:
    env = sandbox_environment(config)
    mounts = tuple(
        (str(mount.source), mount.destination, "ro") for mount in config.extra_read_only_mounts
    ) + tuple(
        (str(mount.source), mount.destination, "rw") for mount in config.extra_read_write_mounts
    )
    return EnvironmentFingerprint(
        mode=config.mode,
        workspace=sandbox_workspace_root(config.workspace),
        home=env.get("HOME", _UNKNOWN),
        extra_mounts=mounts,
    )


@dataclass(frozen=True)
class EnvironmentSnapshot:
    os_name: str
    kernel: str
    runtime: str
    architecture: str
    user: str
    uid: str
    gid: str
    root: str
    workspace: str
    git_repository: str
    sandbox_mode: str
    home: str
    home_storage: str
    tmp: str
    fs_system: str
    extra_mounts: str
    agent_config: str
    agent_install: str
    shell: str
    permission: str
    network: str


@dataclass
class EnvironmentCache:
    """Per-thread snapshots. Permission edits do not probe again."""

    snapshot: EnvironmentSnapshot | None = None
    _entries: dict[str, tuple[EnvironmentFingerprint, EnvironmentSnapshot]] = field(default_factory=dict)

    def ensure(
        self,
        thread_id: str,
        config: SandboxConfig,
        permission: PermissionMode,
        *,
        probe: Callable[[], str | None],
        runtime: str | None = None,
    ) -> EnvironmentSnapshot:
        fingerprint = environment_fingerprint(config)
        cached = self._entries.get(thread_id)
        if cached is not None and cached[0] == fingerprint:
            updated = with_permission(cached[1], permission)
            self._entries[thread_id] = (fingerprint, updated)
            self.snapshot = updated
            return updated
        try:
            output = probe()
        except Exception:  # noqa: BLE001
            LOG.warning("sandbox environment probe failed", exc_info=True)
            output = None
        snapshot = collect_environment(config, permission, probe_output=output, runtime=runtime)
        self._entries[thread_id] = (fingerprint, snapshot)
        self.snapshot = snapshot
        return snapshot

    def invalidate(self, thread_id: str) -> None:
        """Drop a restored thread's snapshot so the next call rechecks the sandbox."""
        self._entries.pop(thread_id, None)
        self.snapshot = None


def with_permission(snapshot: EnvironmentSnapshot, permission: PermissionMode) -> EnvironmentSnapshot:
    return replace(snapshot, permission=permission.value, network=network_rule(permission))


def network_rule(permission: PermissionMode) -> str:
    return "host-network" if permission is PermissionMode.ALLOW else "per-command"


def collect_environment(
    config: SandboxConfig,
    permission: PermissionMode,
    *,
    probe_output: str | None,
    runtime: str | None = None,
) -> EnvironmentSnapshot:
    """Build a snapshot. A missing probe marks probed fields unknown."""
    probed = parse_probe(probe_output)
    env = sandbox_environment(config)
    home = env.get("HOME", _UNKNOWN)
    restricted = config.mode == "restricted"
    return EnvironmentSnapshot(
        os_name=probed["os"],
        kernel=probed["kernel"],
        runtime=runtime if runtime is not None else detect_runtime_environment(),
        architecture=probed["arch"],
        user=probed["user"],
        uid=probed["uid"],
        gid=probed["gid"],
        root=probed["root"],
        workspace=sandbox_workspace_root(config.workspace),
        git_repository=workspace_git_repository(config.workspace),
        sandbox_mode=config.mode,
        home=home,
        home_storage=home_storage_for(config, home),
        tmp=env.get("TMPDIR", "/tmp"),
        fs_system="read-only" if restricted else "host-permissions",
        extra_mounts=extra_mounts_summary(config),
        agent_config="not-mounted" if restricted else "hidden",
        agent_install="" if restricted else "read-only",
        shell=probed["shell"],
        permission=permission.value,
        network=network_rule(permission),
    )


def render_environment_context(snapshot: EnvironmentSnapshot) -> str:
    """Short execute-view text. File tools do not share this view."""
    install = f" agent_install={_field(snapshot.agent_install)}" if snapshot.agent_install else ""
    return "\n".join([
        "Environment",
        "execute 视图，read_file 不共享。已给出的字段不要再查。",
        (
            f"os={_field(snapshot.os_name)} kernel={_field(snapshot.kernel)} "
            f"runtime={_field(snapshot.runtime)} arch={_field(snapshot.architecture)} "
            f"user={_field(snapshot.user)} uid={_field(snapshot.uid)} gid={_field(snapshot.gid)} "
            f"root={_field(snapshot.root)}"
        ),
        (
            f"workspace={_field(snapshot.workspace)} git_repo={_field(snapshot.git_repository)} "
            f"shell={_field(snapshot.shell)}"
        ),
        (
            f"sandbox={_field(snapshot.sandbox_mode)} home={_field(snapshot.home)} "
            f"home_storage={_field(snapshot.home_storage)} tmp={_field(snapshot.tmp)} "
            f"system={_field(snapshot.fs_system)} extra_mounts={_field(snapshot.extra_mounts)} "
            f"agent_config={_field(snapshot.agent_config)}{install}"
        ),
        f"permission={_field(snapshot.permission)} network={_field(snapshot.network)}",
    ])


def parse_probe(output: str | None) -> dict[str, str]:
    found: dict[str, str] = {}
    if output:
        for line in output.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            found[key] = value.strip()
    return {
        "os": _match(_OS_NAME, found.get("os", "")),
        "kernel": _match(_TOKEN, found.get("kernel", "")),
        "arch": _match(_TOKEN, found.get("arch", "")),
        "user": _match(_USER, found.get("user", "")),
        "uid": _match(_NUMBER, found.get("uid", "")),
        "gid": _match(_NUMBER, found.get("gid", "")),
        "root": _choice(found.get("root", ""), {"true", "false"}),
        "shell": "/bin/sh" if found.get("shell") == "/bin/sh" else _UNKNOWN,
    }


def probe_sandbox(backend: Any) -> str | None:
    """One offline bwrap with the same mounts as execute. Never raises."""
    try:
        argv = backend.command_args(PROBE_SCRIPT, network=False)
        completed = subprocess.run(
            argv,
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if completed.returncode != 0 and not completed.stdout:
        return None
    return completed.stdout.decode("utf-8", "replace")[:4096]


def bubblewrap_backend(backend: object) -> BubblewrapBackend | None:
    executor = getattr(backend, "executor", None)
    if isinstance(executor, BubblewrapBackend):
        return executor
    if isinstance(backend, BubblewrapBackend):
        return backend
    return None


def extra_mounts_summary(config: SandboxConfig) -> str:
    """Sandbox destinations and access. Host source paths stay out of the prompt."""
    labels = [
        _mount_label(mount.destination, "ro") for mount in config.extra_read_only_mounts
    ] + [
        _mount_label(mount.destination, "rw") for mount in config.extra_read_write_mounts
    ]
    if not labels:
        return "none"
    shown = labels[:_MAX_LISTED_MOUNTS]
    hidden = len(labels) - len(shown)
    text = ",".join(shown)
    if hidden:
        text = f"{text},+{hidden}"
    return text


def _mount_label(destination: str, access: str) -> str:
    dest = "".join(destination.split()).replace(",", "")
    return f"{dest}:{access}" if dest else _UNKNOWN


def workspace_git_repository(workspace: Path) -> str:
    try:
        return "true" if (workspace.expanduser().resolve() / ".git").exists() else "false"
    except OSError:
        return _UNKNOWN


def home_storage_for(config: SandboxConfig, home: str) -> str:
    if config.mode == "restricted" and home == "/home/agent":
        return "persistent"
    if config.mode == "full-access" and home == str(Path.home()):
        return "persistent"
    return _UNKNOWN


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _match(pattern: re.Pattern[str], value: str) -> str:
    return value if pattern.fullmatch(value) else _UNKNOWN


def _choice(value: str, allowed: set[str]) -> str:
    return value if value in allowed else _UNKNOWN


def _field(value: str) -> str:
    text = " ".join(value.split())
    return text or _UNKNOWN


_active_cache: ContextVar[EnvironmentCache | None] = ContextVar("environment_cache", default=None)


def set_environment_cache(cache: EnvironmentCache | None) -> Token[EnvironmentCache | None]:
    return _active_cache.set(cache)


def reset_environment_cache(token: Token[EnvironmentCache | None]) -> None:
    _active_cache.reset(token)


class EnvironmentContextMiddleware(AgentMiddleware):
    """Attach the current sandbox snapshot to the fixed system prompt on each model call."""

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(self._with_snapshot(request))

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return await handler(self._with_snapshot(request))

    def _with_snapshot(self, request: Any) -> Any:
        cache = _active_cache.get()
        snapshot = None if cache is None else cache.snapshot
        if snapshot is None:
            return request
        text = render_environment_context(snapshot)
        original = request.system_message
        if original is None:
            system_message = SystemMessage(content=text)
        elif isinstance(original.content, str):
            system_message = original.model_copy(update={"content": f"{original.content}\n\n{text}"})
        else:
            system_message = original.model_copy(update={
                "content": [*original.content, {"type": "text", "text": text}],
            })
        return request.override(system_message=system_message)
