"""Workspace Git metadata and read-only file diffs; no snapshots."""
from __future__ import annotations

import os
from pathlib import Path
import re
import signal
import subprocess
import threading

from agent.cli.gitinfo import parse_status, TIMEOUT_SECONDS

GIT_BIN = 'git'
EMPTY_TREE_FALLBACK = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
SCOPES = ('all', 'staged', 'unstaged')
STATUS_BYTES = 2 * 1024 * 1024
PREVIEW_DIFF_BYTES = 64 * 1024
FULL_DIFF_BYTES = 1024 * 1024
PREVIEW_DIFF_LINES = 200
FULL_DIFF_LINES = 8000


class GitRequestError(ValueError):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def _env() -> dict[str, str]:
    return {**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'LC_ALL': 'C', 'GIT_PAGER': 'cat'}


def _kill(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except OSError:
            pass


def _run_git(workspace: Path, args: list[str], limit: int) -> tuple[int, bytes, bool]:
    """Run one git command, stopping it when time or output exceeds the limit."""
    process = subprocess.Popen(
        [GIT_BIN, '--no-optional-locks', *args], cwd=workspace,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_env(), start_new_session=True,
    )
    timed_out = False

    def expire() -> None:
        nonlocal timed_out
        timed_out = True
        _kill(process)

    timer = threading.Timer(TIMEOUT_SECONDS, expire)
    timer.start()
    chunks: list[bytes] = []
    size = 0
    truncated = False
    try:
        assert process.stdout is not None and process.stderr is not None
        while not timed_out:
            block = process.stdout.read(65536)
            if not block:
                break
            room = limit - size
            if len(block) > room:
                chunks.append(block[:room])
                truncated = True
                _kill(process)
                break
            chunks.append(block)
            size += len(block)
        if not timed_out:
            process.stderr.read(65536)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _kill(process)
            process.wait(timeout=1)
    finally:
        timer.cancel()
        if process.poll() is None:
            _kill(process)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
    if timed_out and not truncated and process.returncode not in (0, 1):
        raise subprocess.TimeoutExpired([GIT_BIN, *args], TIMEOUT_SECONDS)
    code = process.returncode if process.returncode is not None else 1
    if truncated and code < 0:
        code = 0
    return code, b''.join(chunks), truncated


def _decode(payload: bytes) -> str:
    return payload.decode('utf-8', 'replace')


def _status_name(code: str) -> str:
    if code == '??':
        return 'untracked'
    if 'R' in code:
        return 'renamed'
    if 'C' in code:
        return 'copied'
    if 'D' in code:
        return 'deleted'
    if 'A' in code:
        return 'added'
    return 'modified'


def _porcelain(workspace: Path) -> list[dict] | None:
    code, payload, truncated = _run_git(
        workspace, ['status', '--porcelain=v1', '-z', '--untracked-files=all'], STATUS_BYTES,
    )
    if code != 0 or truncated:
        return None
    entries = iter(payload.split(b'\0'))
    rows = []
    for item in entries:
        if not item:
            continue
        status = item[:2].decode('utf-8', 'replace')
        path = _decode(item[3:])
        old = None
        if 'R' in status or 'C' in status:
            original = next(entries, None)
            if original:
                old = _decode(original)
        index, worktree = status[0], status[1]
        untracked = status == '??'
        rows.append({
            'path': path,
            'old_path': old,
            'status': _status_name(status),
            'staged': (not untracked) and index not in ' ?!',
            'unstaged': (not untracked) and worktree not in ' ?!',
            'untracked': untracked,
        })
    return rows


def _numstat(workspace: Path, head: str | None) -> dict[str, dict] | None:
    if not head:
        return {'files': {}, 'added': None, 'removed': None}
    code, payload, truncated = _run_git(
        workspace,
        ['diff', '--no-ext-diff', '--no-textconv', '-M', '--numstat', '-z', 'HEAD', '--'],
        STATUS_BYTES,
    )
    if code not in (0, 1) or truncated:
        return None
    files: dict[str, dict] = {}
    added = removed = 0
    parts = payload.split(b'\0')
    index = 0
    while index < len(parts):
        record = parts[index]
        if not record:
            index += 1
            continue
        plus, minus, path = record.split(b'\t', 2)
        old_path = None
        if path == b'':
            # Rename records are `added\tremoved\t\0old\0new\0`.
            if index + 2 >= len(parts):
                break
            old_path = _decode(parts[index + 1])
            name = _decode(parts[index + 2])
            index += 3
        else:
            name = _decode(path)
            index += 1
        binary = plus == b'-' or minus == b'-'
        plus_count, minus_count = (None, None) if binary else (int(plus), int(minus))
        files[name] = {
            'path': name, 'added': plus_count, 'removed': minus_count,
            'status': 'renamed' if old_path else 'modified', 'binary': binary,
            'staged': False, 'unstaged': False, 'untracked': False, 'old_path': old_path,
        }
        if not binary and plus_count is not None and minus_count is not None:
            added += plus_count
            removed += minus_count
    return {'files': files, 'added': added, 'removed': removed}


def workspace_relative(workspace: Path, raw: str) -> str:
    """Return a workspace-relative path, rejecting anything that escapes it."""
    if not isinstance(raw, str) or not raw or raw in ('.', '/'):
        raise GitRequestError('文件路径必须位于目标 workspace 内', 422)
    if '\x00' in raw or raw.startswith('/') or raw.startswith('\\'):
        raise GitRequestError('文件路径必须位于目标 workspace 内', 422)
    candidate = Path(raw)
    if candidate.is_absolute():
        raise GitRequestError('文件路径必须位于目标 workspace 内', 422)
    parts: list[str] = []
    for part in candidate.parts:
        if part in ('', '.'):
            continue
        if part == '..' or '/' in part or '\x00' in part:
            raise GitRequestError('文件路径必须位于目标 workspace 内', 422)
        parts.append(part)
    if not parts:
        raise GitRequestError('文件路径必须位于目标 workspace 内', 422)
    root = workspace.resolve()
    current = root
    for index, part in enumerate(parts):
        current = current / part
        last = index == len(parts) - 1
        try:
            link = current.is_symlink()
            resolved = current.resolve() if link or current.exists() else None
        except OSError as exc:
            raise GitRequestError('文件路径必须位于目标 workspace 内', 422) from exc
        if resolved is not None and resolved.is_dir() and last:
            raise GitRequestError('只能读取文件差异', 422)
        if link:
            # A symlink may point outside, but only the final path is accepted and never followed.
            if resolved is not None and not last and not resolved.is_relative_to(root):
                raise GitRequestError('文件路径必须位于目标 workspace 内', 422)
            continue
        if resolved is not None and not resolved.is_relative_to(root):
            raise GitRequestError('文件路径必须位于目标 workspace 内', 422)
    return '/'.join(parts)


def _empty_tree(workspace: Path) -> str | None:
    try:
        result = subprocess.run(
            [GIT_BIN, 'hash-object', '-t', 'tree', '--stdin'], cwd=workspace, input=b'',
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT_SECONDS, env=_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return EMPTY_TREE_FALLBACK
    text = result.stdout.decode().strip()
    return text or EMPTY_TREE_FALLBACK if result.returncode == 0 else EMPTY_TREE_FALLBACK


_HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def _hunks(patch: str, line_limit: int) -> tuple[list[dict], bool, bool]:
    hunks: list[dict] = []
    current: dict | None = None
    old_no = new_no = 0
    kept = 0
    truncated = False
    binary = False
    for line in patch.splitlines():
        if line.startswith('diff --git '):
            if current is not None:
                hunks.append(current)
            current = None
            continue
        if line.startswith('Binary files ') or line.startswith('GIT binary patch'):
            binary = True
            current = None
            continue
        match = _HUNK.match(line)
        if match:
            if current is not None:
                hunks.append(current)
            if kept >= line_limit:
                truncated = True
                current = None
                break
            old_no = int(match.group(1))
            new_no = int(match.group(3))
            current = {
                'header': line,
                'old_start': old_no,
                'old_count': int(match.group(2) or '1'),
                'new_start': new_no,
                'new_count': int(match.group(4) or '1'),
                'lines': [],
            }
            continue
        if current is None:
            continue
        if kept >= line_limit:
            truncated = True
            break
        kind = 'context'
        text = line[1:] if line[:1] in '+- ' else line
        old_line = new_line = None
        if line.startswith('+'):
            kind, new_line = 'add', new_no
            new_no += 1
        elif line.startswith('-'):
            kind, old_line = 'delete', old_no
            old_no += 1
        elif line.startswith('\\'):
            kind, text = 'meta', line
        else:
            old_line, new_line = old_no, new_no
            old_no += 1
            new_no += 1
        current['lines'].append({'kind': kind, 'text': text, 'old_no': old_line, 'new_no': new_line})
        kept += 1
    if current is not None:
        hunks.append(current)
    return hunks, binary, truncated


def _diff_args(scope: str, path: str, old: str | None, *, head: str | None, untracked: bool, tree: str | None) -> list[str]:
    prefix = ['--literal-pathspecs', 'diff']
    flags = ['--no-ext-diff', '--no-textconv', '--no-color', '-M', '--unified=3']
    if untracked:
        return [*prefix, '--no-index', *flags, '--', '/dev/null', path]
    pair = [old, path] if old and scope != 'unstaged' else [path]
    if scope == 'staged':
        return [*prefix, '--cached', *flags, '--', *pair]
    if scope == 'all':
        base = head or tree or EMPTY_TREE_FALLBACK
        return [*prefix, base, *flags, '--', *pair]
    return [*prefix, *flags, '--', *pair]


def file_diff(workspace: Path, path: str, scope: str = 'all', *, full: bool = False) -> dict:
    if scope not in SCOPES:
        raise GitRequestError('差异范围无效', 422)
    relative = workspace_relative(workspace, path)
    try:
        head_result = _run_git(workspace, ['rev-parse', '--verify', 'HEAD'], 128)
        if head_result[0] not in (0, 1) and head_result[0] != 128:
            return {'available': False}
        head = _decode(head_result[1]).strip() if head_result[0] == 0 else None
        rows = _porcelain(workspace)
        if rows is None:
            return {'available': False}
        row = next((item for item in rows if item['path'] == relative), None)
        if row is None or (scope == 'staged' and not row['staged']) or (scope == 'unstaged' and not row['unstaged'] and not row['untracked']):
            raise GitRequestError('该范围没有此文件的变更', 404)
        candidate = workspace / relative
        if row['untracked'] and candidate.is_symlink():
            return {
                'available': True, 'path': relative, 'scope': scope, 'status': row['status'],
                'old_path': row['old_path'], 'binary': False, 'symlink': True,
                'target': os.readlink(candidate), 'untracked': True, 'truncated': False, 'hunks': [],
            }
        tree = _empty_tree(workspace) if head is None and scope == 'all' and not row['untracked'] else None
        limit = FULL_DIFF_BYTES if full else PREVIEW_DIFF_BYTES
        code, payload, cut = _run_git(
            workspace,
            _diff_args(scope, relative, row['old_path'], head=head, untracked=row['untracked'], tree=tree),
            limit,
        )
        if code not in (0, 1):
            return {'available': False}
        hunks, binary, line_cut = _hunks(_decode(payload), FULL_DIFF_LINES if full else PREVIEW_DIFF_LINES)
        return {
            'available': True, 'path': relative, 'scope': scope, 'status': row['status'],
            'old_path': row['old_path'], 'binary': binary, 'symlink': False, 'target': None,
            'untracked': row['untracked'], 'truncated': cut or line_cut,
            'hunks': [] if binary else hunks,
        }
    except GitRequestError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError):
        return {'available': False}


def workspace_git(workspace: Path) -> dict:
    try:
        status = _run_git(workspace, ['status', '--porcelain', '--branch'], STATUS_BYTES)
        if status[0] != 0 or status[2]:
            return {'available': False}
        summary = parse_status(_decode(status[1]))
        head_result = _run_git(workspace, ['rev-parse', '--verify', 'HEAD'], 128)
        head = _decode(head_result[1]).strip() if head_result[0] == 0 else None
        stats = _numstat(workspace, head)
        if stats is None:
            return {'available': False}
        files: dict[str, dict] = stats['files']
        rows = _porcelain(workspace)
        if rows is None:
            return {'available': False}
        present = {row['path'] for row in rows}
        for row in rows:
            item = files.setdefault(row['path'], {
                'path': row['path'], 'added': None, 'removed': None, 'binary': False,
            })
            item.update({
                'status': row['status'], 'staged': row['staged'], 'unstaged': row['unstaged'],
                'untracked': row['untracked'], 'old_path': row['old_path'],
            })
            source = row['old_path']
            if source and source not in present and source != row['path']:
                files.pop(source, None)
        for item in files.values():
            item.setdefault('staged', False)
            item.setdefault('unstaged', False)
            item.setdefault('untracked', False)
            item.setdefault('old_path', None)
        default = _run_git(workspace, ['symbolic-ref', '--short', 'refs/remotes/origin/HEAD'], 1024)
        base = _decode(default[1]).strip() if default[0] == 0 else None
        if not base:
            for candidate in ('main', 'master'):
                if _run_git(workspace, ['show-ref', '--verify', '--quiet', f'refs/heads/{candidate}'], 64)[0] == 0:
                    base = candidate
                    break
        merged = None
        if head and base and summary.branch not in ('detached', base, base.removeprefix('origin/')):
            result = _run_git(workspace, ['merge-base', '--is-ancestor', 'HEAD', base], 64)
            if result[0] in (0, 1):
                merged = result[0] == 0
        branches_result = _run_git(workspace, ['for-each-ref', '--format=%(refname:short)', 'refs/heads/'], STATUS_BYTES)
        branches = _decode(branches_result[1]).splitlines() if branches_result[0] == 0 and not branches_result[2] else []
        if not head and summary.branch not in branches:
            branches.insert(0, summary.branch)
        return {
            'available': True, 'branches': branches, 'branch': summary.branch, 'changed': summary.changed,
            'head': head, 'base_branch': base, 'merged': merged,
            'added': stats['added'], 'removed': stats['removed'], 'files': list(files.values()),
        }
    except (OSError, subprocess.SubprocessError, ValueError):
        return {'available': False}


def switch_branch(workspace: Path, branch: str) -> dict:
    info = workspace_git(workspace)
    if not info.get('available') or branch not in info.get('branches', []):
        raise ValueError('该 workspace 中没有此本地分支')
    if info['branch'] == branch:
        return info
    try:
        result = subprocess.run(
            [GIT_BIN, 'switch', '--', branch], cwd=workspace,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT_SECONDS, env=_env(),
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError('无法切换分支，请检查 Git 状态') from None
    if result.returncode:
        raise ValueError('Git 未能切换分支，请先处理工作区冲突或被其他 worktree 占用的分支；现有修改已保留')
    return workspace_git(workspace)
