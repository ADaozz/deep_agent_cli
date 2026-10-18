"""Git presentation uses real workspace state and never creates checkpoints."""
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from agent.server.gitinfo import workspace_git
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
    assert next(f for f in info['files'] if f['path']=='untracked.txt')['added'] is None
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
