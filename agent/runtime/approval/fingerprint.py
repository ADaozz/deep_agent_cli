"""Approval environment fingerprint (v2)."""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from agent.execution.environment import network_rule
from agent.execution.layout import sandbox_config_dir
from agent.execution.mounts import effective_user_mounts
from agent.execution.policy import sandbox_workspace_root
from agent.runtime.permissions import PermissionMode

FINGERPRINT_VERSION = 2


def _hash_value(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def build_policy(
    *,
    backend: Any,
    sandbox_config: Any,
    ask_only: bool,
    trust_env: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (classifier context, policy fingerprint). Environment secret values are hashed only."""
    if sandbox_config is None:
        return None, None
    read_only, read_write = effective_user_mounts(sandbox_config)
    context = {
        "workspace": sandbox_workspace_root(sandbox_config.workspace),
        "sandbox": sandbox_config.mode,
        "network_policy": network_rule(PermissionMode.ASK if ask_only else PermissionMode.AUTO),
        "read_write_mounts": [str(m.destination) for m in read_write],
        "env_allowlist": sorted(sandbox_config.env_allowlist),
        "trust_env": bool(trust_env),
        "protected": sorted(str(p.expanduser().resolve()) for p in sandbox_config.protected_files),
    }
    skills = getattr(backend, "skills_dir", None)
    env_set_hashes = {
        key: _hash_value(str(value))
        for key, value in sorted((sandbox_config.env_set or {}).items())
    }
    policy = {
        "version": FINGERPRINT_VERSION,
        **context,
        "backend": type(backend).__qualname__,
        "mounts": [
            (access, str(m.source.expanduser().resolve()), str(m.destination), m.dev_bind)
            for access, mounts in (("ro", read_only), ("rw", read_write))
            for m in mounts
        ],
        "skills": str(skills.resolve()) if skills else None,
        "config_dir": str(sandbox_config_dir(sandbox_config)),
        "env_set": env_set_hashes,
    }
    return context, sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
