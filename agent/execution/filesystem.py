"""Authorized file routing and atomic publication of access policies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
import logging
from pathlib import Path, PurePosixPath
from typing import cast

from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.backends.utils import InvalidGlobPatternError, compile_grep_include_glob
from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
)

from agent.execution.policy import sandbox_workspace_root
from agent.execution.types import SANDBOX_ROOT, SKILLS_ROOT
from agent.execution.policy import SandboxConfig, SandboxMode
from agent.execution.layout import (
    CONFIG_DENIED,
    INSTALL_READ_ONLY,
    _MountPoint,
    _component_within,
    _is_within,
    _mount_points,
    _validate_config,
    _validated_skills_dir,
    agent_install_paths,
    sandbox_config_dir,
    sandbox_environment,
)
from agent.execution.ports import ManagedExecutor


LOG = logging.getLogger(__name__)


_PATH_ESCAPE = "Permission denied: path escapes its authorized mount"


_MOUNT_DELETE = "Permission denied: cannot delete a sandbox mount point"


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


@dataclass(frozen=True)
class _RouteState:
    """Candidate file-tool routes. Building one must not publish it."""

    default: BackendProtocol
    routes: dict[str, BackendProtocol]
    points: tuple[_MountPoint, ...]
    files: dict[str, _MountPoint]
    directories: tuple[str, ...]
    hidden_roots: tuple[Path, ...]
    install_roots: tuple[Path, ...]


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
        state = self._route_state()
        super().__init__(
            default=state.default,
            routes=state.routes,
            artifacts_root=f"{self.workspace_root}/.deepagents",
        )
        self._publish_route_state(state)

    def apply_sandbox_mode(self, mode: SandboxMode) -> None:
        """Switch filesystem mounts on the existing sandbox and drop its processes."""
        config = getattr(self.executor, "config", None)
        if not isinstance(config, SandboxConfig) or not isinstance(self.executor, ManagedExecutor):
            raise ValueError("Sandbox mode can only be changed for a Bubblewrap backend")
        updated = replace(config, mode=mode)
        _validate_config(updated)
        skills = _validated_skills_dir(updated.workspace)
        env = sandbox_environment(updated)
        # Build the next file routes before publishing. A failure here leaves
        # the live config, protection cache, and current pool alone.
        state = self._route_state(config=updated, skills_dir=skills)
        try:
            self.executor.reconfigure(updated, skills, env)
        except Exception as exc:
            raise RuntimeError(
                f"sandbox mode was not changed; the current sandbox did not stop: {exc}"
            ) from exc
        self.skills_dir = skills
        self._publish_route_state(state)

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

    def _publish_route_state(self, state: _RouteState) -> None:
        self.default = state.default
        self.routes = state.routes
        self.sorted_routes = sorted(state.routes.items(), key=lambda item: len(item[0]), reverse=True)
        self._remember_routes(state.points, state.files, list(state.directories), state.default)
        self._hidden_roots = state.hidden_roots
        self._install_roots = state.install_roots

    def _route_state(
        self,
        config: SandboxConfig | None = None,
        skills_dir: Path | None = None,
    ) -> _RouteState:
        executor = self.executor
        if config is None:
            config = getattr(executor, "config", None)
        if skills_dir is None:
            skills_dir = self.skills_dir
        points = _mount_points(config, skills_dir) if isinstance(config, SandboxConfig) else ()
        expose_mounts = isinstance(executor, ManagedExecutor)
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
        hidden_roots: tuple[Path, ...] = ()
        install_roots: tuple[Path, ...] = ()
        if full_access and isinstance(config, SandboxConfig):
            install_roots = agent_install_paths()
            hidden = [sandbox_config_dir(config)]
            hidden.extend(path for path in config.protected_files if path.is_dir())
            hidden_roots = tuple(dict.fromkeys(hidden))
            for root in install_roots:
                if root.is_dir():
                    routes[f"{root.as_posix()}/"] = _ReadOnlyFilesystemBackend(root, reason=INSTALL_READ_ONLY)
                    directories.append(root.as_posix())
                else:
                    routes[f"{root.as_posix()}/"] = _ExactFileBackend(root, read_only=True)
                    files[root.as_posix()] = _MountPoint(
                        PurePosixPath(root.as_posix()), root, True, True,
                    )
            denied = _DeniedBackend()
            for root in hidden_roots:
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
        return _RouteState(
            default=default,
            routes=routes,
            points=active_points,
            files=files,
            directories=tuple(directories),
            hidden_roots=hidden_roots,
            install_roots=install_roots,
        )

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

    def _file_info(self, point: _MountPoint) -> FileInfo:
        info: FileInfo = {"path": point.virtual, "is_dir": False}
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
        entries: list[FileInfo] = []
        for entry in result.entries:
            route = str(entry.get("path", "")).rstrip("/")
            point = self._file_mounts.get(route)
            if point is None:
                entries.append(cast(FileInfo, dict(entry)))
                continue
            if self._covered_by_directory(route):
                continue
            entries.append(self._file_info(point))
        entries.sort(key=lambda item: str(item.get("path", "")))
        return LsResult(entries=entries)

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

    def _uncovered_file_matches(self, pattern: str, path: str | None) -> list[FileInfo]:
        if not self._file_mounts:
            return []
        try:
            matcher = compile_grep_include_glob(pattern)
        except InvalidGlobPatternError:
            return []
        matches: list[FileInfo] = []
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
            matches=matches,
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
        entries: list[FileInfo] = [cast(FileInfo, dict(entry)) for entry in result.entries or []]
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
        return LsResult(entries=entries)

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
