"""Host path routing, mount rules, and one-shot command execution.

Persistent sandbox reuse lives in ``pool.py``. The in-sandbox worker
lives in ``worker.py`` and cannot import this package. ``restricted``
and ``full-access`` mounts are decided here; pool reuse does not change them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
from pathlib import Path
import uuid

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import BackendProtocol, ExecuteResponse, SandboxBackendProtocol

from agent.execution.filesystem import WorkspaceCompositeBackend
from agent.execution.layout import WORKER_SANDBOX_PATH
from agent.execution.ports import ManagedExecutor
from agent.execution.layout import (
    FIXED_PATH,
    NETWORK_FILES,
    RUNTIME_FILES,
    RUNTIME_PATHS,
    SEARCH_BINARY_PATH,
    _full_access_created_dirs,
    _full_access_protection_args,
    _host_root_mirror_args,
    _mount_args,
    _resolve_executable,
    _sandbox_python,
    _validate_config,
    _validated_skills_dir,
    ensure_sandbox_state_dirs,
    sandbox_environment,
)
from agent.execution.process import LocalExecuteResponse, _effective_timeout, _run_in_sandbox
from agent.execution.policy import sandbox_workspace_root
from agent.execution.types import ExecutionMode, SandboxUnavailableError, SKILLS_ROOT
from agent.execution.policy import SandboxConfig
from agent.execution.mounts import effective_user_mounts
from agent.execution.pool import SandboxPool, SandboxStartError


LOG = logging.getLogger(__name__)
WORKER_SOURCE_PATH = Path(__file__).with_name("worker.py")
KILL_CONFIRM_SECONDS = 5.0


@dataclass(frozen=True)
class BackendSelection:
    backend: SandboxBackendProtocol
    mode: ExecutionMode
    warning: str = ""


class BubblewrapBackend(FilesystemBackend, SandboxBackendProtocol, ManagedExecutor):
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

    def reconfigure(self, config: SandboxConfig, skills: Path | None, environment: dict[str, str]) -> None:
        self.pool.shutdown("sandbox mode changed")
        self.config = config
        self.skills_dir = skills
        self._env = environment

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
        read_only, read_write = effective_user_mounts(self.config)
        for mount in read_only:
            args += _mount_args("--ro-bind", mount, workspace_root=self.workspace_root)
        for mount in read_write:
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
        from agent.execution.network import get_execute_network

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
