"""Server-only configuration; callers select IDs rather than filesystem paths."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import re

import yaml

from agent.config import Settings, require_outside_workspace


@dataclass(frozen=True)
class ServerConfig:
    projects: dict[str, Path]
    model_config: Path
    state_dir: Path
    token: str = field(repr=False)
    max_runs: int = 2
    source_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "projects", {
            key: Path(value).expanduser().resolve() for key, value in self.projects.items()
        })
        object.__setattr__(self, "model_config", Path(self.model_config).expanduser().resolve())
        object.__setattr__(self, "state_dir", Path(self.state_dir).expanduser().resolve())
        if not isinstance(self.token, str) or not self.token.strip():
            raise ValueError("A nonempty service token is required")
        if type(self.max_runs) is not int or self.max_runs < 1:
            raise ValueError("max_runs must be a positive integer")
        if not self.projects:
            raise ValueError("At least one project is required")
        roots = list(self.projects.values())
        for key, root in self.projects.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key):
                raise ValueError("Project IDs must contain only letters, digits, _ or -")
            if not root.is_dir():
                raise ValueError(f"Project {key} must reference an existing directory")
            for path in (self.model_config, self.state_dir, self.source_path):
                if path is not None:
                    require_outside_workspace(Path(path), root, label="service configuration/storage")
            if any(other != root and (root.is_relative_to(other) or other.is_relative_to(root))
                   for other in roots) or roots.count(root) > 1:
                raise ValueError("Project workspaces must not overlap")

    @classmethod
    def load(cls, path: str | Path) -> ServerConfig:
        source = Path(path).expanduser().resolve()
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Server configuration must be a mapping")
        unknown = set(raw) - {"projects", "model_config", "state_dir", "token_env", "max_runs"}
        if unknown:
            raise ValueError(f"Unknown server configuration keys: {', '.join(sorted(unknown))}")
        projects = raw.get("projects")
        if not isinstance(projects, dict) or not projects:
            raise ValueError("projects must map project IDs to workspace directories")

        def resolve(value: str) -> Path:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Configuration paths must be nonempty strings")
            result = Path(value).expanduser()
            return (source.parent / result).resolve() if not result.is_absolute() else result.resolve()

        token_env = raw.get("token_env", "DEEP_AGENT_SERVICE_TOKEN")
        if not isinstance(token_env, str) or not token_env:
            raise ValueError("token_env must be an environment variable name")
        return cls(
            projects={key: resolve(value) for key, value in projects.items()},
            model_config=resolve(raw.get("model_config")),
            state_dir=resolve(raw.get("state_dir", "server-state")),
            token=os.environ.get(token_env, ""),
            max_runs=raw.get("max_runs", 2), source_path=source,
        )

    def settings_for(self, project_id: str) -> Settings:
        root = self.projects[project_id]
        settings = Settings.load(self.model_config)
        for path in (settings.config_dir or Path.home() / ".deep-agent",):
            # Skills and runtime settings must remain outside every writable project.
            for workspace in self.projects.values():
                require_outside_workspace(path, workspace, label="agent config directory")
        return replace(
            settings, sandbox=replace(settings.sandbox, workspace=root, allow_unsandboxed=False),
            state_path=self.state_dir / "projects" / project_id / "sessions.sqlite3",
        )
