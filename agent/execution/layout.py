"""Sandbox path and mount layout construction."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
import os
from pathlib import Path, PurePosixPath
import re
import stat
import shutil
import sysconfig

from agent.execution.policy import sandbox_workspace_root
from agent.execution.types import SANDBOX_ROOT, SKILLS_ROOT
from agent.execution.policy import default_skills_dir
from agent.execution.policy import SAFE_INHERITED_ENV, BindMount, SandboxConfig
from agent.execution.mounts import effective_user_mounts


WORKER_SANDBOX_PATH = "/run/deep-agent/sandbox_worker.py"
LOG = logging.getLogger(__name__)
FIXED_PATH = "/run/deep-agent:/usr/local/bin:/usr/bin:/bin"


RUNTIME_PATHS = ("/usr", "/bin", "/lib", "/lib64")


RUNTIME_FILES = (
    "/etc/alternatives",
    "/etc/group",
    "/etc/ld.so.cache",
    "/etc/localtime",
    "/etc/nsswitch.conf",
    "/etc/os-release",
    "/etc/passwd",
)


NETWORK_FILES = ("/etc/hosts", "/etc/resolv.conf", "/etc/ssl")


ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


SEARCH_BINARY_PATH = "/run/deep-agent/rg"


CONFIG_DENIED = "Permission denied: agent configuration is not accessible"


INSTALL_READ_ONLY = "Permission denied: agent installation is read-only"


def _component_within(path: str, root: str) -> bool:
    """Match ``path`` to ``root`` on a path-component boundary.

    ``/workspace2`` is not inside ``/workspace``.
    """
    normalized = path.rstrip("/") or "/"
    base = root.rstrip("/") or "/"
    return normalized == base or normalized.startswith(base + "/")


def sandbox_config_dir(config: SandboxConfig) -> Path:
    """Directory that holds agent configuration, sessions, and the persistent sandbox home."""
    raw = config.protected_config_dir or (Path.home() / ".deep-agent")
    return raw.expanduser().resolve()


def sandbox_state_dirs(config: SandboxConfig) -> tuple[Path, Path]:
    """Host directories mounted at ``/home`` and ``/tmp`` in restricted mode."""
    root = sandbox_config_dir(config) / "sandbox"
    return root / "home", root / "tmp"


def ensure_sandbox_state_dirs(config: SandboxConfig) -> tuple[Path, Path]:
    home, tmp = sandbox_state_dirs(config)
    for path in (home, tmp):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return home, tmp


def agent_install_paths() -> tuple[Path, ...]:
    """The agent package and its console script, without the rest of site-packages."""
    package = Path(__file__).resolve().parents[1]
    paths: list[Path] = []
    if package.is_dir():
        paths.append(package)
    entry = Path(sysconfig.get_path("scripts")) / "deep-agent"
    if entry.exists() and not entry.is_dir():
        resolved = entry.resolve()
        if not any(resolved == path or _is_within(path, resolved) for path in paths):
            paths.append(resolved)
        if entry.is_symlink() and not any(_same_path(entry, path) for path in paths):
            paths.append(entry)
    return tuple(paths)


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().absolute() == right.expanduser().absolute()


def _is_within(root: Path, path: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _path_inside_config(path: Path, config_dir: Path, skills: Path | None) -> bool:
    if not _is_within(config_dir, path):
        return False
    return skills is None or not _is_within(skills, path)


@dataclass(frozen=True)
class _MountPoint:
    """One host path exposed at a fixed virtual destination."""

    destination: PurePosixPath
    source: Path
    read_only: bool
    is_file: bool

    @property
    def virtual(self) -> str:
        return self.destination.as_posix()


def _mount_points(config: SandboxConfig, skills_dir: Path | None) -> tuple[_MountPoint, ...]:
    """Resolve the file-tool mount stack and reject layouts file tools cannot honor.

    The workspace, extra mounts, and skills are applied in that order, matching
    ``BubblewrapBackend.sandbox_args`` after the runtime directories. A later
    mount hides an earlier mount at the same path or at a descendant.
    A nested mount is expressible only when its source is exactly the
    corresponding path inside the parent mount. The same host directory may
    also be mounted at several paths with different permissions; file tools
    then judge read-only versus read-write by the path the caller used.
    """
    read_only, read_write = effective_user_mounts(config)
    ordered: list[tuple[Path, str, bool]] = [(config.workspace, sandbox_workspace_root(config.workspace), False)]
    ordered.extend((mount.source, mount.destination, True) for mount in read_only)
    ordered.extend((mount.source, mount.destination, False) for mount in read_write)
    if skills_dir is not None:
        ordered.append((skills_dir, SKILLS_ROOT, False))

    effective: list[_MountPoint] = []
    for raw_source, raw_destination, read_only in ordered:
        source = raw_source.expanduser().resolve()
        destination = PurePosixPath(PurePosixPath(raw_destination).as_posix())
        if not destination.is_absolute() or ".." in destination.parts:
            raise ValueError(f"mount destination must be an absolute sandbox path: {raw_destination!r}")
        if not source.exists():
            raise ValueError(f"mount source does not exist: {source}")
        is_file = source.is_file() or _is_device_node(source)
        if not is_file and not source.is_dir():
            raise ValueError(f"mount source must be a file, directory, or device node: {destination}")
        point = _MountPoint(destination, source, read_only, is_file)
        for existing in effective:
            if existing.destination == destination:
                raise ValueError(f"duplicate mount destination: {destination}")
            if existing.destination.is_relative_to(destination):
                raise ValueError(
                    f"mount {existing.destination} is hidden by later mount {destination}; "
                    "file tools cannot preserve the declared permissions"
                )
        parents = [
            item for item in effective
            if destination.is_relative_to(item.destination) and destination != item.destination
        ]
        if parents:
            parent = max(parents, key=lambda item: len(item.destination.parts))
            if parent.is_file:
                raise ValueError(f"cannot mount {destination} inside file mount {parent.destination}")
            expected = (parent.source / destination.relative_to(parent.destination)).resolve()
            if source != expected:
                raise ValueError(
                    f"nested mount {destination} does not match the contents of {parent.destination}; "
                    "refusing a file-tool route that would disagree with the sandbox mount"
                )
        effective.append(point)
    return tuple(effective)


def sandbox_environment(config: SandboxConfig, environ: dict[str, str] | None = None) -> dict[str, str]:
    from agent.models.http import apply_http_environment

    source = os.environ if environ is None else environ
    result = {
        "PATH": FIXED_PATH,
        "HOME": str(Path.home()) if config.mode == "full-access" else "/home/agent",
        "PWD": sandbox_workspace_root(config.workspace),
        "TMPDIR": "/tmp",
    }
    for name in SAFE_INHERITED_ENV:
        if name in source:
            result[name] = source[name]
    for name, value in source.items():
        if name.startswith("LC_"):
            result[name] = value
    for name in config.env_allowlist:
        if name in source:
            result[name] = source[name]
    result.update(config.env_set)
    result = apply_http_environment(result, source, trust_env=config.trust_env)
    # Keep built-in executables available even when configuration replaces PATH.
    tools_path = str(PurePosixPath(SEARCH_BINARY_PATH).parent)
    configured_paths = [part for part in result["PATH"].split(":") if part != tools_path]
    result["PATH"] = ":".join([tools_path, *configured_paths])
    return result


def _validated_skills_dir(workspace: Path) -> Path | None:
    source = default_skills_dir()
    if source.is_symlink():
        raise ValueError(f"skills directory must not be a symlink: {source}")
    if source.exists() and not source.is_dir():
        raise ValueError(f"skills directory must be a directory: {source}")
    parent = source.parent
    resolved_parent = parent.expanduser().resolve() if parent.exists() else parent.expanduser()
    resolved = resolved_parent / source.name
    if resolved == workspace or _is_within(workspace, resolved):
        raise ValueError(f"skills directory must be outside workspace: {source}")
    if not source.exists():
        source.mkdir(parents=True, exist_ok=True, mode=0o700)
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"skills directory must be a real directory: {source}")
    return source.resolve()


def _mark_missing_mount(path: str, created: set[str]) -> None:
    """Record a sandbox path that does not exist on the host, including missing parents."""
    current = PurePosixPath(path)
    if current.as_posix() in {"", "/"}:
        return
    built = PurePosixPath("/")
    for part in current.parts[1:]:
        built = built / part
        text = built.as_posix()
        if text in created or Path(text).exists():
            continue
        created.add(text)


def _full_access_created_dirs(
    config: SandboxConfig, workspace_root: str, skills_dir: Path | None,
) -> set[str]:
    """Directories Bubblewrap must create because they are not already on the host.

    Parents of these paths stay on the sandbox tmpfs. Binding that parent from
    the host would make ``mkdir`` run on a root-owned directory such as ``/run``
    or ``/opt``.
    """
    created: set[str] = set()
    _mark_missing_mount(workspace_root, created)
    if skills_dir is not None:
        _mark_missing_mount(SKILLS_ROOT, created)
    created.add("/run/deep-agent")
    read_only, read_write = effective_user_mounts(config)
    for mount in (*read_only, *read_write):
        destination = str(PurePosixPath(mount.destination))
        if Path(destination).exists():
            continue
        source = mount.source.expanduser()
        if source.is_dir():
            _mark_missing_mount(destination, created)
        else:
            parent = str(PurePosixPath(destination).parent)
            if parent != "/":
                created.add(parent)
                _mark_missing_mount(parent, created)
    return created


def _contains_created(dest: str, created: set[str]) -> bool:
    prefix = dest.rstrip("/") or "/"
    return any(path == prefix or path.startswith(prefix + "/") for path in created)


def _host_root_mirror_args(workspace_root: str, created: set[str]) -> list[str]:
    """Expose the host root without binding ``/`` onto ``/``.

    ``--bind / /`` makes the sandbox root the host root. Bubblewrap then
    tries to create sandbox-only paths such as ``/skills`` and
    ``/run/deep-agent`` on that real root, which a normal user cannot do.
    The empty tmpfs root can hold those new directories. Host directories
    that must contain one of them are recreated there, and their existing
    children are mounted individually. ``/proc`` is left for the sandbox's
    own instance. ``/dev`` is skipped here so ``sandbox_args`` can attach a
    private tmpfs, which extra ``dev_bind`` mounts may overlay. The
    workspace itself is its real absolute path and is not created as a new
    top-level name.
    """
    del workspace_root
    try:
        return _mirror_host_directory(Path("/"), "/", created)
    except OSError:
        args: list[str] = []
        for path in sorted(created):
            if path.count("/") == 1:
                args += ["--dir", path]
        return args


def _mirror_host_directory(host: Path, dest: str, created: set[str]) -> list[str]:
    dest = dest.rstrip("/") or "/"
    if dest != "/" and not _contains_created(dest, created):
        if host.is_symlink():
            return ["--symlink", os.readlink(host), dest]
        return ["--bind", str(host), dest]
    args: list[str] = []
    if dest != "/":
        args += ["--dir", dest]
    target = host.resolve() if host.is_symlink() else host
    if not target.is_dir():
        return args
    names: set[str] = set()
    try:
        entries = sorted(os.scandir(target), key=lambda item: item.name)
    except OSError:
        entries = []
    for entry in entries:
        if dest == "/" and entry.name in {"proc", "dev"}:
            continue
        names.add(entry.name)
        child_dest = f"/{entry.name}" if dest == "/" else f"{dest}/{entry.name}"
        try:
            args += _mirror_host_directory(Path(entry.path), child_dest, created)
        except OSError:
            continue
    for path in sorted(created):
        parent = str(PurePosixPath(path).parent)
        if parent != dest or PurePosixPath(path).name in names:
            continue
        args += ["--dir", path]
    if dest == "/":
        host_names = names
        for path in sorted(created):
            top = path.strip("/").split("/", 1)[0]
            if top in host_names:
                continue
            args += ["--dir", path]
    return args


def _full_access_protection_args(config: SandboxConfig, skills_dir: Path | None) -> list[str]:
    """Mask configuration and remount the agent install read-only. Applied last."""
    del skills_dir  # skills stay reachable at /skills; the config directory mask hides the host path
    ensure_sandbox_state_dirs(config)
    args: list[str] = []
    for path in agent_install_paths():
        if path.is_symlink():
            args += ["--ro-bind", str(path.resolve()), str(path)]
        elif path.exists():
            args += ["--ro-bind", str(path), str(path)]
    config_dir = sandbox_config_dir(config)
    masked: list[Path] = []
    if config_dir.is_dir():
        masked.append(config_dir)
        real = config_dir.resolve()
        if real != config_dir and real.is_dir():
            masked.append(real)
    for path in masked:
        args += ["--perms", "000", "--tmpfs", str(path)]
    for path in config.protected_files:
        if not path.exists() or any(_is_within(root, path) for root in masked):
            continue
        if path.is_dir():
            args += ["--perms", "000", "--tmpfs", str(path)]
            continue
        mask = ensure_sandbox_state_dirs(config)[0].parent / "protected-file"
        if not mask.exists():
            mask.touch(mode=0o000)
        else:
            mask.chmod(0o000)
        args += ["--ro-bind", str(mask), str(path)]
    return args


def _validate_full_access(config: SandboxConfig, workspace: Path, skills: Path | None) -> None:
    config_dir = sandbox_config_dir(config)
    if config_dir == Path("/") or config_dir == workspace or _is_within(config_dir, workspace):
        raise ValueError("full-access cannot hide a configuration directory that contains the workspace")
    installs = agent_install_paths()
    for path in installs:
        if path == Path("/") or path == workspace or _is_within(path, workspace):
            raise ValueError("full-access cannot protect an install path that contains the workspace")
    read_only, read_write = effective_user_mounts(config)
    for mount in (*read_only, *read_write):
        source = mount.source.expanduser().resolve()
        destination = PurePosixPath(mount.destination)
        writable = mount in read_write
        if _path_inside_config(source, config_dir, skills):
            raise ValueError(f"extra mount cannot expose the agent configuration directory: {source}")
        if any(destination == PurePosixPath(path.as_posix()) for path in (config_dir, *config.protected_files)):
            raise ValueError(f"extra mount cannot uncover a protected path: {destination}")
        if not writable:
            continue
        for install in installs:
            install_path = PurePosixPath(install.as_posix())
            if (
                source == install or _is_within(install, source) or _is_within(source, install)
                or destination == install_path or install_path.is_relative_to(destination)
                or destination.is_relative_to(install_path)
            ):
                raise ValueError(f"extra mount cannot make the agent installation writable: {destination}")


def _validated_workspace(workspace: Path) -> Path:
    resolved = workspace.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"sandbox workspace must be an existing directory: {resolved}")
    return resolved


def _validate_config(config: SandboxConfig) -> Path:
    workspace = _validated_workspace(config.workspace)
    if config.timeout_seconds is not None and config.timeout_seconds <= 0:
        raise ValueError(f"timeout_seconds must be positive, got {config.timeout_seconds}")
    if config.max_output_bytes <= 0:
        raise ValueError(f"max_output_bytes must be positive, got {config.max_output_bytes}")
    skills = _validated_skills_dir(workspace)
    home, tmp = sandbox_state_dirs(config)
    if any(path == workspace or _is_within(workspace, path) for path in (home, tmp, sandbox_config_dir(config))):
        raise ValueError("sandbox state directories must be outside the workspace")
    if config.mode == "full-access":
        _validate_full_access(config, workspace, skills)
    read_only, read_write = effective_user_mounts(config)
    for mount in read_only:
        _mount_args("--ro-bind", mount, workspace_root=sandbox_workspace_root(workspace))
    for mount in read_write:
        _mount_args("--bind", mount, workspace_root=sandbox_workspace_root(workspace))
    _mount_points(replace(config, workspace=workspace), skills)
    for name in (*config.env_allowlist, *config.env_set):
        if not ENV_NAME.fullmatch(name):
            raise ValueError(f"invalid environment variable name: {name!r}")
    return workspace


def _sandbox_python() -> str | None:
    """解析到只读运行时挂载内的 python3。"""
    for directory in FIXED_PATH.split(":"):
        candidate = Path(directory) / "python3"
        if not (candidate.is_file() and os.access(candidate, os.X_OK)):
            continue
        resolved = candidate.resolve()
        if any(resolved.is_relative_to(root) for root in RUNTIME_PATHS):
            return str(candidate)
    return None


def _resolve_executable(value: str) -> str | None:
    if not value.strip():
        raise ValueError("bwrap_path cannot be empty")
    if "/" in value:
        candidate = Path(value).expanduser().resolve()
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    return shutil.which(value)


def _mount_args(flag: str, mount: BindMount, *, workspace_root: str = SANDBOX_ROOT) -> list[str]:
    source = mount.source.expanduser().resolve()
    destination = PurePosixPath(mount.destination)
    if not source.exists():
        raise ValueError(f"mount source does not exist: {source}")
    if not destination.is_absolute() or ".." in destination.parts:
        raise ValueError(f"mount destination must be an absolute sandbox path: {mount.destination!r}")
    protected = {PurePosixPath(workspace_root), PurePosixPath("/tmp"), PurePosixPath("/home/agent")}
    if destination in protected:
        raise ValueError(f"extra mount cannot replace protected destination: {destination}")
    worker_dir = PurePosixPath(WORKER_SANDBOX_PATH).parent
    if destination == worker_dir or worker_dir in destination.parents or destination in worker_dir.parents:
        raise ValueError(f"extra mount conflicts with the sandbox worker: {destination}")
    if destination == PurePosixPath(SKILLS_ROOT) or PurePosixPath(SKILLS_ROOT) in destination.parents or destination in PurePosixPath(SKILLS_ROOT).parents:
        raise ValueError(f"extra mount conflicts with skills destination: {destination}")
    if mount.dev_bind:
        if flag == "--ro-bind":
            raise ValueError("dev_bind cannot be used on extra_read_only_mounts")
        flag = "--dev-bind"
    elif _is_device_node(source):
        raise ValueError(f"device node {source} requires extra_read_write_mounts with dev_bind: true")
    return [flag, str(source), str(destination)]


def _is_device_node(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return stat.S_ISCHR(mode) or stat.S_ISBLK(mode)
