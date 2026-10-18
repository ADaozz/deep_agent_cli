"""Git presentation uses real workspace state and never creates checkpoints."""
import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.server.gitinfo import GitRequestError, file_diff, workspace_git
from agent.server.app import create_app
from agent.server.config import ServerConfig


def git(root, *args):
    return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *args], cwd=root, check=True, capture_output=True).stdout.decode().strip()


def repository(root):
    root.mkdir()
    git(root, 'init', '-b', 'main')
    (root/'file.txt').write_text('before\n')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'base')


def test_git_stats_include_index_and_worktree_and_do_not_write(tmp_path):
    root=tmp_path/'repo';repository(root)
    git(root,'switch','-c','feature')
    (root/'file.txt').write_text('after\nsecond\n')
    git(root,'add','file.txt')
    (root/'file.txt').write_text('after\nsecond\nthird\n')
    (root/'untracked.txt').write_text('untracked contents\n')
    index_before=(root/'.git/index').read_bytes()
    head_before=git(root,'rev-parse','HEAD')
    info=workspace_git(root)
    assert info['available'] and info['branch']=='feature'
    assert (info['added'],info['removed'])==(3,1)
    assert info['changed']==2
    tracked=next(f for f in info['files'] if f['path']=='file.txt')
    assert tracked['staged'] and tracked['unstaged'] and not tracked['untracked']
    untracked=next(f for f in info['files'] if f['path']=='untracked.txt')
    assert untracked['added'] is None and untracked['untracked'] and not untracked['staged']
    assert (root/'.git/index').read_bytes()==index_before
    assert git(root,'rev-parse','HEAD')==head_before
    assert not (root/'.git/refs/deep-agent').exists()


def test_merged_flag_requires_actual_ancestry_and_is_absent_on_base_branch(tmp_path):
    root=tmp_path/'repo';repository(root)
    assert workspace_git(root)['merged'] is None
    git(root,'switch','-c','feature')
    (root/'feature.txt').write_text('feature\n')
    git(root,'add','.');git(root,'commit','-m','feature')
    assert workspace_git(root)['merged'] is False
    git(root,'switch','main');git(root,'merge','--no-ff','feature','-m','merged')
    git(root,'switch','feature')
    assert workspace_git(root)['merged'] is True
    git(root,'checkout','--detach')
    assert workspace_git(root)['branch']=='detached'
    assert workspace_git(root)['merged'] is None


def test_non_repository_and_unborn_repository(tmp_path):
    assert workspace_git(tmp_path)=={'available':False}
    git(tmp_path,'init','-b','main')
    (tmp_path/'first.txt').write_text('first\n')
    info=workspace_git(tmp_path)
    assert info['available'] and info['branch']=='main'
    assert info['added'] is None and info['removed'] is None
    assert info['head'] is None
    assert info['files'][0]['status']=='untracked'


def test_git_endpoint_auth_and_removed_restore_endpoints(tmp_path):
    root=tmp_path/'repo';repository(root)
    config=ServerConfig({'app':root},tmp_path/'model.yaml',tmp_path/'state','git-test-token')
    auth={'Authorization':'Bearer git-test-token'}
    with TestClient(create_app(config,factory=lambda *args:None)) as client:
        assert client.get('/v1/projects/app/git').status_code==401
        assert client.get('/v1/projects/missing/git',headers=auth).status_code==404
        assert client.get('/v1/projects/app/git',headers=auth).json()['branch']=='main'
        assert client.get('/v1/runs/any/code-checkpoint',headers=auth).status_code==404
        assert client.post('/v1/runs/any/code-restore',headers=auth,json={'action':'undo','revision':'x'}).status_code==404
    assert not list(config.state_dir.glob('*code*'))


def lines(diff, kind):
    return [line['text'] for hunk in diff['hunks'] for line in hunk['lines'] if line['kind'] == kind]


def test_file_diff_separates_staged_unstaged_and_untracked(tmp_path):
    root = tmp_path / 'repo'
    repository(root)
    (root / 'file.txt').write_text('after\nsecond\n')
    git(root, 'add', 'file.txt')
    (root / 'file.txt').write_text('after\nsecond\nthird\n')
    (root / 'untracked.txt').write_text('fresh\n')
    index_before = (root / '.git/index').read_bytes()
    head_before = git(root, 'rev-parse', 'HEAD')
    combined = file_diff(root, 'file.txt', 'all')
    staged = file_diff(root, 'file.txt', 'staged')
    unstaged = file_diff(root, 'file.txt', 'unstaged')
    added = file_diff(root, 'untracked.txt', 'all')
    assert lines(combined, 'add') == ['after', 'second', 'third']
    assert lines(combined, 'delete') == ['before']
    assert lines(staged, 'add') == ['after', 'second']
    assert lines(unstaged, 'add') == ['third']
    assert added['untracked'] and lines(added, 'add') == ['fresh']
    assert file_diff(root, 'untracked.txt', 'unstaged')['untracked']
    with pytest.raises(GitRequestError) as missing:
        file_diff(root, 'untracked.txt', 'staged')
    assert missing.value.status == 404
    assert (root / '.git/index').read_bytes() == index_before
    assert git(root, 'rev-parse', 'HEAD') == head_before


def test_file_diff_handles_rename_binary_and_unborn_repository(tmp_path):
    root = tmp_path / 'repo'
    repository(root)
    git(root, 'mv', 'file.txt', 'renamed.txt')
    (root / 'renamed.txt').write_text('before\nextra\n')
    (root / 'bin.dat').write_bytes(b'a\0b\n')
    info = workspace_git(root)
    renamed = next(item for item in info['files'] if item['path'] == 'renamed.txt')
    assert renamed['status'] == 'renamed' and renamed['old_path'] == 'file.txt'
    assert (renamed['added'], renamed['removed']) == (1, 0)
    assert (info['added'], info['removed']) == (1, 0)
    assert all(item['path'] != 'file.txt' for item in info['files'])
    diff = file_diff(root, 'renamed.txt', 'all')
    assert diff['old_path'] == 'file.txt'
    assert 'extra' in lines(diff, 'add')
    assert 'before' in lines(diff, 'context')
    assert file_diff(root, 'bin.dat', 'all')['binary'] is True
    assert file_diff(root, 'bin.dat', 'all')['hunks'] == []
    empty = tmp_path / 'empty'
    empty.mkdir()
    git(empty, 'init', '-b', 'main')
    (empty / 'first.txt').write_text('first\n')
    assert file_diff(empty, 'first.txt', 'all')['hunks'][0]['lines'][0]['text'] == 'first'
    git(empty, 'add', 'first.txt')
    staged = file_diff(empty, 'first.txt', 'staged')
    assert lines(staged, 'add') == ['first']
    assert workspace_git(empty)['head'] is None


def test_pure_rename_counts_match_between_summary_and_files(tmp_path):
    root = tmp_path / 'repo'
    repository(root)
    (root / 'other.txt').write_text('one\n')
    git(root, 'add', 'other.txt')
    git(root, 'commit', '-m', 'other')
    git(root, 'mv', 'file.txt', 'lib.py')
    (root / 'other.txt').write_text('one\nmore\n')
    info = workspace_git(root)
    renamed = next(item for item in info['files'] if item['path'] == 'lib.py')
    other = next(item for item in info['files'] if item['path'] == 'other.txt')
    assert renamed['status'] == 'renamed' and renamed['old_path'] == 'file.txt'
    assert (renamed['added'], renamed['removed']) == (0, 0)
    assert (other['added'], other['removed']) == (1, 0)
    counted = [item for item in info['files'] if item['added'] is not None and item['removed'] is not None]
    assert (info['added'], info['removed']) == (sum(item['added'] for item in counted), sum(item['removed'] for item in counted))
    assert (info['added'], info['removed']) == (1, 0)


def test_file_diff_rejects_paths_outside_workspace_and_keeps_special_names(tmp_path):
    root = tmp_path / 'repo'
    repository(root)
    secret = tmp_path / 'secret.txt'
    secret.write_text('DO_NOT_LEAK_BODY\n')
    (root / 'link.txt').symlink_to(secret)
    (root / 'outside').symlink_to(tmp_path)
    nested = 'dir/we ird\n"quote".txt'
    (root / nested).parent.mkdir()
    (root / nested).write_text('kept\n')
    listed = workspace_git(root)
    assert any(item['path'] == nested for item in listed['files'])
    assert lines(file_diff(root, nested, 'all'), 'add') == ['kept']
    linked = file_diff(root, 'link.txt', 'all')
    assert linked['symlink'] is True and linked['hunks'] == []
    assert 'DO_NOT_LEAK_BODY' not in str(linked)
    for raw in ('../secret.txt', str(secret), 'outside/secret.txt', '/etc/passwd', 'dir/../../secret.txt'):
        with pytest.raises(GitRequestError) as rejected:
            file_diff(root, raw, 'all')
        assert rejected.value.status == 422
        assert 'DO_NOT_LEAK_BODY' not in str(rejected.value)


def test_file_diff_limits_time_and_output(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    repository(root)
    (root / 'file.txt').write_text(''.join(f'line {index}\n' for index in range(30)))
    monkeypatch.setattr('agent.server.gitinfo.PREVIEW_DIFF_LINES', 4)
    preview = file_diff(root, 'file.txt', 'all', full=False)
    full = file_diff(root, 'file.txt', 'all', full=True)
    assert preview['truncated'] is True
    assert sum(len(hunk['lines']) for hunk in preview['hunks']) <= 4
    assert full['truncated'] is False
    assert sum(len(hunk['lines']) for hunk in full['hunks']) > 4
    pidfile = tmp_path / 'git.pid'
    script = tmp_path / 'slowgit'
    script.write_text(f'#!/bin/sh\necho $$ > "{pidfile}"\nsleep 30\n')
    script.chmod(0o755)
    monkeypatch.setattr('agent.server.gitinfo.GIT_BIN', str(script))
    monkeypatch.setattr('agent.server.gitinfo.TIMEOUT_SECONDS', 0.2)
    started = time.monotonic()
    assert file_diff(root, 'file.txt', 'all') == {'available': False}
    assert time.monotonic() - started < 5
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_git_diff_endpoint_auth_scope_and_path(tmp_path):
    root = tmp_path / 'repo'
    repository(root)
    (root / 'file.txt').write_text('after\n')
    (root / 'space name.txt').write_text('space\n')
    config = ServerConfig({'app': root}, tmp_path / 'model.yaml', tmp_path / 'state', 'git-test-token')
    auth = {'Authorization': 'Bearer git-test-token'}
    with TestClient(create_app(config, factory=lambda *args: None)) as client:
        assert client.get('/v1/projects/app/git/diff', params={'path': 'file.txt'}).status_code == 401
        assert client.get('/v1/projects/missing/git/diff', params={'path': 'file.txt'}, headers=auth).status_code == 404
        body = client.get('/v1/projects/app/git/diff', params={'path': 'file.txt', 'scope': 'all'}, headers=auth).json()
        assert lines(body, 'add') == ['after']
        assert client.get('/v1/projects/app/git/diff', params={'path': 'space name.txt'}, headers=auth).json()['hunks'][0]['lines'][0]['text'] == 'space'
        assert client.get('/v1/projects/app/git/diff', params={'path': '../file.txt'}, headers=auth).status_code == 422
        assert client.get('/v1/projects/app/git/diff', params={'path': 'missing.txt'}, headers=auth).status_code == 404
        assert client.get('/v1/projects/app/git/diff', params={'path': 'file.txt', 'scope': 'nope'}, headers=auth).status_code == 422
