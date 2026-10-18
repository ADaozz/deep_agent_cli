"""Workspace catalog and folder selection for the local presentation layer."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile

from agent.server.config import ServerConfig


def _saved(config: ServerConfig) -> dict[str, str]:
    path = config.state_dir / 'workspaces.json'
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if data.get('version') != 1 or not isinstance(data.get('workspaces'), dict):
        raise ValueError('Invalid workspace catalog')
    return data['workspaces']


def restore_workspaces(config: ServerConfig) -> None:
    for project_id, path in _saved(config).items():
        if project_id in config.projects:
            continue
        try:
            candidate = replace(config, projects={**config.projects, project_id: Path(path)})
        except (OSError, ValueError, TypeError):
            continue  # A removed/moved folder does not stop other workspaces.
        config.projects.update(candidate.projects)


def prepare_workspace(config: ServerConfig, value: str) -> tuple[str, ServerConfig]:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError('请选择已存在的文件夹')
    for project_id, current in config.projects.items():
        if current == path:
            return project_id, config
    project_id = 'ws_' + hashlib.sha256(str(path).encode()).hexdigest()[:24]
    candidate = replace(config, projects={**config.projects, project_id: path})
    return project_id, candidate


def save_workspace(config: ServerConfig, project_id: str, path: Path) -> None:
    saved = {**_saved(config), project_id: str(path)}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=config.state_dir, prefix='.workspaces-', delete=False) as file:
            temporary = file.name
            json.dump({'version': 1, 'workspaces': saved}, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, config.state_dir / 'workspaces.json')
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def directories(value: str | None) -> dict:
    path = Path(value).expanduser().resolve(strict=True) if value else Path.cwd().resolve()
    if not path.is_dir():
        raise ValueError('请选择已存在的文件夹')
    entries = []
    for child in path.iterdir():
        try:
            if child.is_dir() and not child.name.startswith('.'):
                entries.append({'name': child.name, 'path': str(child.resolve())})
        except OSError:
            continue
    entries.sort(key=lambda item: item['name'].casefold())
    return {'path': str(path), 'parent': str(path.parent) if path.parent != path else None,
            'directories': entries[:500], 'truncated': len(entries) > 500}
