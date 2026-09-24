from __future__ import annotations

from codecs import IncrementalDecoder, getincrementaldecoder
from dataclasses import dataclass, replace
from datetime import datetime
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

from agent.config import SAFE_INHERITED_ENV, BindMount, SandboxConfig, default_skills_dir


LOG = logging.getLogger(__name__)
SANDBOX_ROOT = "/workspace"
SKILLS_ROOT = "/skills"
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


@dataclass
class LocalExecuteResponse(ExecuteResponse):
    """Execute result with local metadata for the UI's ToolMessage artifact."""

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


_OUTSIDE_WORKSPACE_ERROR = "Permission denied: paths must be under /workspace or /skills"


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


class _ReadOnlySkillsBackend(FilesystemBackend):
    """Expose application skills to file tools without write operations."""

    def __init__(self, root: Path) -> None:
        super().__init__(root_dir=root, virtual_mode=True, max_file_size_mb=10)

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error="Permission denied: skills are read-only")

    def edit(self, file_path: str, old_string: str, new_string: str,
             replace_all: bool = False) -> EditResult:
        return EditResult(error="Permission denied: skills are read-only")

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error="Permission denied: skills are read-only")

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error="Permission denied: skills are read-only")
                for path, _ in files]


class WorkspaceCompositeBackend(CompositeBackend, SandboxBackendProtocol):
    """Expose workspace and read-only skills while delegating execution."""

    def __init__(self, executor: SandboxBackendProtocol, *, skills_dir: Path | None) -> None:
        routes: dict[str, BackendProtocol] = {f"{SANDBOX_ROOT}/": executor}
        if skills_dir is not None:
            routes[f"{SKILLS_ROOT}/"] = _ReadOnlySkillsBackend(skills_dir)
        super().__init__(
            default=_OutsideWorkspaceBackend(),
            routes=routes,
            artifacts_root=f"{SANDBOX_ROOT}/.deepagents",
        )
        self.executor = executor
        self.skills_dir = skills_dir

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


class BubblewrapBackend(FilesystemBackend, SandboxBackendProtocol):
    """Filesystem backend with command execution isolated by bubblewrap."""

    def __init__(self, config: SandboxConfig, *, executable: str) -> None:
        workspace = _validate_config(config)
        super().__init__(root_dir=workspace, virtual_mode=True, max_file_size_mb=10)
        self.config = replace(config, workspace=workspace)
        self.skills_dir = _validated_skills_dir(workspace)
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
        for mount in self.config.extra_read_only_mounts:
            args += _mount_args("--ro-bind", mount)
        for mount in self.config.extra_read_write_mounts:
            args += _mount_args("--bind", mount)
        if self.skills_dir is not None:
            args += ["--ro-bind", str(self.skills_dir), SKILLS_ROOT]
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
            workspace=self.config.workspace,
        )


class UnsandboxedShellBackend(LocalShellBackend):
    """Explicitly authorized host execution with a sanitized, ephemeral environment."""

    def __init__(self, config: SandboxConfig) -> None:
        workspace = _validate_config(config)
        super().__init__(
            root_dir=workspace,
            virtual_mode=True,
            timeout=config.timeout_seconds or 120,  # Inert parent default; execute() uses our optional cap.
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
                workspace=self.config.workspace,
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
        probe = candidate.execute(
            "true", timeout=min(5, validated.timeout_seconds) if validated.timeout_seconds else 5,
        )
        if probe.exit_code == 0:
            return BackendSelection(_workspace_backend(candidate), ExecutionMode.SANDBOXED)
        reason = f"bubblewrap preflight failed: {probe.output}"
    if not validated.allow_unsandboxed:
        raise SandboxUnavailableError(
            f"{reason}. Install bubblewrap or set sandbox.allow_unsandboxed: true "
            "in ~/.deep-agent/config.yaml."
        )
    warning = f"{UNSANDBOXED_WARNING} Reason: {reason}"
    warnings.warn(warning, RuntimeWarning, stacklevel=2)
    LOG.warning(warning)
    fallback = UnsandboxedShellBackend(validated)
    return BackendSelection(_workspace_backend(fallback), ExecutionMode.UNSANDBOXED, warning)


def _workspace_backend(backend: SandboxBackendProtocol) -> WorkspaceCompositeBackend:
    return WorkspaceCompositeBackend(backend, skills_dir=_validated_skills_dir(backend.config.workspace))


def _validated_skills_dir(workspace: Path) -> Path | None:
    source = default_skills_dir()
    if source.is_symlink():
        raise ValueError(f"skills directory must not be a symlink: {source}")
    if not source.exists():
        return None
    if not source.is_dir():
        raise ValueError(f"skills directory must be a directory: {source}")
    resolved = source.resolve()
    if resolved.is_relative_to(workspace):
        raise ValueError(f"skills directory must be outside workspace: {source}")
    return resolved


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
    _validated_skills_dir(workspace)
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
    if destination == PurePosixPath(SKILLS_ROOT) or PurePosixPath(SKILLS_ROOT) in destination.parents or destination in PurePosixPath(SKILLS_ROOT).parents:
        raise ValueError(f"extra mount conflicts with skills destination: {destination}")
    return [flag, str(source), str(destination)]


def _effective_timeout(requested: int | None, cap: int | None) -> int | None:
    if requested is not None and requested <= 0:
        raise ValueError(f"timeout must be positive, got {requested}")
    if cap is None:
        return requested
    return min(requested, cap) if requested is not None else cap


def _create_exec_log(workspace: Path) -> tuple[int, int, str]:
    """Create a private log below the workspace without following directory links."""
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
    decoder: IncrementalDecoder = getincrementaldecoder("utf-8")(errors="replace")
    tail_bytes = bytearray()
    total_bytes = 0
    truncated = False
    truncation_marked = False
    cancelled = False
    timed_out = False
    log_fd: int | None = None
    log_directory_fd: int | None = None
    log_name: str | None = None
    log_error: str | None = None
    workspace_root = (workspace or cwd or Path.cwd()).resolve()
    post_exit_idle_since: float | None = None

    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")

    def _discard_log(exc: OSError) -> None:
        nonlocal log_fd, log_error
        log_error = f"{type(exc).__name__}: {exc}"
        if log_fd is not None:
            try:
                os.close(log_fd)
            except OSError:
                pass
            log_fd = None
        if log_directory_fd is not None and log_name is not None:
            try:
                os.unlink(log_name, dir_fd=log_directory_fd)
            except OSError:
                pass

    def _append_chunk(raw: bytes) -> None:
        nonlocal total_bytes, truncated, truncation_marked
        nonlocal log_fd, log_directory_fd, log_name, log_error
        if not raw:
            return
        remaining = max(0, max_output_bytes - total_bytes)
        accepted = raw[:remaining] if remaining > 0 else b""
        if accepted:
            text = decoder.decode(accepted)
            if text:
                emit_tool_output(tool_call_id, text, stream="merged")
        if not truncated and total_bytes + len(raw) > max_output_bytes:
            truncated = True
            if log_fd is None and log_error is None:
                try:
                    log_fd, log_directory_fd, log_name = _create_exec_log(workspace_root)
                    _write_all(log_fd, tail_bytes)
                    _write_all(log_fd, raw)
                except OSError as exc:
                    _discard_log(exc)
        elif log_fd is not None:
            try:
                _write_all(log_fd, raw)
            except OSError as exc:
                _discard_log(exc)
        total_bytes += len(raw)
        tail_bytes.extend(raw)
        if len(tail_bytes) > max_output_bytes:
            del tail_bytes[:-max_output_bytes]
        if truncated and not truncation_marked:
            tail = decoder.decode(b"", final=True)
            if tail:
                emit_tool_output(tool_call_id, tail, stream="merged")
            marker = f"\n\n... Output truncated at {max_output_bytes} bytes."
            emit_tool_output(tool_call_id, marker, stream="merged")
            truncation_marked = True

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
                    _append_chunk(chunk)
                    if stopping or process.poll() is not None:
                        post_exit_idle_since = time.monotonic()
                else:
                    try:
                        selector.unregister(key.fileobj)
                    except Exception:  # noqa: BLE001
                        pass
            if (cancelled or timed_out or process.poll() is not None) and not selector.get_map():
                break
        # Final decoder flush for partial multi-byte sequences.
        if not truncated:
            tail = decoder.decode(b"", final=True)
            if tail:
                emit_tool_output(tool_call_id, tail, stream="merged")
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
        if log_fd is not None:
            try:
                os.fsync(log_fd)
                os.close(log_fd)
                log_fd = None
            except OSError as exc:
                _discard_log(exc)
        if log_directory_fd is not None:
            os.close(log_directory_fd)

    output = bytes(tail_bytes).decode("utf-8", errors="replace") if tail_bytes else ""
    host_log_path: str | None = None
    agent_log_path: str | None = None
    if truncated:
        detail = f"Output truncated: showing the last {max_output_bytes} bytes."
        if log_error is not None:
            detail += f"\nFull output could not be saved: {log_error}"
        elif log_name is not None:
            relative_log_path = Path(".deep-agent/logs/exec") / log_name
            host_log_path = str(workspace_root / relative_log_path)
            agent_log_path = f"{SANDBOX_ROOT}/{relative_log_path.as_posix()}"
            detail += f"\nFull output saved to: {host_log_path}"
            detail += f"\nAgent path: {agent_log_path}"
        output = f"{output}\n\n[{detail}]"

    result_metadata = {
        "host_log_path": host_log_path,
        "agent_log_path": agent_log_path,
        "log_error": log_error,
        "max_output_bytes": max_output_bytes,
    }

    if cancelled or (ctx is not None and ctx.cancelled):
        return LocalExecuteResponse(
            f"{output.rstrip()}\n\nCancelled by user.".strip(), 130, truncated,
            termination_reason="cancelled", **result_metadata,
        )

    if timed_out:
        return LocalExecuteResponse(
            f"{output.rstrip()}\n\nError: Command timed out after {timeout} seconds.".strip(),
            124, truncated, termination_reason="timeout", **result_metadata,
        )

    if not output:
        output = "<no output>"
    exit_code = int(process.returncode or 0)
    if exit_code:
        output = f"{output.rstrip()}\n\nExit code: {exit_code}"
    return LocalExecuteResponse(output, exit_code, truncated, **result_metadata)


def _kill_process_group_pid(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
