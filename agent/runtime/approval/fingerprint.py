"""Approval environment: what the reviewer sees, and the fingerprint a grant is bound to."""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from agent.execution.environment import network_rule
from agent.execution.layout import sandbox_config_dir
from agent.execution.mounts import effective_user_mounts
from agent.execution.policy import sandbox_workspace_root
from agent.runtime.permissions import PermissionMode


def build_policy(
    *,
    backend: Any,
    sandbox_config: Any,
    ask_only: bool,
    trust_env: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (reviewer environment, fingerprint). Environment secret values enter the fingerprint as hashes only."""
    if sandbox_config is None:
        return None, None
    read_only, read_write = effective_user_mounts(sandbox_config)
    environment = {
        "workspace": sandbox_workspace_root(sandbox_config.workspace),
        "sandbox": sandbox_config.mode,
        "network_policy": network_rule(PermissionMode.ASK if ask_only else PermissionMode.AUTO),
        "read_write_mounts": [str(m.destination) for m in read_write],
    }
    skills = getattr(backend, "skills_dir", None)
    policy = {
        **environment,
        "env_allowlist": sorted(sandbox_config.env_allowlist),
        "trust_env": bool(trust_env),
        "protected": sorted(str(p.expanduser().resolve()) for p in sandbox_config.protected_files),
        "backend": type(backend).__qualname__,
        "mounts": [
            (access, str(m.source.expanduser().resolve()), str(m.destination), m.dev_bind)
            for access, mounts in (("ro", read_only), ("rw", read_write))
            for m in mounts
        ],
        "skills": str(skills.resolve()) if skills else None,
        "config_dir": str(sandbox_config_dir(sandbox_config)),
        "env_set": {
            key: sha256(str(value).encode()).hexdigest()
            for key, value in sorted((sandbox_config.env_set or {}).items())
        },
    }
    return environment, sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
