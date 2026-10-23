from __future__ import annotations

import asyncio
from codecs import IncrementalDecoder, getincrementaldecoder
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
import logging
import os
from pathlib import Path, PurePosixPath
import re
import stat
import selectors
import shutil
import signal
import subprocess
import sysconfig
import time
import uuid

from agent.cancel import emit_tool_output, get_cancel_context

from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.backends.utils import InvalidGlobPatternError, compile_grep_include_glob
from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
)

from agent.config import SAFE_INHERITED_ENV, BindMount, SandboxConfig, SandboxMode, default_skills_dir
from agent.sandbox_pool import PersistentSandbox, SandboxLostError, SandboxPool, SandboxStartError


LOG = logging.getLogger(__name__)
SANDBOX_ROOT = "/workspace"
SKILLS_ROOT = "/skills"
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
WORKER_SOURCE_PATH = Path(__file__).with_name("sandbox_worker.py")
WORKER_SANDBOX_PATH = "/run/deep-agent/sandbox_worker.py"
SEARCH_BINARY_PATH = "/run/deep-agent/rg"
KILL_CONFIRM_SECONDS = 5.0
CONFIG_DENIED = "Permission denied: agent configuration is not accessible"
INSTALL_READ_ONLY = "Permission denied: agent installation is read-only"


class ExecutionMode(StrEnum):
    SANDBOXED = "SANDBOXED"
    CUSTOM = "CUSTOM"


class SandboxUnavailableError(RuntimeError):
    """沙箱不可用，并带有可机读的诊断类别。"""

    def __init__(self, message: str, *, kind: str = "preflight_failed") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class LocalExecuteResponse(ExecuteResponse):
    """execute 结果，附带 UI ToolMessage artifact 所需的本地元数据。"""

    host_log_path: str | None = None
    agent_log_path: str | None = None
    log_error: str | None = None
    termination_reason: str | None = None
    max_output_bytes: int | None = None


@dataclass(frozen=True)
class BackendSelection:
    backend: SandboxBackendProtocol
    mode: ExecutionMode
    warning: str = ""


_PATH_ESCAPE = "Permission denied: path escapes its authorized mount"
_MOUNT_DELETE = "Permission denied: cannot delete a sandbox mount point"


def _component_within(path: str, root: str) -> bool:
    """Match ``path`` to ``root`` on a path-component boundary.

    ``/workspace2`` is not inside ``/workspace``.
    """
    normalized = path.rstrip("/") or "/"
    base = root.rstrip("/") or "/"
    return normalized == base or normalized.startswith(base + "/")


def sandbox_workspace_root(workspace: Path) -> str:
    """Path of the workspace inside the sandbox: its real absolute path.

    Both restricted and full-access use this path as the working directory.
    A project at ``/mnt/d/test`` is ``/mnt/d/test`` in either mode.
    """
    return workspace.expanduser().resolve().as_posix()


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
    package = Path(__file__).resolve().parent
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


def _permission_denied(directories: tuple[str, ...], files: tuple[str, ...] = ()) -> str:
    roots: list[str] = []
    for root in (*directories, *sorted(set(files) - set(directories))):
        if root not in roots:
            roots.append(root)
    if not roots:
        roots = [SANDBOX_ROOT]
    if len(roots) == 1:
        listing = roots[0]
    elif len(roots) == 2:
        listing = f"{roots[0]} or {roots[1]}"
    else:
        listing = ", ".join(roots[:-1]) + f", or {roots[-1]}"
    return f"Permission denied: paths must be under {listing}"


def _glob_literal_directory(pattern: str) -> str | None:
    """Return the absolute literal directory prefix of a glob, or None if relative.

    ``/`` means the pattern is anchored at the composite root (``/*.py``, ``/**``).
    """
    if not pattern.startswith("/"):
        return None
    literal: list[str] = []
    for part in pattern.split("/")[1:]:
        if part == "" or any(char in part for char in "*?["):
            break
        literal.append(part)
    if not literal:
        return "/"
    return "/" + "/".join(literal)


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


def _host_contains(root: Path, path: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _unfollowed_host_path(backend: BackendProtocol, key: str) -> tuple[Path, Path, bool]:
    """Return the resolved parent, the final path, and whether that path is a symlink.

    The parent is fully resolved, including intermediate symlinks. The final
    component is not followed. ``..`` and a symlink that leaves the mount are
    rejected by the backend's virtual-mode checks.
    """
    resolve = backend._resolve_path
    vpath = key if key.startswith("/") else f"/{key}"
    if ".." in vpath.split("/") or "~" in vpath or "\x00" in vpath:
        raise ValueError("Permission denied: invalid virtual path")
    if vpath in {"/", ""}:
        cwd = backend.cwd
        return cwd, cwd, cwd.is_symlink()
    parent_virtual, name = vpath.rsplit("/", 1)
    if name in {"", ".", ".."}:
        raise ValueError("Permission denied: invalid virtual path")
    parent = resolve(parent_virtual or "/")
    candidate = parent / name
    return parent, candidate, candidate.is_symlink()


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
    ordered: list[tuple[Path, str, bool]] = [(config.workspace, sandbox_workspace_root(config.workspace), False)]
    ordered.extend((mount.source, mount.destination, True) for mount in config.extra_read_only_mounts)
    ordered.extend((mount.source, mount.destination, False) for mount in config.extra_read_write_mounts)
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


class _OutsideWorkspaceBackend(BackendProtocol):
    """默认路由：根聚合返回空结果，明确越界返回 Permission denied。"""

    def __init__(self, directories: tuple[str, ...] = (SANDBOX_ROOT,), files: tuple[str, ...] = ()) -> None:
        self._directories = directories
        self._files = files
        self.denied = _permission_denied(directories, files)

    def _allows(self, path: str) -> bool:
        if any(_component_within(path, root) for root in self._directories):
            return True
        normalized = path.rstrip("/") or "/"
        return normalized in self._files

    def ls(self, path: str) -> LsResult:
        if path == "/":
            return LsResult(entries=[])
        return LsResult(error=self.denied, entries=None)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return ReadResult(error=self.denied)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        if path in (None, "/"):
            return GrepResult(matches=[])
        if self._allows(path):
            return GrepResult(matches=[])
        return GrepResult(error=self.denied)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        if path not in (None, "/"):
            if self._allows(path):
                return GlobResult(matches=[])
            return GlobResult(error=self.denied, matches=None)
        prefix = _glob_literal_directory(pattern)
        if prefix is None or prefix == "/" or self._allows(prefix):
            return GlobResult(matches=[])
        return GlobResult(error=self.denied, matches=None)

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=self.denied)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return EditResult(error=self.denied)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=self.denied)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error=self.denied) for path, _ in files]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [FileDownloadResponse(path=path, error=self.denied) for path in paths]


class _ReadOnlyFilesystemBackend(FilesystemBackend):
    """目录挂载的只读文件后端。写入在接触宿主文件之前被拒绝。"""

    denies_mutation = True

    def __init__(self, root: Path, *, reason: str) -> None:
        super().__init__(root_dir=root, virtual_mode=True, max_file_size_mb=10)
        self.mutation_error = f"Permission denied: {reason}"

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=self.mutation_error)

    def edit(self, file_path: str, old_string: str, new_string: str,
             replace_all: bool = False) -> EditResult:
        return EditResult(error=self.mutation_error)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=self.mutation_error)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error=self.mutation_error) for path, _ in files]


class _ExactFileBackend(BackendProtocol):
    """只暴露一个已挂载的文件，不开放其所在目录的其他条目。"""

    def __init__(self, source: Path, *, read_only: bool) -> None:
        self.source = source
        self.host_dir = source.parent
        self.filename = source.name
        self.cwd = self.host_dir
        self.max_file_size_bytes = 10 * 1024 * 1024
        self.denies_mutation = read_only
        self.mutation_error = "Permission denied: mount is read-only"
        self._files = FilesystemBackend(root_dir=self.host_dir, virtual_mode=True, max_file_size_mb=10)
        self._virtual = f"/{self.filename}"

    def _exact(self, path: str) -> bool:
        normalized = path if path.startswith("/") else f"/{path}"
        return normalized in {"/", self._virtual}

    def _resolve_path(self, key: str) -> Path:
        if not self._exact(key):
            raise ValueError("Permission denied: path is outside the mounted file")
        return self._files._resolve_path(self._virtual)

    def _outside(self, path: str) -> str:
        return "Permission denied: path is outside the mounted file"

    def ls(self, path: str) -> LsResult:
        if not self._exact(path):
            return LsResult(error=self._outside(path), entries=None)
        return LsResult(error=f"Path '{path}': not_a_directory", entries=None)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        if not self._exact(file_path):
            return ReadResult(error=self._outside(file_path))
        return self._files.read(self._virtual, offset=offset, limit=limit)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None, *,
             max_count: int | None = None) -> GrepResult:
        if path not in (None, "/", self._virtual):
            return GrepResult(error=self._outside(path or ""))
        return self._files.grep(pattern, self._virtual, glob, max_count=max_count)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return GlobResult(matches=[])

    def write(self, file_path: str, content: str) -> WriteResult:
        if self.denies_mutation:
            return WriteResult(error=self.mutation_error)
        if not self._exact(file_path):
            return WriteResult(error=self._outside(file_path))
        return self._files.write(self._virtual, content)

    def edit(self, file_path: str, old_string: str, new_string: str,
             replace_all: bool = False) -> EditResult:
        if self.denies_mutation:
            return EditResult(error=self.mutation_error)
        if not self._exact(file_path):
            return EditResult(error=self._outside(file_path))
        return self._files.edit(self._virtual, old_string, new_string, replace_all=replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        if not self._exact(file_path):
            return DeleteResult(error=self._outside(file_path))
        return DeleteResult(error=_MOUNT_DELETE)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        responses: list[FileUploadResponse] = []
        for path, content in files:
            if self.denies_mutation:
                responses.append(FileUploadResponse(path=path, error=self.mutation_error))
                continue
            if not self._exact(path):
                responses.append(FileUploadResponse(path=path, error=self._outside(path)))
                continue
            result = self._files.upload_files([(self._virtual, content)])[0]
            responses.append(FileUploadResponse(path=path, error=result.error))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for path in paths:
            if not self._exact(path):
                responses.append(FileDownloadResponse(path=path, error="permission_denied"))
                continue
            result = self._files.download_files([self._virtual])[0]
            responses.append(FileDownloadResponse(path=path, content=result.content, error=result.error))
        return responses


def _directory_backend(point: _MountPoint, executor: SandboxBackendProtocol) -> BackendProtocol:
    destination = point.virtual
    if destination == sandbox_workspace_root(executor.config.workspace):
        return executor
    if point.read_only:
        return _ReadOnlyFilesystemBackend(point.source, reason="mount is read-only")
    return FilesystemBackend(root_dir=point.source, virtual_mode=True, max_file_size_mb=10)


class _DeniedBackend(BackendProtocol):
    """Hide agent configuration from file tools."""

    denies_mutation = True
    mutation_error = CONFIG_DENIED

    def ls(self, path: str) -> LsResult:
        return LsResult(error=CONFIG_DENIED, entries=None)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return ReadResult(error=CONFIG_DENIED)

    def grep(
        self, pattern: str, path: str | None = None, glob: str | None = None, *,
        max_count: int | None = None,
    ) -> GrepResult:
        return GrepResult(error=CONFIG_DENIED)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return GlobResult(error=CONFIG_DENIED, matches=None)

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=CONFIG_DENIED)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return EditResult(error=CONFIG_DENIED)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=CONFIG_DENIED)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error=CONFIG_DENIED) for path, _content in files]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [FileDownloadResponse(path=path, error=CONFIG_DENIED) for path in paths]


class WorkspaceCompositeBackend(CompositeBackend, SandboxBackendProtocol):
    """按启动时固定的挂载表路由文件工具，执行仍委托给底层 executor。"""

    def __init__(self, executor: SandboxBackendProtocol, *, skills_dir: Path | None) -> None:
        config = getattr(executor, "config", None)
        self.workspace_root = (
            sandbox_workspace_root(config.workspace) if isinstance(config, SandboxConfig) else SANDBOX_ROOT
        )
        self.executor = executor
        self.skills_dir = skills_dir
        self._hidden_roots: tuple[Path, ...] = ()
        self._install_roots: tuple[Path, ...] = ()
        default, routes, points, files, directories = self._route_state()
        super().__init__(
            default=default,
            routes=routes,
            artifacts_root=f"{self.workspace_root}/.deepagents",
        )
        self._remember_routes(points, files, directories, default)

    def apply_sandbox_mode(self, mode: SandboxMode) -> None:
        """Switch filesystem mounts on the existing sandbox and drop its processes."""
        config = getattr(self.executor, "config", None)
        if not isinstance(config, SandboxConfig) or not isinstance(self.executor, BubblewrapBackend):
            raise ValueError("Sandbox mode can only be changed for a Bubblewrap backend")
        updated = replace(config, mode=mode)
        _validate_config(updated)
        self.executor.config = updated
        self.executor.skills_dir = _validated_skills_dir(updated.workspace)
        self.skills_dir = self.executor.skills_dir
        self.executor._env = sandbox_environment(updated)
        self.executor.pool.shutdown("sandbox mode changed")
        default, routes, points, files, directories = self._route_state()
        self.default = default
        self.routes = routes
        self.sorted_routes = sorted(routes.items(), key=lambda item: len(item[0]), reverse=True)
        self._remember_routes(points, files, directories, default)

    def _remember_routes(
        self,
        points: tuple[_MountPoint, ...],
        files: dict[str, _MountPoint],
        directories: list[str],
        default: BackendProtocol,
    ) -> None:
        self._mount_points = points
        self._file_mounts = files
        self._directory_roots = tuple(directories)
        self.exact_file_mounts = frozenset(files)
        self._denied = getattr(default, "denied", CONFIG_DENIED)

    def _route_state(
        self,
    ) -> tuple[BackendProtocol, dict[str, BackendProtocol], tuple[_MountPoint, ...], dict[str, _MountPoint], list[str]]:
        executor = self.executor
        config = getattr(executor, "config", None)
        skills_dir = self.skills_dir
        points = _mount_points(config, skills_dir) if isinstance(config, SandboxConfig) else ()
        expose_mounts = isinstance(executor, BubblewrapBackend)
        full_access = expose_mounts and isinstance(config, SandboxConfig) and config.mode == "full-access"
        routes: dict[str, BackendProtocol] = {}
        directories: list[str] = []
        files: dict[str, _MountPoint] = {}
        active_points = points if expose_mounts else tuple(
            point for point in points if point.virtual in {self.workspace_root, SKILLS_ROOT}
        )
        if expose_mounts:
            for point in active_points:
                if point.is_file:
                    files[point.virtual] = point
                    routes[point.virtual + "/"] = _ExactFileBackend(point.source, read_only=point.read_only)
                    continue
                directories.append(point.virtual)
                routes[point.virtual + "/"] = _directory_backend(point, executor)
        else:
            routes[f"{self.workspace_root}/"] = executor
            directories.append(self.workspace_root)
            if skills_dir is not None:
                routes[f"{SKILLS_ROOT}/"] = FilesystemBackend(
                    root_dir=skills_dir, virtual_mode=True, max_file_size_mb=10,
                )
                directories.append(SKILLS_ROOT)
        self._hidden_roots = ()
        self._install_roots = ()
        if full_access and isinstance(config, SandboxConfig):
            self._install_roots = agent_install_paths()
            hidden = [sandbox_config_dir(config)]
            hidden.extend(path for path in config.protected_files if path.is_dir())
            self._hidden_roots = tuple(dict.fromkeys(hidden))
            for root in self._install_roots:
                if root.is_dir():
                    routes[f"{root.as_posix()}/"] = _ReadOnlyFilesystemBackend(root, reason=INSTALL_READ_ONLY)
                    directories.append(root.as_posix())
                else:
                    routes[f"{root.as_posix()}/"] = _ExactFileBackend(root, read_only=True)
                    files[root.as_posix()] = _MountPoint(
                        PurePosixPath(root.as_posix()), root, True, True,
                    )
            denied = _DeniedBackend()
            for root in self._hidden_roots:
                routes[f"{root.as_posix()}/"] = denied
                directories.append(root.as_posix())
            for path in config.protected_files:
                if path.is_dir() or not path.exists():
                    continue
                routes[f"{path.as_posix()}/"] = denied
                files[path.as_posix()] = _MountPoint(PurePosixPath(path.as_posix()), path, True, True)
            default: BackendProtocol = FilesystemBackend(root_dir=Path("/"), virtual_mode=True, max_file_size_mb=10)
        else:
            default = _OutsideWorkspaceBackend(tuple(directories), tuple(files))
        return default, routes, active_points, files, directories

    @property
    def supports_regex_search(self) -> bool:
        return isinstance(getattr(self.executor, "config", None), SandboxConfig)

    def regex_search_location(self, path: str | None) -> tuple[Path, str, str, int]:
        """Validate a virtual mount path using the routed filesystem's checks.

        Return the original relative path so the worker can reject symlinks
        with descriptor-relative opens rather than trusting a resolved pathname.
        """
        if not self.supports_regex_search:
            raise ValueError("Backend does not support local regex search")
        virtual = path or self.workspace_root
        if not virtual.startswith("/") or ".." in virtual.split("/") or "~" in virtual or "\x00" in virtual:
            raise ValueError("Permission denied: invalid virtual search path")
        for mount, backend in sorted(self.routes.items(), key=lambda item: len(item[0]), reverse=True):
            prefix = mount.rstrip("/") or "/"
            if not _component_within(virtual, prefix):
                continue
            if isinstance(backend, _ExactFileBackend):
                if virtual.rstrip("/") != prefix:
                    raise ValueError("Permission denied: search path escapes its authorized mount")
                parent = PurePosixPath(prefix).parent.as_posix()
                return backend.host_dir, backend.filename, parent, backend.max_file_size_bytes
            relative = virtual[len(prefix):].lstrip("/") if prefix != "/" else virtual.lstrip("/")
            try:
                backend._resolve_path(relative or "/")
            except (ValueError, OSError):
                raise ValueError("Permission denied: search path escapes its authorized mount") from None
            return backend.cwd, relative, prefix, backend.max_file_size_bytes
        raise ValueError(self._denied)

    def _covered_by_directory(self, destination: str) -> bool:
        return any(_component_within(destination, root) and destination.rstrip("/") != root.rstrip("/")
                   for root in self._directory_roots)

    def _file_info(self, point: _MountPoint) -> dict[str, object]:
        info: dict[str, object] = {"path": point.virtual, "is_dir": False}
        try:
            stat_result = point.source.stat()
        except OSError:
            return info
        info["size"] = int(stat_result.st_size)
        info["modified_at"] = datetime.fromtimestamp(stat_result.st_mtime).isoformat()
        return info

    def _present_ls(self, path: str, result: LsResult) -> LsResult:
        if path != "/" or result.error or not result.entries or not self._file_mounts:
            return result
        entries: list[dict[str, object]] = []
        for entry in result.entries:
            route = str(entry.get("path", "")).rstrip("/")
            point = self._file_mounts.get(route)
            if point is None:
                entries.append(dict(entry))
                continue
            if self._covered_by_directory(route):
                continue
            entries.append(self._file_info(point))
        entries.sort(key=lambda item: str(item.get("path", "")))
        return LsResult(entries=entries)  # type: ignore[arg-type]

    def ls(self, path: str) -> LsResult:
        normalized = path.rstrip("/") or "/"
        if self._full_access() and normalized == "/":
            return self._ls_host_root()
        try:
            result = super().ls(path)
        except ValueError:
            return LsResult(error=_PATH_ESCAPE, entries=None)
        return self._present_ls(path, result)

    async def als(self, path: str) -> LsResult:
        return await asyncio.to_thread(self.ls, path)

    def _file_relative(self, destination: str, path: str | None) -> str | None:
        if path in (None, "/"):
            return destination.lstrip("/")
        base = path.rstrip("/") or "/"
        if destination.rstrip("/") == base:
            return PurePosixPath(destination).name
        if _component_within(destination, base) and destination.rstrip("/") != base:
            return destination[len(base):].lstrip("/")
        return None

    def _uncovered_file_matches(self, pattern: str, path: str | None) -> list[dict[str, object]]:
        if not self._file_mounts:
            return []
        try:
            matcher = compile_grep_include_glob(pattern)
        except InvalidGlobPatternError:
            return []
        matches: list[dict[str, object]] = []
        for destination, point in self._file_mounts.items():
            if self._covered_by_directory(destination):
                continue
            relative = self._file_relative(destination, path)
            if relative is None or not matcher(relative):
                continue
            matches.append(self._file_info(point))
        return matches

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        try:
            result = super().glob(pattern, path)
        except ValueError:
            return GlobResult(error=_PATH_ESCAPE, matches=None)
        if result.error:
            return result
        extra = self._uncovered_file_matches(pattern, path)
        if not extra:
            return result
        matches = [*(result.matches or []), *extra]
        matches.sort(key=lambda item: str(item.get("path", "")))
        return GlobResult(
            matches=matches,  # type: ignore[arg-type]
            truncated=result.truncated,
            truncation_reason=result.truncation_reason,
        )

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        return await asyncio.to_thread(self.glob, pattern, path)

    def _route_point(self, file_path: str) -> _MountPoint | None:
        virtual = file_path if file_path.startswith("/") else f"/{file_path}"
        matches = [point for point in self._mount_points if _component_within(virtual, point.virtual)]
        if not matches:
            return None
        return max(matches, key=lambda point: len(point.destination.parts))

    def _is_mount_virtual(self, file_path: str) -> bool:
        virtual = file_path if file_path.startswith("/") else f"/{file_path}"
        normalized = virtual.rstrip("/") or "/"
        return any((point.virtual.rstrip("/") or "/") == normalized for point in self._mount_points)

    def _more_specific_mount(self, route: _MountPoint, host_path: Path) -> _MountPoint | None:
        """Return a nested mount under ``route`` that contains ``host_path``.

        Aliases of the same host directory are not more specific: their
        permission comes from the virtual path that was actually used.
        """
        nested = [
            point for point in self._mount_points
            if point.destination != route.destination
            and point.destination.is_relative_to(route.destination)
            and point.source != route.source
            and point.source.is_relative_to(route.source)
            and _host_contains(point.source, host_path)
        ]
        if not nested:
            return None
        return max(nested, key=lambda point: (len(point.source.parts), len(point.destination.parts)))

    def _full_access(self) -> bool:
        config = getattr(self.executor, "config", None)
        return isinstance(config, SandboxConfig) and config.mode == "full-access"

    def _ls_host_root(self) -> LsResult:
        host = FilesystemBackend(root_dir=Path("/"), virtual_mode=True, max_file_size_mb=10)
        result = host.ls("/")
        if result.error:
            return result
        entries = [dict(entry) for entry in result.entries or []]
        seen = {str(entry.get("path", "")).rstrip("/") or "/" for entry in entries}
        for route in self.routes:
            prefix = route.rstrip("/") or "/"
            if prefix.count("/") != 1 or prefix in seen:
                continue
            if any(_component_within(prefix, root) and prefix != root.rstrip("/") for root in self._hidden_roots_text()):
                continue
            entries.append({"path": prefix, "is_dir": True, "size": 0, "modified_at": ""})
            seen.add(prefix)
        entries.sort(key=lambda item: str(item.get("path", "")))
        return LsResult(entries=entries)  # type: ignore[arg-type]

    def _hidden_roots_text(self) -> tuple[str, ...]:
        return tuple(path.as_posix() for path in self._hidden_roots)

    def _protection_error(self, file_path: str, *, write: bool) -> str | None:
        if not self._full_access():
            return None
        virtual = file_path if file_path.startswith("/") else f"/{file_path}"
        try:
            backend, stripped = self._get_backend_and_key(virtual)
            resolve = getattr(backend, "_resolve_path", None)
            host = resolve(stripped) if resolve is not None else None
        except (ValueError, OSError):
            return None
        if host is None:
            return CONFIG_DENIED if self._virtual_is_hidden(virtual) else None
        skills = self.skills_dir
        if skills is not None and _component_within(virtual, SKILLS_ROOT) and _is_within(skills, host):
            return None
        if self._touches_hidden(host, write=write):
            return CONFIG_DENIED
        if write and self._touches_install(host):
            return INSTALL_READ_ONLY
        return None

    def _virtual_is_hidden(self, virtual: str) -> bool:
        return any(_component_within(virtual, root) for root in self._hidden_roots_text())

    def _touches_hidden(self, host: Path, *, write: bool) -> bool:
        for root in self._hidden_roots:
            if _is_within(root, host):
                return True
            if write and _is_within(host, root) and host != root:
                return True
        config = getattr(self.executor, "config", None)
        if isinstance(config, SandboxConfig):
            for path in config.protected_files:
                if host == path or (write and _is_within(host, path) and host != path):
                    return True
                if path.is_dir() and _is_within(path, host):
                    return True
        return False

    def _touches_install(self, host: Path) -> bool:
        for root in self._install_roots:
            if _is_within(root, host):
                return True
            if _is_within(host, root) and host != root:
                return True
        return False

    def _mutation_plan(self, file_path: str) -> tuple[BackendProtocol, str] | str:
        """Choose the backend that may write ``file_path``, or an error string.

        The host path is always fully resolved, whether or not the final
        component is a symlink. A result that lands in a more specific
        read-only mount nested under the virtual route is rejected before any
        write. A same-directory read-only alias is not such a mount: its
        permission stays with the virtual path that was requested. A symlink
        that leaves the route is rejected instead of being retargeted.
        """
        protected = self._protection_error(file_path, write=True)
        if protected is not None:
            return protected
        return self._mutation_plan_unprotected(file_path)

    def _mutation_plan_unprotected(self, file_path: str) -> tuple[BackendProtocol, str] | str:
        """Choose the backend that may write ``file_path``, or an error string.

        The host path is always fully resolved, whether or not the final
        component is a symlink. A result that lands in a more specific
        read-only mount nested under the virtual route is rejected before any
        write. A same-directory read-only alias is not such a mount: its
        permission stays with the virtual path that was requested. A symlink
        that leaves the route is rejected instead of being retargeted.
        """
        backend, stripped = self._get_backend_and_key(file_path)
        resolve = getattr(backend, "_resolve_path", None)
        if resolve is None:
            if getattr(backend, "denies_mutation", False):
                return getattr(backend, "mutation_error", "Permission denied: mount is read-only")
            return backend, stripped
        try:
            host_path = resolve(stripped)
        except (ValueError, OSError):
            return _PATH_ESCAPE
        route = self._route_point(file_path)
        owner = self._more_specific_mount(route, host_path) if route is not None else None
        if owner is not None and owner.read_only:
            owner_backend, _owner_path = self._get_backend_and_key(owner.virtual)
            return getattr(owner_backend, "mutation_error", "Permission denied: mount is read-only")
        if owner is not None:
            relative = host_path.relative_to(owner.source).as_posix()
            virtual = owner.virtual if relative in {"", "."} else f"{owner.virtual}/{relative}"
            return self._get_backend_and_key(virtual)
        if getattr(backend, "denies_mutation", False):
            return getattr(backend, "mutation_error", "Permission denied: mount is read-only")
        return backend, stripped

    def _deletion_removes_mount_source(self, host_path: Path) -> bool:
        try:
            real = host_path.resolve()
        except OSError:
            return True
        return any(
            point.source == real or point.source.is_relative_to(real)
            for point in self._mount_points
        )

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        protected = self._protection_error(file_path, write=False)
        if protected is not None:
            return ReadResult(error=protected)
        try:
            return super().read(file_path, offset=offset, limit=limit)
        except ValueError:
            return ReadResult(error=_PATH_ESCAPE)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return await asyncio.to_thread(self.read, file_path, offset, limit)

    def write(self, file_path: str, content: str) -> WriteResult:
        plan = self._mutation_plan(file_path)
        if isinstance(plan, str):
            return WriteResult(error=plan)
        backend, stripped = plan
        try:
            result = backend.write(stripped, content)
        except ValueError:
            return WriteResult(error=_PATH_ESCAPE)
        if result.path is not None:
            result.path = file_path
        return result

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return await asyncio.to_thread(self.write, file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        plan = self._mutation_plan(file_path)
        if isinstance(plan, str):
            return EditResult(error=plan)
        backend, stripped = plan
        try:
            result = backend.edit(stripped, old_string, new_string, replace_all=replace_all)
        except ValueError:
            return EditResult(error=_PATH_ESCAPE)
        if result.path is not None:
            result.path = file_path
        return result

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return await asyncio.to_thread(self.edit, file_path, old_string, new_string, replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        """Delete a file without removing a mount point or following a symlink.

        Mount roots, single-file mounts, and any path whose removal would
        unlink a mount source through an alias or a parent directory are
        rejected. The parent directory is resolved first. If that directory
        lies in a read-only submount of the virtual route, nothing is removed.
        A final symlink is unlinked itself and its target is left untouched.
        """
        protected = self._protection_error(file_path, write=True)
        if protected is not None:
            return DeleteResult(error=protected)
        backend, stripped = self._get_backend_and_key(file_path)
        resolve = getattr(backend, "_resolve_path", None)
        if resolve is None:
            if getattr(backend, "denies_mutation", False):
                return DeleteResult(error=getattr(backend, "mutation_error", "Permission denied: mount is read-only"))
            try:
                return backend.delete(stripped)
            except ValueError:
                return DeleteResult(error=_PATH_ESCAPE)
        if self._is_mount_virtual(file_path) or stripped in {"/", ""}:
            return DeleteResult(error=_MOUNT_DELETE)
        try:
            parent, host_path, is_link = _unfollowed_host_path(backend, stripped)
        except (ValueError, OSError):
            return DeleteResult(error=_PATH_ESCAPE)
        route = self._route_point(file_path)
        owner = self._more_specific_mount(route, parent) if route is not None else None
        if owner is not None and owner.read_only:
            owner_backend, _owner_path = self._get_backend_and_key(owner.virtual)
            return DeleteResult(error=getattr(owner_backend, "mutation_error", "Permission denied: mount is read-only"))
        if is_link:
            if getattr(backend, "denies_mutation", False):
                return DeleteResult(error=getattr(backend, "mutation_error", "Permission denied: mount is read-only"))
            try:
                host_path.unlink()
            except OSError:
                return DeleteResult(error=f"Error deleting '{file_path}'")
            return DeleteResult(path=file_path)
        if self._deletion_removes_mount_source(host_path):
            return DeleteResult(error=_MOUNT_DELETE)
        if getattr(backend, "denies_mutation", False):
            return DeleteResult(error=getattr(backend, "mutation_error", "Permission denied: mount is read-only"))
        try:
            result = backend.delete(stripped)
        except ValueError:
            return DeleteResult(error=_PATH_ESCAPE)
        if result.path is not None:
            result.path = file_path
        return result

    async def adelete(self, file_path: str) -> DeleteResult:
        return await asyncio.to_thread(self.delete, file_path)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        results: list[FileUploadResponse | None] = [None] * len(files)
        groups: dict[BackendProtocol, list[tuple[int, str, bytes, str]]] = {}
        for index, (path, content) in enumerate(files):
            plan = self._mutation_plan(path)
            if isinstance(plan, str):
                results[index] = FileUploadResponse(path=path, error=plan)
                continue
            backend, stripped = plan
            groups.setdefault(backend, []).append((index, stripped, content, path))
        for backend, batch in groups.items():
            try:
                responses = backend.upload_files([(stripped, content) for _, stripped, content, _ in batch])
            except ValueError:
                responses = [FileUploadResponse(path=stripped, error=_PATH_ESCAPE) for _, stripped, _, _ in batch]
            for (index, _stripped, _content, original), response in zip(batch, responses, strict=True):
                results[index] = FileUploadResponse(path=original, error=response.error)
        return [item or FileUploadResponse(path=files[index][0], error=self._denied)
                for index, item in enumerate(results)]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse | None] = [None] * len(paths)
        allowed: list[str] = []
        for index, path in enumerate(paths):
            protected = self._protection_error(path, write=False)
            if protected is not None:
                responses[index] = FileDownloadResponse(path=path, error=protected)
            else:
                allowed.append(path)
        if allowed:
            try:
                downloaded = super().download_files(allowed)
            except ValueError:
                downloaded = [FileDownloadResponse(path=path, error=_PATH_ESCAPE) for path in allowed]
            cursor = 0
            for index, path in enumerate(paths):
                if responses[index] is None:
                    responses[index] = downloaded[cursor]
                    cursor += 1
        return [item or FileDownloadResponse(path=paths[index], error=self._denied) for index, item in enumerate(responses)]

    @property
    def id(self) -> str:
        return self.executor.id

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self.executor.execute(command, timeout=timeout)

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return await self.executor.aexecute(command, timeout=timeout)


def sandbox_environment(config: SandboxConfig, environ: dict[str, str] | None = None) -> dict[str, str]:
    from agent.http_environment import apply_http_environment

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


class BubblewrapBackend(FilesystemBackend, SandboxBackendProtocol):
    """文件系统 backend，命令执行由 Bubblewrap 隔离。"""

    def __init__(self, config: SandboxConfig, *, executable: str) -> None:
        workspace = _validate_config(config)
        super().__init__(root_dir=workspace, virtual_mode=True, max_file_size_mb=10)
        self.config = replace(config, workspace=workspace)
        self.workspace_root = sandbox_workspace_root(workspace)
        self.skills_dir = _validated_skills_dir(workspace)
        self.executable = executable
        self._env = sandbox_environment(self.config)
        self._sandbox_id = f"bwrap-{uuid.uuid4().hex[:8]}"
        self.pool = SandboxPool(lambda network: self.worker_args(network))

    @property
    def id(self) -> str:
        return self._sandbox_id

    def command_args(self, command: str, *, network: bool = False) -> list[str]:
        """一次性 bwrap 调用：以 ``command`` 作为沙箱主进程。"""
        if not command or not isinstance(command, str):
            raise ValueError("command must be a non-empty string")
        return [*self.sandbox_args(network=network), "/bin/sh", "-lc", command]

    def worker_args(self, network: bool = False) -> list[str]:
        """bwrap 调用：主进程为常驻命令 Worker。"""
        python = _sandbox_python()
        if python is None:
            raise SandboxStartError(
                "python3 is required inside the sandbox for the persistent command worker, "
                f"but none was found under {FIXED_PATH} resolving into {', '.join(RUNTIME_PATHS)}"
            )
        mount = ["--ro-bind", str(WORKER_SOURCE_PATH), WORKER_SANDBOX_PATH]
        return [*self.sandbox_args(network=network, extra=mount), python, "-I", "-S", WORKER_SANDBOX_PATH]

    def sandbox_args(self, *, network: bool = False, extra: list[str] | None = None) -> list[str]:
        args = [
            self.executable,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
        ]
        if not network:
            args.append("--unshare-net")
        if self.config.mode == "full-access":
            args += _host_root_mirror_args(
                self.workspace_root,
                _full_access_created_dirs(self.config, self.workspace_root, self.skills_dir),
            )
        args += ["--proc", "/proc", "--dev", "/dev"]
        if self.config.mode == "restricted":
            home, tmp = ensure_sandbox_state_dirs(self.config)
            args += [
                "--bind", str(tmp), "/tmp",
                "--bind", str(home), "/home",
                "--dir", "/home/agent",
                "--dir", "/etc",
            ]
        if self.config.mode == "restricted":
            for path in (*RUNTIME_PATHS, *RUNTIME_FILES):
                if Path(path).exists():
                    args += ["--ro-bind", path, path]
            if network:
                for path in NETWORK_FILES:
                    if Path(path).exists():
                        args += ["--ro-bind", path, path]
        # After runtime mounts so a workspace under /usr or /tmp stays reachable
        # at its real path and remains writable.
        args += ["--bind", str(self.config.workspace.expanduser().resolve()), self.workspace_root]
        for mount in self.config.extra_read_only_mounts:
            args += _mount_args("--ro-bind", mount, workspace_root=self.workspace_root)
        for mount in self.config.extra_read_write_mounts:
            flag = "--dev-bind" if mount.dev_bind else "--bind"
            args += _mount_args(flag, mount, workspace_root=self.workspace_root)
        if self.skills_dir is not None:
            args += ["--bind", str(self.skills_dir), SKILLS_ROOT]
        # Shared by persistent workers and one-shot shell commands.
        from agent.tools.grep import resolve_rg

        rg = resolve_rg()
        if rg is not None:
            args += ["--ro-bind", str(Path(rg).resolve()), SEARCH_BINARY_PATH]
        else:
            LOG.warning(
                "ripgrep (rg) is not installed on the host; "
                "the sandbox cannot provide /run/deep-agent/rg and code search is unavailable"
            )
        args += [*(extra or ())]
        if self.config.mode == "full-access":
            args += _full_access_protection_args(self.config, self.skills_dir)
        args += ["--clearenv"]
        for name, value in sorted(self._env.items()):
            args += ["--setenv", name, value]
        args += ["--chdir", self.workspace_root, "--"]
        return args

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if not command or not isinstance(command, str):
            return ExecuteResponse("Error: Command must be a non-empty string.", 1, False)
        from agent.network import get_execute_network

        effective_timeout = _effective_timeout(timeout, self.config.timeout_seconds)
        pool = self.pool
        try:
            return pool.run(get_execute_network(), lambda sandbox: _run_in_sandbox(
                pool, sandbox, command,
                timeout=effective_timeout,
                max_output_bytes=self.config.max_output_bytes,
                workspace=self.config.workspace,
            ))
        except SandboxStartError as exc:
            return LocalExecuteResponse(
                f"Error: sandbox failed to start: {exc}", 1, False, termination_reason="spawn_error",
            )


def select_backend(config: SandboxConfig, *, check: bool = True) -> BackendSelection:
    workspace = _validate_config(config)
    validated = replace(config, workspace=workspace)
    executable = _resolve_executable(validated.bwrap_path)
    kind = "not_installed"
    reason = (
        f"Bubblewrap executable {validated.bwrap_path!r} was not found.\n"
        "Ubuntu / Debian: sudo apt install bubblewrap; then check command -v bwrap.\n"
        "deep-agent sandboxing supports Linux and WSL2; WSL1 and native Windows/macOS are unsupported."
    )
    if executable:
        candidate = BubblewrapBackend(validated, executable=executable)
        if not check:
            return BackendSelection(_workspace_backend(candidate), ExecutionMode.SANDBOXED)
        try:
            probe = candidate.execute(
                "true", timeout=min(5, validated.timeout_seconds) if validated.timeout_seconds else 5,
            )
        finally:
            # 预检不得在第一次真正的 execute 之前留下正在运行的沙箱。
            candidate.pool.shutdown("preflight finished")
        if probe.exit_code == 0:
            return BackendSelection(_workspace_backend(candidate), ExecutionMode.SANDBOXED)
        kind, reason = _preflight_diagnostic(probe.output, probe.exit_code)
    raise SandboxUnavailableError(
        f"{reason}\nSandboxing is required. Install Bubblewrap and make sure {validated.bwrap_path!r} can start.",
        kind=kind,
    )


def _preflight_diagnostic(output: str, exit_code: int | None) -> tuple[str, str]:
    detail = f"bubblewrap preflight failed (exit code {exit_code}): {output}"
    lowered = output.lower()
    restricted = any(token in lowered for token in (
        "operation not permitted", "permission denied", "no permissions to create",
        "unprivileged user namespaces are not enabled",
    ))
    if restricted:
        return "namespace_restricted", (
            f"{detail}\nNamespace permissions are restricted; installed bwrap alone is insufficient.\n"
            "Inspect: sysctl kernel.unprivileged_userns_clone kernel.apparmor_restrict_unprivileged_userns user.max_user_namespaces\n"
            "Inspect AppArmor denials: sudo journalctl -k -g 'apparmor|DENIED|userns'\n"
            "On Ubuntu, inspect sudo aa-status and the bwrap profile under /etc/apparmor.d/. "
            "Use the executable-specific userns profile described in docs/sandbox.md (Ubuntu / WSL2 troubleshooting); "
            "do not globally disable AppArmor. Containers may instead need namespace/seccomp policy changes "
            "by their administrator; WSL1 is unsupported."
        )
    return "preflight_failed", (
        f"{detail}\nCheck sandbox.bwrap_path, workspace/mount paths and permissions, "
        "available /bin/sh, and probe timeout. Run the same bwrap preflight in your terminal "
        "and inspect its output before changing isolation settings."
    )


def sandbox_pool_of(backend: BackendProtocol | None) -> SandboxPool | None:
    """从（可能是组合的）backend 取出持久沙箱池。"""
    executor = getattr(backend, "executor", backend)
    pool = getattr(executor, "pool", None)
    return pool if isinstance(pool, SandboxPool) else None


def _workspace_backend(backend: SandboxBackendProtocol) -> WorkspaceCompositeBackend:
    return WorkspaceCompositeBackend(backend, skills_dir=_validated_skills_dir(backend.config.workspace))


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
    for mount in (*config.extra_read_only_mounts, *config.extra_read_write_mounts):
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
    for mount in (*config.extra_read_only_mounts, *config.extra_read_write_mounts):
        source = mount.source.expanduser().resolve()
        destination = PurePosixPath(mount.destination)
        writable = mount in config.extra_read_write_mounts
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
    for mount in config.extra_read_only_mounts:
        _mount_args("--ro-bind", mount, workspace_root=sandbox_workspace_root(workspace))
    for mount in config.extra_read_write_mounts:
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


def _effective_timeout(requested: int | None, cap: int | None) -> int | None:
    if requested is not None and requested <= 0:
        raise ValueError(f"timeout must be positive, got {requested}")
    if cap is None:
        return requested
    return min(requested, cap) if requested is not None else cap


def _create_exec_log(workspace: Path) -> tuple[int, int, str]:
    """在工作区下创建私有日志，不跟随目录符号链接。"""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(workspace, directory_flags)
    try:
        for part in (".deep-agent", "logs", "exec"):
            try:
                os.mkdir(part, mode=0o700, dir_fd=directory_fd)
            except FileExistsError:
                pass
            child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:12]}.log"
        file_fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory_fd,
        )
        return file_fd, directory_fd, name
    except Exception:
        os.close(directory_fd)
        raise


def _write_all(fd: int, data: bytes | bytearray) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("could not write command output log")
        view = view[written:]


def _drop_partial_character_head(data: bytearray) -> None:
    """丢掉窗口裁剪后留在头部的 UTF-8 续字节。

    ``del data[:-max]`` 可能把多字节字符拦腰切断；头部残留的续字节会在每次
    快照和最终尾巴里都解码成 U+FFFD。
    """
    head = 0
    while head < len(data) and (data[head] & 0xC0) == 0x80:
        head += 1
    del data[:head]


def _decode_tail_snapshot(data: bytes) -> str:
    """解码滚动尾巴，避免把被截断的字符闪成乱码。

    非最终的增量解码会把末尾不完整的多字节字符留在缓冲里，而不是立刻替换成
    U+FFFD；下一块数据补全后，下一次快照就会显示完整字符。
    """
    return getincrementaldecoder("utf-8")(errors="replace").decode(data)


class _OutputCollector:
    """收集一条命令合并后的 stdout/stderr：流式推送、截断并写日志。"""

    def __init__(self, *, tool_call_id: str, max_output_bytes: int, workspace_root: Path) -> None:
        self.tool_call_id = tool_call_id
        self.max_output_bytes = max_output_bytes
        self.workspace_root = workspace_root
        self.decoder: IncrementalDecoder = getincrementaldecoder("utf-8")(errors="replace")
        self.tail_bytes = bytearray()
        self.total_bytes = 0
        self.truncated = False
        self.log_fd: int | None = None
        self.log_directory_fd: int | None = None
        self.log_name: str | None = None
        self.log_error: str | None = None

    def _discard_log(self, exc: OSError) -> None:
        self.log_error = f"{type(exc).__name__}: {exc}"
        if self.log_fd is not None:
            try:
                os.close(self.log_fd)
            except OSError:
                pass
            self.log_fd = None
        if self.log_directory_fd is not None and self.log_name is not None:
            try:
                os.unlink(self.log_name, dir_fd=self.log_directory_fd)
            except OSError:
                pass

    def append(self, raw: bytes) -> None:
        if not raw:
            return
        max_output_bytes = self.max_output_bytes
        remaining = max(0, max_output_bytes - self.total_bytes)
        accepted = raw[:remaining] if remaining > 0 else b""
        if accepted:
            text = self.decoder.decode(accepted)
            if text:
                emit_tool_output(self.tool_call_id, text, stream="merged")
        if not self.truncated and self.total_bytes + len(raw) > max_output_bytes:
            self.truncated = True
            if self.log_fd is None and self.log_error is None:
                try:
                    self.log_fd, self.log_directory_fd, self.log_name = _create_exec_log(self.workspace_root)
                    _write_all(self.log_fd, self.tail_bytes)
                    _write_all(self.log_fd, raw)
                except OSError as exc:
                    self._discard_log(exc)
        elif self.log_fd is not None:
            try:
                _write_all(self.log_fd, raw)
            except OSError as exc:
                self._discard_log(exc)
        self.total_bytes += len(raw)
        self.tail_bytes.extend(raw)
        if len(self.tail_bytes) > max_output_bytes:
            del self.tail_bytes[:-max_output_bytes]
            _drop_partial_character_head(self.tail_bytes)
        if self.truncated:
            # 每个分块都用最新字节替换有界内存视图；完整流继续写入工作区日志。
            current_tail = _decode_tail_snapshot(bytes(self.tail_bytes))
            emit_tool_output(self.tool_call_id, current_tail, stream="tail_snapshot")

    def flush(self) -> None:
        """刷新解码器，处理末尾不完整的多字节序列。"""
        if not self.truncated:
            tail = self.decoder.decode(b"", final=True)
            if tail:
                emit_tool_output(self.tool_call_id, tail, stream="merged")

    def close(self) -> None:
        if self.log_fd is not None:
            try:
                os.fsync(self.log_fd)
                os.close(self.log_fd)
                self.log_fd = None
            except OSError as exc:
                self._discard_log(exc)
        if self.log_directory_fd is not None:
            os.close(self.log_directory_fd)
            self.log_directory_fd = None

    def response(
        self,
        *,
        exit_code: int,
        timeout: int | None,
        cancelled: bool = False,
        timed_out: bool = False,
        failure: str | None = None,
    ) -> LocalExecuteResponse:
        max_output_bytes = self.max_output_bytes
        truncated = self.truncated
        output = bytes(self.tail_bytes).decode("utf-8", errors="replace") if self.tail_bytes else ""
        host_log_path: str | None = None
        agent_log_path: str | None = None
        if truncated:
            detail = f"Output truncated: showing the last {max_output_bytes} bytes."
            if self.log_error is not None:
                detail += f"\nFull output could not be saved: {self.log_error}"
            elif self.log_name is not None:
                relative_log_path = Path(".deep-agent/logs/exec") / self.log_name
                host_log_path = str(self.workspace_root / relative_log_path)
                agent_log_path = f"{sandbox_workspace_root(self.workspace_root)}/{relative_log_path.as_posix()}"
                detail += f"\nFull output saved to: {host_log_path}"
                detail += f"\nAgent path: {agent_log_path}"
            output = f"{output}\n\n[{detail}]"

        result_metadata = {
            "host_log_path": host_log_path,
            "agent_log_path": agent_log_path,
            "log_error": self.log_error,
            "max_output_bytes": max_output_bytes,
        }

        if cancelled:
            return LocalExecuteResponse(
                f"{output.rstrip()}\n\nCancelled by user.".strip(), 130, truncated,
                termination_reason="cancelled", **result_metadata,
            )

        if timed_out:
            return LocalExecuteResponse(
                f"{output.rstrip()}\n\nError: Command timed out after {timeout} seconds.".strip(),
                124, truncated, termination_reason="timeout", **result_metadata,
            )

        if failure is not None:
            return LocalExecuteResponse(
                f"{output.rstrip()}\n\nError: {failure}".strip(), exit_code, truncated,
                termination_reason="sandbox_lost", **result_metadata,
            )

        if not output:
            output = "<no output>"
        if exit_code:
            output = f"{output.rstrip()}\n\nExit code: {exit_code}"
        return LocalExecuteResponse(output, exit_code, truncated, **result_metadata)


def _run_process(
    command: list[str] | str,
    *,
    timeout: int | None,
    max_output_bytes: int,
    workspace: Path | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    shell: bool = False,
) -> ExecuteResponse:
    ctx = get_cancel_context()
    tool_call_id = ctx.tool_call_id if ctx is not None else ""
    try:
        process = subprocess.Popen(  # noqa: S603
            command,
            shell=shell,
            executable="/bin/sh" if shell else None,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001
        return LocalExecuteResponse(
            f"Error executing command ({type(exc).__name__}): {exc}", 1, False,
            termination_reason="spawn_error",
        )

    if ctx is not None:
        ctx.register_process(process)

    deadline = time.monotonic() + timeout if timeout is not None else None
    cancelled = False
    timed_out = False
    collector = _OutputCollector(
        tool_call_id=tool_call_id, max_output_bytes=max_output_bytes,
        workspace_root=(workspace or cwd or Path.cwd()).resolve(),
    )
    post_exit_idle_since: float | None = None

    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")

    try:
        while True:
            if ctx is not None and ctx.cancelled:
                cancelled = True
                _kill_process_group_pid(process.pid)
            elif deadline is not None and not cancelled and time.monotonic() >= deadline:
                timed_out = True
                _kill_process_group_pid(process.pid)
            now = time.monotonic()
            stopping = cancelled or timed_out or process.poll() is not None
            if stopping:
                if not selector.get_map():
                    break
                if post_exit_idle_since is None:
                    post_exit_idle_since = now
                if now - post_exit_idle_since >= 0.1:
                    break
            wait = 0.1
            if deadline is not None and not cancelled and not timed_out:
                wait = max(0.0, min(0.1, deadline - now))
            if post_exit_idle_since is not None:
                wait = min(wait, max(0.0, 0.1 - (now - post_exit_idle_since)))
            if selector.get_map():
                events = selector.select(timeout=wait)
            else:
                time.sleep(wait)
                events = []
            if not events:
                if stopping and not selector.get_map():
                    break
                continue
            for key, _mask in events:
                try:
                    chunk = key.fileobj.read1(4096)  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    chunk = b""
                if chunk:
                    collector.append(chunk)
                    if stopping or process.poll() is not None:
                        post_exit_idle_since = time.monotonic()
                else:
                    try:
                        selector.unregister(key.fileobj)
                    except Exception:  # noqa: BLE001
                        pass
            if (cancelled or timed_out or process.poll() is not None) and not selector.get_map():
                break
        collector.flush()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _kill_process_group_pid(process.pid)
            process.wait(timeout=1)
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
        if ctx is not None:
            ctx.clear_process()
        collector.close()

    return collector.response(
        exit_code=int(process.returncode or 0), timeout=timeout,
        cancelled=cancelled or (ctx is not None and ctx.cancelled), timed_out=timed_out,
    )


def _run_in_sandbox(
    pool: SandboxPool,
    sandbox: PersistentSandbox,
    command: str,
    *,
    timeout: int | None,
    max_output_bytes: int,
    workspace: Path,
) -> ExecuteResponse:
    """在持久沙箱里为一条命令启动新的 shell。

    取消和超时只杀掉该命令的进程组。Worker 若未及时确认 kill，视为已损坏，
    下一次 execute 会换新沙箱；本条命令不会重发。
    """
    ctx = get_cancel_context()
    tool_call_id = ctx.tool_call_id if ctx is not None else ""
    try:
        handle = sandbox.start_command(command)
    except SandboxLostError as exc:
        pool.discard(sandbox, str(exc))
        return LocalExecuteResponse(
            f"Error: sandbox is unavailable ({exc}). The command was not run.", 1, False,
            termination_reason="sandbox_lost",
        )
    if ctx is not None:
        ctx.register_process(handle)

    deadline = time.monotonic() + timeout if timeout is not None else None
    cancelled = False
    timed_out = False
    kill_sent_at: float | None = None
    exit_code = 0
    failure: str | None = None
    spawn_error: str | None = None
    collector = _OutputCollector(
        tool_call_id=tool_call_id, max_output_bytes=max_output_bytes, workspace_root=workspace,
    )
    try:
        while True:
            now = time.monotonic()
            if kill_sent_at is None:
                if ctx is not None and ctx.cancelled:
                    cancelled = True
                elif deadline is not None and now >= deadline:
                    timed_out = True
                if cancelled or timed_out:
                    handle.kill()
                    kill_sent_at = now
            elif now - kill_sent_at >= KILL_CONFIRM_SECONDS:
                pool.discard(sandbox, "sandbox worker did not confirm a command kill")
                break
            wait = 0.1
            if deadline is not None and kill_sent_at is None:
                wait = max(0.0, min(wait, deadline - now))
            event = handle.next_event(wait)
            if event is None:
                continue
            kind, payload = event
            if kind == "out":
                collector.append(payload)
            elif kind == "exit":
                exit_code = int(payload)
                break
            elif kind == "spawn_error":
                spawn_error = str(payload)
                break
            else:
                failure = (
                    f"Sandbox worker stopped while this command was running ({payload}). "
                    "The command was not retried; the next execute starts a fresh sandbox."
                )
                exit_code = 125
                pool.discard(sandbox, str(payload))
                break
        collector.flush()
    finally:
        if ctx is not None:
            ctx.clear_process()
        handle.release()
        collector.close()

    if spawn_error is not None and not (cancelled or timed_out):
        return LocalExecuteResponse(
            f"Error executing command ({spawn_error})", 1, False, termination_reason="spawn_error",
        )
    return collector.response(
        exit_code=exit_code, timeout=timeout,
        cancelled=cancelled or (ctx is not None and ctx.cancelled and not timed_out),
        timed_out=timed_out, failure=failure,
    )


def _kill_process_group_pid(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
