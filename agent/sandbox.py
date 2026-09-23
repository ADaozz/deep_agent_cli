from __future__ import annotations

from codecs import IncrementalDecoder, getincrementaldecoder
from dataclasses import dataclass, replace
from enum import StrEnum
import logging
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
import warnings

from agent.cancel import emit_tool_output, get_cancel_context

from deepagents.backends import CompositeBackend, FilesystemBackend, LocalShellBackend
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

from agent.config import SAFE_INHERITED_ENV, BindMount, SandboxConfig


LOG = logging.getLogger(__name__)
SANDBOX_ROOT = "/workspace"
FIXED_PATH = "/usr/local/bin:/usr/bin:/bin"
RUNTIME_PATHS = ("/usr", "/bin", "/lib", "/lib64")
RUNTIME_FILES = (
    "/etc/alternatives",
    "/etc/group",
    "/etc/ld.so.cache",
    "/etc/localtime",
    "/etc/nsswitch.conf",
    "/etc/passwd",
)
NETWORK_FILES = ("/etc/hosts", "/etc/resolv.conf", "/etc/ssl")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
UNSANDBOXED_WARNING = (
    "UNSANDBOXED MODE: bubblewrap is unavailable. Agent commands will run "
    "directly on the host with the current user's permissions."
)


class ExecutionMode(StrEnum):
    SANDBOXED = "SANDBOXED"
    UNSANDBOXED = "UNSANDBOXED"
    CUSTOM = "CUSTOM"


class SandboxUnavailableError(RuntimeError):
    """Raised when sandboxing is unavailable and unsafe fallback was not authorized."""


@dataclass(frozen=True)
class BackendSelection:
    backend: SandboxBackendProtocol
    mode: ExecutionMode
    warning: str = ""


_OUTSIDE_WORKSPACE_ERROR = "Permission denied: paths must be under /workspace"


class _OutsideWorkspaceBackend(BackendProtocol):
    """Non-persistent default route that rejects paths outside /workspace."""

    def ls(self, path: str) -> LsResult:
        return LsResult(entries=[])

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return ReadResult(error=_OUTSIDE_WORKSPACE_ERROR)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        return GrepResult(matches=[])

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return GlobResult(matches=[])

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=_OUTSIDE_WORKSPACE_ERROR)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return EditResult(error=_OUTSIDE_WORKSPACE_ERROR)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=_OUTSIDE_WORKSPACE_ERROR)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error=_OUTSIDE_WORKSPACE_ERROR) for path, _ in files]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [FileDownloadResponse(path=path, error=_OUTSIDE_WORKSPACE_ERROR) for path in paths]


class WorkspaceCompositeBackend(CompositeBackend, SandboxBackendProtocol):
    """Expose one /workspace file route while delegating execution to its sandbox."""

    def __init__(self, executor: SandboxBackendProtocol) -> None:
        super().__init__(
            default=_OutsideWorkspaceBackend(),
            routes={f"{SANDBOX_ROOT}/": executor},
            artifacts_root=f"{SANDBOX_ROOT}/.deepagents",
        )
        self.executor = executor

    @property
    def id(self) -> str:
        return self.executor.id

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self.executor.execute(command, timeout=timeout)

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return await self.executor.aexecute(command, timeout=timeout)


def sandbox_environment(config: SandboxConfig, environ: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    result = {
        "PATH": FIXED_PATH,
        "HOME": "/home/agent",
        "PWD": SANDBOX_ROOT,
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
    return result


class _ProtectedWorkspaceMixin:
    config: SandboxConfig

    def _write_is_protected(self, file_path: str) -> bool:
        path_parts = PurePosixPath(file_path).parts
        if path_parts and path_parts[0] == "/":
            path_parts = path_parts[1:]
        for relative in self.config.protected_workspace_paths:
            protected_parts = PurePosixPath(relative).parts
            if path_parts[: len(protected_parts)] == protected_parts:
                return True
        return False

    def write(self, file_path: str, content: str) -> WriteResult:
        if self._write_is_protected(file_path):
            return WriteResult(error=f"Permission denied: protected workspace path '{file_path}'")
        return super().write(file_path, content)  # type: ignore[misc]

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        if self._write_is_protected(file_path):
            return EditResult(error=f"Permission denied: protected workspace path '{file_path}'")
        return super().edit(file_path, old_string, new_string, replace_all=replace_all)  # type: ignore[misc]

    def delete(self, file_path: str) -> DeleteResult:
        if self._write_is_protected(file_path):
            return DeleteResult(error=f"Permission denied: protected workspace path '{file_path}'")
        return super().delete(file_path)  # type: ignore[misc]

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        responses: list[FileUploadResponse | None] = [None] * len(files)
        allowed: list[tuple[int, str, bytes]] = []
        for index, (path, content) in enumerate(files):
            if self._write_is_protected(path):
                responses[index] = FileUploadResponse(
                    path=path,
                    error=f"Permission denied: protected workspace path '{path}'",
                )
            else:
                allowed.append((index, path, content))
        if allowed:
            delegated = super().upload_files([(path, content) for _, path, content in allowed])  # type: ignore[misc]
            for (index, _, _), response in zip(allowed, delegated, strict=True):
                responses[index] = response
        return [response for response in responses if response is not None]


class BubblewrapBackend(_ProtectedWorkspaceMixin, FilesystemBackend, SandboxBackendProtocol):
    """Filesystem backend with command execution isolated by bubblewrap."""

    def __init__(self, config: SandboxConfig, *, executable: str) -> None:
        workspace = _validate_config(config)
        super().__init__(root_dir=workspace, virtual_mode=True, max_file_size_mb=10)
        self.config = replace(config, workspace=workspace)
        self.executable = executable
        self._env = sandbox_environment(config)
        self._sandbox_id = f"bwrap-{uuid.uuid4().hex[:8]}"

    @property
    def id(self) -> str:
        return self._sandbox_id

    def command_args(self, command: str, *, network: bool = False) -> list[str]:
        if not command or not isinstance(command, str):
            raise ValueError("command must be a non-empty string")
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
        args += [
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--tmpfs", "/home",
            "--dir", "/home/agent",
            "--dir", "/etc",
            "--bind", str(self.config.workspace), SANDBOX_ROOT,
        ]
        for path in (*RUNTIME_PATHS, *RUNTIME_FILES):
            if Path(path).exists():
                args += ["--ro-bind", path, path]
        if network:
            for path in NETWORK_FILES:
                if Path(path).exists():
                    args += ["--ro-bind", path, path]
        for relative in self.config.protected_workspace_paths:
            source = _protected_source(self.config.workspace, relative)
            if source.exists():
                destination = f"{SANDBOX_ROOT}/{relative.strip('/')}"
                args += ["--ro-bind", str(source), destination]
        for mount in self.config.extra_read_only_mounts:
            args += _mount_args("--ro-bind", mount)
        for mount in self.config.extra_read_write_mounts:
            args += _mount_args("--bind", mount)
        args += ["--clearenv"]
        for name, value in sorted(self._env.items()):
            args += ["--setenv", name, value]
        args += ["--chdir", SANDBOX_ROOT, "--", "/bin/sh", "-lc", command]
        return args

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if not command or not isinstance(command, str):
            return ExecuteResponse("Error: Command must be a non-empty string.", 1, False)
        from agent.network import get_execute_network

        effective_timeout = _effective_timeout(timeout, self.config.timeout_seconds)
        return _run_process(
            self.command_args(command, network=get_execute_network()),
            timeout=effective_timeout,
            max_output_bytes=self.config.max_output_bytes,
        )


class UnsandboxedShellBackend(_ProtectedWorkspaceMixin, LocalShellBackend):
    """Explicitly authorized host execution with a sanitized, ephemeral environment."""

    def __init__(self, config: SandboxConfig) -> None:
        workspace = _validate_config(config)
        super().__init__(
            root_dir=workspace,
            virtual_mode=True,
            timeout=config.timeout_seconds,
            max_output_bytes=config.max_output_bytes,
            env={},
            inherit_env=False,
        )
        self.config = replace(config, workspace=workspace)
        self._base_env = sandbox_environment(config)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if not command or not isinstance(command, str):
            return ExecuteResponse("Error: Command must be a non-empty string.", 1, False)
        effective_timeout = _effective_timeout(timeout, self.config.timeout_seconds)
        with tempfile.TemporaryDirectory(prefix="deepagent-home-") as home_dir:
            temp_dir = Path(home_dir) / "tmp"
            temp_dir.mkdir()
            env = dict(self._base_env)
            env.update({"HOME": home_dir, "PWD": str(self.config.workspace), "TMPDIR": str(temp_dir)})
            return _run_process(
                command,
                timeout=effective_timeout,
                max_output_bytes=self.config.max_output_bytes,
                cwd=self.config.workspace,
                env=env,
                shell=True,
            )


def select_backend(config: SandboxConfig, *, check: bool = True) -> BackendSelection:
    workspace = _validate_config(config)
    validated = replace(config, workspace=workspace)
    executable = _resolve_executable(validated.bwrap_path)
    reason = "bubblewrap executable was not found"
    if executable:
        candidate = BubblewrapBackend(validated, executable=executable)
        if not check:
            return BackendSelection(_workspace_backend(candidate), ExecutionMode.SANDBOXED)
        probe = candidate.execute("true", timeout=min(5, validated.timeout_seconds))
        if probe.exit_code == 0:
            return BackendSelection(_workspace_backend(candidate), ExecutionMode.SANDBOXED)
        reason = f"bubblewrap preflight failed: {probe.output}"
    if not validated.allow_unsandboxed:
        raise SandboxUnavailableError(
            f"{reason}. Install bubblewrap or explicitly set SANDBOX_ALLOW_UNSANDBOXED=true."
        )
    warning = f"{UNSANDBOXED_WARNING} Reason: {reason}"
    warnings.warn(warning, RuntimeWarning, stacklevel=2)
    LOG.warning(warning)
    fallback = UnsandboxedShellBackend(validated)
    return BackendSelection(_workspace_backend(fallback), ExecutionMode.UNSANDBOXED, warning)


def _workspace_backend(backend: SandboxBackendProtocol) -> WorkspaceCompositeBackend:
    return WorkspaceCompositeBackend(backend)


def _validated_workspace(workspace: Path) -> Path:
    resolved = workspace.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"sandbox workspace must be an existing directory: {resolved}")
    return resolved


def _validate_config(config: SandboxConfig) -> Path:
    workspace = _validated_workspace(config.workspace)
    if config.timeout_seconds <= 0:
        raise ValueError(f"timeout_seconds must be positive, got {config.timeout_seconds}")
    if config.max_output_bytes <= 0:
        raise ValueError(f"max_output_bytes must be positive, got {config.max_output_bytes}")
    for relative in config.protected_workspace_paths:
        _protected_source(workspace, relative)
    for mount in config.extra_read_only_mounts:
        _mount_args("--ro-bind", mount)
    for mount in config.extra_read_write_mounts:
        _mount_args("--bind", mount)
    for name in (*config.env_allowlist, *config.env_set):
        if not ENV_NAME.fullmatch(name):
            raise ValueError(f"invalid environment variable name: {name!r}")
    return workspace


def _resolve_executable(value: str) -> str | None:
    if not value.strip():
        raise ValueError("bwrap_path cannot be empty")
    if "/" in value:
        candidate = Path(value).expanduser().resolve()
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    return shutil.which(value)


def _protected_source(workspace: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ValueError(f"protected workspace path must be relative and contained: {relative!r}")
    resolved = (workspace / Path(*pure.parts)).resolve()
    if not resolved.is_relative_to(workspace):
        raise ValueError(f"protected workspace path escapes workspace: {relative!r}")
    return resolved


def _mount_args(flag: str, mount: BindMount) -> list[str]:
    source = mount.source.expanduser().resolve()
    destination = PurePosixPath(mount.destination)
    if not source.exists():
        raise ValueError(f"mount source does not exist: {source}")
    if not destination.is_absolute() or ".." in destination.parts:
        raise ValueError(f"mount destination must be an absolute sandbox path: {mount.destination!r}")
    protected = {PurePosixPath(SANDBOX_ROOT), PurePosixPath("/tmp"), PurePosixPath("/home/agent")}
    if destination in protected:
        raise ValueError(f"extra mount cannot replace protected destination: {destination}")
    return [flag, str(source), str(destination)]


def _effective_timeout(requested: int | None, default: int) -> int:
    value = default if requested is None else requested
    if value <= 0:
        raise ValueError(f"timeout must be positive, got {value}")
    return value


def _run_process(
    command: list[str] | str,
    *,
    timeout: int,
    max_output_bytes: int,
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
        return ExecuteResponse(f"Error executing command ({type(exc).__name__}): {exc}", 1, False)

    if ctx is not None:
        ctx.register_process(process)

    deadline = time.monotonic() + timeout
    decoder: IncrementalDecoder = getincrementaldecoder("utf-8")(errors="replace")
    kept = bytearray()
    truncated = False
    truncation_marked = False
    saw_output = False
    cancelled = False
    timed_out = False

    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")

    def _append_chunk(raw: bytes) -> None:
        nonlocal truncated, truncation_marked, saw_output
        if not raw:
            return
        saw_output = True
        text = decoder.decode(raw)
        if not text:
            return
        emit_tool_output(tool_call_id, text, stream="merged")
        remaining = max_output_bytes - len(kept)
        if remaining > 0:
            kept.extend(raw[:remaining])
            if len(raw) > remaining:
                truncated = True
        else:
            truncated = True
        if truncated and not truncation_marked:
            marker = f"\n\n... Output truncated at {max_output_bytes} bytes."
            emit_tool_output(tool_call_id, marker, stream="merged")
            truncation_marked = True

    try:
        while True:
            if ctx is not None and ctx.cancelled:
                cancelled = True
                _kill_process_group_pid(process.pid)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _kill_process_group_pid(process.pid)
                break
            wait = max(0.0, min(0.1, deadline - time.monotonic()))
            events = selector.select(timeout=wait)
            if not events:
                if process.poll() is not None:
                    # Drain any remaining buffered bytes.
                    for stream in (process.stdout, process.stderr):
                        try:
                            leftover = stream.read()
                        except Exception:  # noqa: BLE001
                            leftover = b""
                        if leftover:
                            _append_chunk(leftover)
                    break
                continue
            for key, _mask in events:
                try:
                    chunk = key.fileobj.read1(4096)  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    chunk = b""
                if chunk:
                    _append_chunk(chunk)
                else:
                    try:
                        selector.unregister(key.fileobj)
                    except Exception:  # noqa: BLE001
                        pass
            if process.poll() is not None and not selector.get_map():
                break
        # Final decoder flush for partial multi-byte sequences.
        tail = decoder.decode(b"", final=True)
        if tail:
            emit_tool_output(tool_call_id, tail, stream="merged")
            remaining = max_output_bytes - len(kept)
            if remaining > 0:
                kept.extend(tail.encode("utf-8", errors="replace")[:remaining])
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _kill_process_group_pid(process.pid)
            process.wait(timeout=1)
    finally:
        selector.close()
        if ctx is not None:
            ctx.clear_process()

    if cancelled or (ctx is not None and ctx.cancelled):
        output = "Cancelled by user."
        if saw_output and kept:
            text = bytes(kept).decode("utf-8", errors="replace")
            output = f"{text.rstrip()}\n\nCancelled by user."
        return ExecuteResponse(output, 130, truncated)

    if timed_out:
        return ExecuteResponse(f"Error: Command timed out after {timeout} seconds.", 124, truncated)

    output = bytes(kept).decode("utf-8", errors="replace") if kept else ""
    if truncated and not output.endswith("truncated"):
        output = f"{output}\n\n... Output truncated at {max_output_bytes} bytes."
    if not output:
        output = "<no output>"
    exit_code = int(process.returncode or 0)
    if exit_code:
        output = f"{output.rstrip()}\n\nExit code: {exit_code}"
    return ExecuteResponse(output, exit_code, truncated)


def _kill_process_group_pid(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _truncate_output(output: str, limit: int) -> tuple[str, bool]:
    encoded = output.encode("utf-8")
    if len(encoded) <= limit:
        return output, False
    shortened = encoded[:limit].decode("utf-8", errors="ignore")
    return f"{shortened}\n\n... Output truncated at {limit} bytes.", True
