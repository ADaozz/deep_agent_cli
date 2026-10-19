"""Real path selection, catalog persistence and read-only Git metadata."""
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from agent.server.app import create_app
from agent.server.config import ServerConfig
from tests.test_server_gitinfo import git, repository

AUTH={'Authorization':'Bearer workspace-test'}

@pytest.fixture
def config(tmp_path):
    root=tmp_path/'initial';root.mkdir()
    model=tmp_path/'model.yaml'
    model.write_text(yaml.safe_dump({'llm':{'default':'Org/test','models':{'Org':{'models':{'test':{}}}}},'paths':{'config_dir':str(tmp_path/'agent-config')}}))
    return ServerConfig({'initial':root},model,tmp_path/'state','workspace-test')


def test_workspace_catalog_and_directory_browser_require_auth(config):
    with TestClient(create_app(config,factory=lambda *args:None)) as client:
        assert client.get('/v1/workspaces').status_code==401
        assert client.post('/v1/workspace-directories',json={}).status_code==401
        assert client.get('/v1/workspaces',headers=AUTH).json()=={'workspaces':[{'id':'initial','path':str(config.projects['initial'])}], 'home_path':str(Path.home())}
        result=client.post('/v1/workspace-directories',headers=AUTH,json={'path':str(config.model_config.parent)}).json()
        assert result['path']==str(config.model_config.parent)
        assert 'initial' in [entry['name'] for entry in result['directories']]
        assert client.post('/v1/workspace-directories',headers=AUTH,json={'path':str(config.model_config)}).status_code==422


def test_selected_workspace_is_canonical_and_persists_without_starting_a_run(config):
    root=config.model_config.parent/'another directory';root.mkdir()
    with TestClient(create_app(config,factory=lambda *args:None)) as client:
        response=client.post('/v1/workspaces',headers=AUTH,json={'path':str(root)})
        assert response.status_code==200,response.text
        chosen=response.json()
        assert chosen['path']==str(root)
        assert client.post('/v1/workspaces',headers=AUTH,json={'path':str(root/'.')}).json()==chosen
        assert len(client.get('/v1/workspaces',headers=AUTH).json()['workspaces'])==2
        assert not client.app.state.manager.active
    # A fresh configuration object proves persistence is not only in memory.
    fresh=ServerConfig({'initial':config.projects['initial']},config.model_config,config.state_dir,config.token)
    with TestClient(create_app(fresh,factory=lambda *args:None)) as client:
        assert chosen in client.get('/v1/workspaces',headers=AUTH).json()['workspaces']
        session=client.post('/v1/sessions',headers=AUTH,json={'project_id':chosen['id']})
        assert session.status_code==201


@pytest.mark.parametrize('kind',['missing','file','parent','nested','storage'])
def test_invalid_workspaces_preserve_main_configuration_boundaries(config,kind):
    parent=config.model_config.parent
    nested=config.projects['initial']/'nested';nested.mkdir()
    choices={'missing':parent/'missing','file':config.model_config,'parent':parent,'nested':nested,'storage':config.state_dir}
    with TestClient(create_app(config,factory=lambda *args:None)) as client:
        assert client.post('/v1/workspaces',headers=AUTH,json={'path':str(choices[kind])}).status_code==422
        assert len(config.projects)==1


def test_git_lists_all_branches_and_tracking_relationships_without_writes(config):
    root=config.projects['initial'];root.rmdir();repository(root)
    git(root,'remote','add','origin',str(root))
    git(root,'branch','local-only')
    git(root,'update-ref','refs/remotes/origin/main','HEAD')
    git(root,'update-ref','refs/remotes/origin/remote-only','HEAD')
    git(root,'symbolic-ref','refs/remotes/origin/HEAD','refs/remotes/origin/main')
    git(root,'branch','--set-upstream-to','origin/main','main')
    (root/'file.txt').write_text('unsaved edit\n')
    before=git(root,'status','--porcelain','--branch')
    index=(root/'.git/index').read_bytes()
    with TestClient(create_app(config,factory=lambda *args:None)) as client:
        info=client.get('/v1/projects/initial/git',headers=AUTH).json()
        assert info['branches']==['local-only','main']
        assert info['remote_branches']==['origin/main','origin/remote-only']
        assert info['branch_upstreams']=={'main':'origin/main'}
        assert info['branch']=='main' and info['upstream']=='origin/main'
        assert client.get('/v1/projects/initial/git/diff',headers=AUTH,params={'path':'file.txt'}).status_code==200
    assert git(root,'status','--porcelain','--branch')==before
    assert (root/'.git/index').read_bytes()==index
    assert (root/'file.txt').read_text()=='unsaved edit\n'


def test_git_mutation_endpoints_are_absent_and_preserve_repository(config):
    root=config.projects['initial'];root.rmdir();repository(root)
    git(root,'branch','feature')
    (root/'file.txt').write_text('unsaved edit\n')
    before=git(root,'status','--porcelain','--branch')
    head=git(root,'rev-parse','HEAD')
    with TestClient(create_app(config,factory=lambda *args:None)) as client:
        schema=client.get('/openapi.json',headers=AUTH).json()
        paths={path:methods for path,methods in schema['paths'].items() if '/git' in path}
        assert set(paths)=={'/v1/projects/{project_id}/git','/v1/projects/{project_id}/git/diff','/v1/projects/{project_id}/git/commits'}
        assert all(set(methods)=={'get'} for methods in paths.values())
        for action in ('branch','checkout','commit','reset','restore','clean','stash','merge','rebase','fetch','pull','push','add'):
            response=client.post('/v1/projects/initial/git/'+action,headers=AUTH,json={'branch':'feature'})
            assert response.status_code==404,(action,response.text)
    assert git(root,'status','--porcelain','--branch')==before
    assert git(root,'rev-parse','HEAD')==head
    assert (root/'file.txt').read_text()=='unsaved edit\n'
