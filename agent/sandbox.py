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
from agent.sandbox_pool import PersistentSandbox, SandboxLostError, SandboxPool, SandboxStartError


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
WORKER_SOURCE_PATH = Path(__file__).with_name("sandbox_worker.py")
WORKER_SANDBOX_PATH = "/run/deep-agent/sandbox_worker.py"
KILL_CONFIRM_SECONDS = 5.0
UNSANDBOXED_WARNING = (
    "UNSANDBOXED MODE: bubblewrap is unavailable. Agent commands will run "
    "directly on the host with the current user's permissions."
)


class ExecutionMode(StrEnum):
    SANDBOXED = "SANDBOXED"
    UNSANDBOXED = "UNSANDBOXED"
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


_OUTSIDE_WORKSPACE_ERROR = "Permission denied: paths must be under /workspace or /skills"


class _OutsideWorkspaceBackend(BackendProtocol):
    """默认路由：拒绝 /workspace 与 /skills 之外的路径，不持久化。"""

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
    """向文件工具暴露应用 Skills，禁止写入。"""

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
    """暴露工作区与只读 Skills，执行委托给底层 executor。"""

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
    """文件系统 backend，命令执行由 Bubblewrap 隔离。"""

    def __init__(self, config: SandboxConfig, *, executable: str) -> None:
        workspace = _validate_config(config)
        super().__init__(root_dir=workspace, virtual_mode=True, max_file_size_mb=10)
        self.config = replace(config, workspace=workspace)
        self.skills_dir = _validated_skills_dir(workspace)
        self.executable = executable
        self._env = sandbox_environment(config)
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
        args += [*(extra or ()), "--clearenv"]
        for name, value in sorted(self._env.items()):
            args += ["--setenv", name, value]
        args += ["--chdir", SANDBOX_ROOT, "--"]
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


class UnsandboxedShellBackend(LocalShellBackend):
    """经明确授权的宿主执行，使用净化后的临时环境。"""

    def __init__(self, config: SandboxConfig) -> None:
        workspace = _validate_config(config)
        super().__init__(
            root_dir=workspace,
            virtual_mode=True,
            timeout=config.timeout_seconds or 120,  # 父类默认值不起作用；execute() 使用我们的可选上限。
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
    if not validated.allow_unsandboxed:
        raise SandboxUnavailableError(
            f"{reason}\nSandboxing remains required. Only explicit sandbox.allow_unsandboxed: true "
            "or the CLI UNSANDBOXED confirmation authorizes host execution.", kind=kind,
        )
    warning = f"{UNSANDBOXED_WARNING} Reason: {reason}"
    warnings.warn(warning, RuntimeWarning, stacklevel=2)
    LOG.warning(warning)
    fallback = UnsandboxedShellBackend(validated)
    return BackendSelection(_workspace_backend(fallback), ExecutionMode.UNSANDBOXED, warning)


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
    worker_dir = PurePosixPath(WORKER_SANDBOX_PATH).parent
    if destination == worker_dir or worker_dir in destination.parents or destination in worker_dir.parents:
        raise ValueError(f"extra mount conflicts with the sandbox worker: {destination}")
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
                agent_log_path = f"{SANDBOX_ROOT}/{relative_log_path.as_posix()}"
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
