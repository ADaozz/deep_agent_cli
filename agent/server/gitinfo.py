"""Workspace Git metadata and explicit native branch selection; no snapshots."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

from agent.cli.gitinfo import parse_status, TIMEOUT_SECONDS


def workspace_git(workspace: Path) -> dict:
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ['git', '--no-optional-locks', *args], cwd=workspace,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT_SECONDS,
            env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'LC_ALL': 'C'},
        )
    try:
        status = git('status', '--porcelain', '--branch')
        if status.returncode:
            return {'available': False}
        # Branch and changed count follow the exact parser used by main's CLI.
        summary = parse_status(status.stdout.decode('utf-8', 'replace'))
        head_result = git('rev-parse', '--verify', 'HEAD')
        head = head_result.stdout.decode().strip() if head_result.returncode == 0 else None
        files = {}
        added = removed = 0 if head else None
        if head:
            stats = git('diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--numstat', '-z', 'HEAD', '--')
            if stats.returncode:
                return {'available': False}
            for record in stats.stdout.split(b'\0'):
                if not record:
                    continue
                plus, minus, path = record.split(b'\t', 2)
                binary = plus == b'-' or minus == b'-'
                a, r = (None, None) if binary else (int(plus), int(minus))
                name = path.decode('utf-8', 'replace')
                files[name] = {'path': name, 'added': a, 'removed': r, 'status': 'modified', 'binary': binary}
                if not binary:
                    added += a
                    removed += r
        raw = git('status', '--porcelain=v1', '-z')
        if raw.returncode:
            return {'available': False}
        entries = iter(raw.stdout.split(b'\0'))
        for item in entries:
            if not item:
                continue
            code, path = item[:2].decode(), item[3:].decode('utf-8', 'replace')
            if 'R' in code or 'C' in code:
                next(entries, None)  # Original name, not another status entry.
            row = files.setdefault(path, {'path': path, 'added': None, 'removed': None, 'binary': False})
            row['status'] = 'untracked' if code == '??' else 'deleted' if 'D' in code else 'added' if 'A' in code else 'renamed' if 'R' in code else 'modified'
        default = git('symbolic-ref', '--short', 'refs/remotes/origin/HEAD')
        base = default.stdout.decode().strip() if default.returncode == 0 else None
        if not base:
            for candidate in ('main', 'master'):
                if git('show-ref', '--verify', '--quiet', f'refs/heads/{candidate}').returncode == 0:
                    base = candidate
                    break
        merged = None
        if head and base and summary.branch not in ('detached', base, base.removeprefix('origin/')):
            result = git('merge-base', '--is-ancestor', 'HEAD', base)
            if result.returncode in (0, 1):
                merged = result.returncode == 0
        branches_result = git('for-each-ref', '--format=%(refname:short)', 'refs/heads/')
        branches = branches_result.stdout.decode('utf-8', 'replace').splitlines() if branches_result.returncode == 0 else []
        if not head and summary.branch not in branches:
            branches.insert(0, summary.branch)
        return {'available': True, 'branches': branches, 'branch': summary.branch, 'changed': summary.changed,
                'head': head, 'base_branch': base, 'merged': merged,
                'added': added, 'removed': removed, 'files': list(files.values())}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {'available': False}


def switch_branch(workspace: Path, branch: str) -> dict:
    info = workspace_git(workspace)
    if not info.get('available') or branch not in info.get('branches', []):
        raise ValueError('该 workspace 中没有此本地分支')
    if info['branch'] == branch:
        return info
    try:
        result = subprocess.run(['git', 'switch', '--', branch], cwd=workspace,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT_SECONDS,
                                env={**os.environ, 'LC_ALL': 'C'})
    except (OSError, subprocess.SubprocessError):
        raise ValueError('无法切换分支，请检查 Git 状态') from None
    if result.returncode:
        raise ValueError('Git 未能切换分支，请先处理工作区冲突或被其他 worktree 占用的分支；现有修改已保留')
    return workspace_git(workspace)
