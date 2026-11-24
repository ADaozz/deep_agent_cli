"""Initialize the CLI's user-owned configuration on first launch."""

from __future__ import annotations

import os
from importlib.resources import abc, files
from pathlib import Path
import tempfile

from agent.bootstrap.config import default_skills_dir, require_outside_workspace
from agent.companion import STATES


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
    # Creating config.yaml and filling missing companion prompts/GIFs are
    # independent: an older config must still receive newly required assets.
    initialize_companion_files(root)
    if config.exists() or config.is_symlink():
        if not config.is_file():
            raise ValueError(f"configuration path must be a file: {config}")
        return None
    template = files("agent").joinpath("config.example.yaml")
    return config if _write_default(config, template) else None


def initialize_companion_files(config_dir: Path) -> None:
    source = files("agent.companion")
    companion_dir = config_dir / "companion"
    prompts = companion_dir / "prompts"
    gifs = companion_dir / "gifs"
    for directory in (companion_dir, prompts, gifs):
        if directory.is_symlink():
            raise ValueError(f"Companion directory must not be a symlink: {directory}")
        directory.mkdir(mode=0o700, exist_ok=True)
    for name in ("persona.md", "chat.md"):
        _write_default(prompts / name, source.joinpath(name))
    for state in STATES:
        name = f"lain_{state}.gif"
        _write_default(gifs / name, source.joinpath("assets", name))


def _write_default(destination: Path, source: abc.Traversable) -> bool:
    if destination.exists() or destination.is_symlink():
        if not destination.is_file():
            raise ValueError(f"configuration asset must be a file: {destination}")
        return False
    with tempfile.NamedTemporaryFile(mode="wb", dir=destination.parent, prefix=".config-", delete=False) as temporary:
        temp_path = Path(temporary.name)
        try:
            os.chmod(temp_path, 0o600)
            temporary.write(source.read_bytes())
            temporary.flush()
            os.fsync(temporary.fileno())
            try:
                os.link(temp_path, destination)
            except FileExistsError:
                if not destination.is_file():
                    raise ValueError(f"configuration asset must be a file: {destination}") from None
                return False
        finally:
            temp_path.unlink(missing_ok=True)
    return True
