"""Initialize the CLI's user-owned configuration on first launch."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

from agent.config import default_skills_dir, require_outside_workspace


def initialize_user_files(*, workspace: Path | None = None) -> Path | None:
    """Create missing default files; return config path only when newly created."""
    work = (workspace or Path.cwd()).resolve()
    skills = default_skills_dir()
    root = skills.parent
    config = root / "config.yaml"
    require_outside_workspace(root, work, label="configuration directory")
    require_outside_workspace(skills, work, label="skills directory")
    if root.is_symlink() or skills.is_symlink():
        raise ValueError("configuration and skills directories must not be symlinks")
    root.mkdir(mode=0o700, exist_ok=True)
    skills.mkdir(mode=0o700, exist_ok=True)
    if os.environ.get("DEEP_AGENT_CONFIG"):
        return None
    if config.exists() or config.is_symlink():
        if not config.is_file():
            raise ValueError(f"configuration path must be a file: {config}")
        return None
    template = Path(__file__).resolve().parents[1] / "config.example.yaml"
    with tempfile.NamedTemporaryFile(mode="wb", dir=root, prefix=".config-", delete=False) as temporary:
        temp_path = Path(temporary.name)
        try:
            os.chmod(temp_path, 0o600)
            temporary.write(template.read_bytes())
            temporary.flush()
            os.fsync(temporary.fileno())
            try:
                os.link(temp_path, config)
            except FileExistsError:
                return None
        finally:
            temp_path.unlink(missing_ok=True)
    return config
