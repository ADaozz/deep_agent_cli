"""Host process double for tests that need execute without Bubblewrap.

This is not a sandbox mode. Production execution always goes through Bubblewrap.
"""
from __future__ import annotations

import os
from pathlib import Path

from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse

from agent.config import SandboxConfig
from agent.sandbox import _effective_timeout, _run_process, sandbox_workspace_root


class HostCommandBackend(LocalShellBackend):
    def __init__(self, workspace: Path, **kwargs: object) -> None:
        config = SandboxConfig(workspace=workspace, **kwargs)  # type: ignore[arg-type]
        super().__init__(
            root_dir=config.workspace,
            virtual_mode=True,
            timeout=config.timeout_seconds or 120,
            max_output_bytes=config.max_output_bytes,
            env={},
            inherit_env=False,
        )
        self.config = config
        self.workspace_root = sandbox_workspace_root(config.workspace)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if not command or not isinstance(command, str):
            return ExecuteResponse("Error: Command must be a non-empty string.", 1, False)
        effective = _effective_timeout(timeout, self.config.timeout_seconds)
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self.config.workspace),
            "TMPDIR": str(self.config.workspace),
            "LANG": "C.UTF-8",
        }
        return _run_process(
            command,
            timeout=effective,
            max_output_bytes=self.config.max_output_bytes,
            workspace=self.config.workspace,
            cwd=self.config.workspace,
            env=env,
            shell=True,
        )
