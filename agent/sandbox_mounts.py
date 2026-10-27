"""One reading of the sandbox mount policy for the current filesystem mode.

Bubblewrap arguments and file-tool routes both start from
``effective_user_mounts``. The launch manifest describes that same policy.
It is not a dump of a running process's kernel mount table.
"""
from __future__ import annotations

from pathlib import Path

from agent.config import BindMount, SandboxConfig, SandboxMode


def mount_applies(mount: BindMount, mode: SandboxMode) -> bool:
    return mount.modes is None or mode in mount.modes


def effective_user_mounts(config: SandboxConfig) -> tuple[tuple[BindMount, ...], tuple[BindMount, ...]]:
    """Read-only then read-write mounts that the current mode actually uses."""
    read_only = tuple(mount for mount in config.extra_read_only_mounts if mount_applies(mount, config.mode))
    read_write = tuple(mount for mount in config.extra_read_write_mounts if mount_applies(mount, config.mode))
    return read_only, read_write


def render_launch_manifest(
    config: SandboxConfig,
    *,
    skills_dir: Path | None,
    pending_mode: SandboxMode | None = None,
    rg_path: str | None = None,
) -> str:
    """Text for ``/sandbox mounts``. Effective policy for the next worker, not a live mount table."""
    from agent.sandbox import (
        NETWORK_FILES,
        RUNTIME_FILES,
        RUNTIME_PATHS,
        SEARCH_BINARY_PATH,
        SKILLS_ROOT,
        sandbox_state_dirs,
        sandbox_config_dir,
        sandbox_workspace_root,
    )
    from agent.tools.grep import resolve_rg

    read_only, read_write = effective_user_mounts(config)
    workspace = sandbox_workspace_root(config.workspace)
    host_workspace = str(config.workspace.expanduser().resolve())
    lines = [
        f"Sandbox filesystem: {config.mode}",
        f"Pending mode: {pending_mode or 'none'}",
        "Manifest: effective launch policy (not a live kernel mount table)",
        "",
        "Execution mounts:",
        f"  RW  {workspace}  <- {host_workspace} [workspace]",
    ]
    if skills_dir is not None:
        lines.append(f"  RW  {SKILLS_ROOT}  <- {skills_dir} [skills]")
    if config.mode == "restricted":
        home, tmp = sandbox_state_dirs(config)
        lines.append(f"  RW  /home  <- {home} [state]")
        lines.append(f"  RW  /tmp  <- {tmp} [state]")
        for path in (*RUNTIME_PATHS, *RUNTIME_FILES):
            if Path(path).exists():
                lines.append(f"  RO  {path}  <- {path} [runtime]")
        for path in NETWORK_FILES:
            if Path(path).exists():
                lines.append(f"  RO  {path}  <- {path} [runtime, online-only]")
    else:
        lines.append("  RW  /  <- / [host filesystem, then protections below]")
        config_dir = sandbox_config_dir(config)
        lines.append(f"  --  {config_dir} [config hidden]")
        lines.append("  RO  agent install paths [install]")
    for mount in read_only:
        lines.append(f"  RO  {mount.destination}  <- {mount.source} [user]")
    host_dev = any(mount.dev_bind and mount.destination.rstrip("/") == "/dev" for mount in read_write)
    for mount in read_write:
        kind = "host dev-bind" if mount.dev_bind else "user"
        lines.append(f"  RW  {mount.destination}  <- {mount.source} [{kind}]")
    rg = rg_path if rg_path is not None else resolve_rg()
    if rg:
        lines.append(f"  RO  {SEARCH_BINARY_PATH}  <- {rg} [internal]")
    lines.append("  --  /proc  [private]")
    if not host_dev:
        lines.append("  --  /dev  [private]")
    lines.extend(["", "User mounts:"])
    user = [*read_only, *read_write]
    if user:
        for mount in user:
            access = "RW" if mount in read_write else "RO"
            scope = "both modes" if mount.modes is None else ",".join(mount.modes)
            lines.append(f"  {access}  {mount.destination}  <- {mount.source} [{scope}]")
    else:
        lines.append("  None")
    lines.extend(["", "File-tool access:"])
    lines.append(f"  RW  {workspace}")
    if skills_dir is not None:
        lines.append(f"  RW  {SKILLS_ROOT}")
    for mount in read_only:
        lines.append(f"  RO  {mount.destination}")
    for mount in read_write:
        lines.append(f"  RW  {mount.destination}")
    if config.mode == "full-access":
        lines.append("  RW  other host paths, except the hidden config directory and read-only install paths")
    else:
        lines.append("  --  runtime directories such as /usr and /etc are execute-only")
    lines.extend([
        "",
        "Network:",
        "  Offline instance: network namespace",
        "  Online instance: host network",
    ])
    return "\n".join(lines)
