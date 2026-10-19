import pytest
from fastapi.testclient import TestClient

from agent.server.app import create_app
from agent.server.commits import commit_history
from agent.server.config import ServerConfig
from tests.test_server_gitinfo import git, repository


def test_commit_history_preserves_repository_and_paginates_from_a_fixed_head(tmp_path):
    root=tmp_path/'repo';repository(root)
    git(root,'remote','add','origin','git@github.com:ADaozz/deep_agent_cli.git')
    base=git(root,'rev-parse','HEAD')
    (root/'file.txt').write_text('after\nsecond\n')
    (root/'odd\tname\n.txt').write_text('new\n')
    (root/'binary.bin').write_bytes(b'\x00binary')
    git(root,'add','.');git(root,'commit','-m','中文提交\n\n详细说明')
    latest=git(root,'rev-parse','HEAD')
    (root/'file.txt').write_text('unsaved\n')
    index=(root/'.git/index').read_bytes()
    before=git(root,'status','--porcelain','--branch')
    first=commit_history(root,1)
    assert first['available']
    commit=first['commits'][0]
    assert commit['sha']==latest and commit['subject']=='中文提交'
    assert commit['message']=='中文提交\n\n详细说明'
    assert (commit['added'],commit['removed'],commit['files'])==(3,1,3)
    assert commit['author']=='Test'
    assert commit['url']==f'https://github.com/ADaozz/deep_agent_cli/commit/{latest}'
    assert (root/'.git/index').read_bytes()==index
    assert git(root,'status','--porcelain','--branch')==before
    git(root,'commit','--allow-empty','-m','a newer commit')
    second=commit_history(root,1,first['next_cursor'])
    assert [row['sha'] for row in second['commits']]==[base]
    assert (second['commits'][0]['added'],second['commits'][0]['files'])==(1,1)
    assert second['next_cursor'] is None


def test_merge_stats_compare_against_first_parent(tmp_path):
    root=tmp_path/'repo';repository(root)
    git(root,'switch','-c','feature')
    (root/'feature.txt').write_text('feature\n');git(root,'add','.');git(root,'commit','-m','feature')
    git(root,'switch','main');git(root,'merge','--no-ff','feature','-m','merge feature')
    merged=commit_history(root,1)['commits'][0]
    assert (merged['added'],merged['removed'],merged['files'])==(1,0,1)


def test_history_handles_empty_and_non_git_workspaces(tmp_path):
    root=tmp_path/'empty';root.mkdir();git(root,'init','-b','main')
    assert commit_history(root)=={'available':True,'commits':[],'next_cursor':None}
    assert not commit_history(tmp_path)['available']


@pytest.mark.parametrize('remote',['https://github.com/ADaozz/deep_agent_cli.git','ssh://git@github.com/ADaozz/deep_agent_cli.git','https://token@github.com/ADaozz/deep_agent_cli.git','https://evil.test/ADaozz/deep_agent_cli.git'])
def test_commit_links_are_safe_and_do_not_expose_remote_credentials(tmp_path,remote):
    root=tmp_path/'repo';repository(root);git(root,'remote','add','origin',remote)
    url=commit_history(root,1)['commits'][0]['url']
    if 'evil.test' in remote:
        assert url is None
    else:
        assert url.startswith('https://github.com/ADaozz/deep_agent_cli/commit/')
        assert 'token' not in url


def test_history_endpoint_auth_limits_cursor_and_read_only_contract(tmp_path):
    root=tmp_path/'repo';repository(root)
    config=ServerConfig({'app':root},tmp_path/'model.yaml',tmp_path/'state','history-token')
    auth={'Authorization':'Bearer history-token'}
    path='/v1/projects/app/git/commits'
    with TestClient(create_app(config,factory=lambda *args:None)) as client:
        assert client.get(path).status_code==401
        assert client.get(path,headers=auth).json()['commits'][0]['subject']=='base'
        assert client.get('/v1/projects/missing/git/commits',headers=auth).status_code==404
        for params in ({'limit':0},{'limit':51},{'cursor':'--all'},{'cursor':'HEAD:1'}):
            assert client.get(path,headers=auth,params=params).status_code==422
        assert client.post(path,headers=auth,json={}).status_code==405
