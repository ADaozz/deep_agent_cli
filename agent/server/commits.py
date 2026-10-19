"""Bounded, read-only commit history for the Web console."""
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from agent.server.gitinfo import GitRequestError, STATUS_BYTES, _decode, _run_git


def _github_repository(workspace: Path) -> str | None:
    code, payload, cut = _run_git(workspace, ['remote', 'get-url', 'origin'], 4096)
    if code or cut:
        return None
    remote = _decode(payload).strip()
    if remote.startswith('git@github.com:'):
        path = remote.removeprefix('git@github.com:')
    else:
        url = urlsplit(remote)
        if url.scheme not in ('https', 'ssh') or url.hostname != 'github.com':
            return None
        path = url.path.lstrip('/')
    path = path.removesuffix('.git').rstrip('/')
    return f'https://github.com/{path}' if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', path) else None


def commit_history(workspace: Path, limit: int = 20, cursor: str | None = None) -> dict:
    if not 1 <= limit <= 50:
        raise GitRequestError('提交数量必须在 1 到 50 之间', 422)
    offset = 0
    if cursor:
        match = re.fullmatch(r'([0-9a-f]{40}|[0-9a-f]{64}):([0-9]{1,7})', cursor)
        if not match:
            raise GitRequestError('无效的提交分页游标', 422)
        head, raw_offset = match.groups()
        offset = int(raw_offset)
    else:
        head = 'HEAD'
    try:
        code, payload, cut = _run_git(workspace, ['rev-parse', '--verify', head], 128)
        if code or cut:
            inside = _run_git(workspace, ['rev-parse', '--is-inside-work-tree'], 128)
            if not cursor and inside[0] == 0:
                return {'available': True, 'commits': [], 'next_cursor': None}
            return {'available': False, 'commits': [], 'next_cursor': None}
        head = _decode(payload).strip()
        code, payload, cut = _run_git(workspace, [
            'log', '-z', '--no-show-signature', f'--max-count={limit + 1}', f'--skip={offset}',
            '--format=%H%x00%P%x00%an%x00%aI%x00%cI%x00%B', head, '--',
        ], STATUS_BYTES)
        if code or cut:
            return {'available': False, 'commits': [], 'next_cursor': None}
        fields = payload.split(b'\0')
        if fields[-1:] == [b'']:
            fields.pop()
        if len(fields) % 6:
            return {'available': False, 'commits': [], 'next_cursor': None}
        records = [fields[index:index + 6] for index in range(0, len(fields), 6)]
        repository = _github_repository(workspace)
        commits = []
        for raw_sha, raw_parents, raw_author, raw_authored, raw_committed, raw_message in records[:limit]:
            sha = _decode(raw_sha)
            parents = _decode(raw_parents).split()
            args = (['diff', parents[0], sha] if parents else ['diff-tree', '--root', '--no-commit-id', '-r', sha])
            code, stats, cut = _run_git(workspace, [*args, '--numstat', '-z', '--no-renames', '--no-ext-diff', '--no-textconv', '--'], STATUS_BYTES)
            added = removed = count = 0
            for row in stats.split(b'\0') if code == 0 and not cut else []:
                if not row:
                    continue
                plus, minus, _ = row.split(b'\t', 2)
                count += 1
                if plus != b'-' and minus != b'-':
                    added += int(plus)
                    removed += int(minus)
            message = _decode(raw_message).strip()
            commits.append({
                'sha': sha, 'subject': message.split('\n', 1)[0], 'message': message,
                'author': _decode(raw_author), 'authored_at': _decode(raw_authored),
                'committed_at': _decode(raw_committed),
                'added': added if code == 0 and not cut else None,
                'removed': removed if code == 0 and not cut else None,
                'files': count if code == 0 and not cut else None,
                'url': f'{repository}/commit/{sha}' if repository else None,
            })
        return {'available': True, 'commits': commits,
                'next_cursor': f'{head}:{offset + limit}' if len(records) > limit else None}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {'available': False, 'commits': [], 'next_cursor': None}
