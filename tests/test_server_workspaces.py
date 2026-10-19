"""Real path selection, catalog persistence and native Git branch switching."""
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from agent.server.app import create_app
from agent.server.config import ServerConfig
from agent.server.runtime import ActiveRun
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
        assert client.get('/v1/workspaces',headers=AUTH).json()=={'workspaces':[{'id':'initial','path':str(config.projects['initial'])}]}
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


def test_switch_branch_uses_native_git_and_rejects_active_tasks(config):
    root=config.projects['initial'];root.rmdir();repository(root)
    git(root,'branch','feature')
    with TestClient(create_app(config,factory=lambda *args:None)) as client:
        info=client.get('/v1/projects/initial/git',headers=AUTH).json()
        assert info['branches']==['feature','main']
        response=client.post('/v1/projects/initial/git/branch',headers=AUTH,json={'branch':'feature'})
        assert response.status_code==200 and response.json()['branch']=='feature'
        assert git(root,'branch','--show-current')=='feature'
        manager=client.app.state.manager
        manager.active['reservation']=ActiveRun('initial')
        try:
            assert client.post('/v1/projects/initial/git/branch',headers=AUTH,json={'branch':'main'}).status_code==409
        finally:manager.active.pop('reservation')
        assert client.post('/v1/projects/initial/git/branch',headers=AUTH,json={'branch':'missing'}).status_code==409
        assert git(root,'branch','--show-current')=='feature'


def test_branch_conflict_does_not_discard_existing_edits(config):
    root=config.projects['initial'];root.rmdir();repository(root)
    git(root,'switch','-c','feature')
    (root/'file.txt').write_text('feature version\n');git(root,'commit','-am','feature')
    git(root,'switch','main');(root/'file.txt').write_text('unsaved user edit\n')
    with TestClient(create_app(config,factory=lambda *args:None)) as client:
        response=client.post('/v1/projects/initial/git/branch',headers=AUTH,json={'branch':'feature'})
        assert response.status_code==409
        assert (root/'file.txt').read_text()=='unsaved user edit\n'
        assert git(root,'branch','--show-current')=='main'


def test_remote_branches_are_listed_and_switch_to_the_exact_remote_commit(config):
    root=config.projects['initial'];root.rmdir();repository(root)
    base=git(root,'rev-parse','HEAD')
    git(root,'update-ref','refs/remotes/origin/topic',base)
    git(root,'update-ref','refs/remotes/upstream/nested/topic',base)
    git(root,'symbolic-ref','refs/remotes/origin/HEAD','refs/remotes/origin/topic')
    # A same-named local branch must not override the selected remote ref.
    git(root,'switch','-c','origin/topic')
    (root/'file.txt').write_text('local version\n');git(root,'commit','-am','local')
    with TestClient(create_app(config,factory=lambda *args:None)) as client:
        info=client.get('/v1/projects/initial/git',headers=AUTH).json()
        assert info['branches']==['main','origin/topic']
        assert info['remote_branches']==['origin/topic','upstream/nested/topic']
        response=client.post('/v1/projects/initial/git/branch',headers=AUTH,json={'branch':'refs/remotes/origin/topic'})
        assert response.status_code==200,response.text
        assert response.json()['branch']=='detached'
        assert git(root,'rev-parse','HEAD')==base
        assert git(root,'branch','--show-current')==''
        assert client.post('/v1/projects/initial/git/branch',headers=AUTH,json={'branch':'refs/remotes/origin/HEAD'}).status_code==409


def test_remote_branch_conflict_preserves_edits(config):
    root=config.projects['initial'];root.rmdir();repository(root)
    git(root,'switch','-c','topic')
    (root/'file.txt').write_text('remote version\n');git(root,'commit','-am','remote')
    git(root,'update-ref','refs/remotes/origin/topic','HEAD')
    git(root,'switch','main');(root/'file.txt').write_text('unsaved edit\n')
    with TestClient(create_app(config,factory=lambda *args:None)) as client:
        response=client.post('/v1/projects/initial/git/branch',headers=AUTH,json={'branch':'refs/remotes/origin/topic'})
        assert response.status_code==409
        assert (root/'file.txt').read_text()=='unsaved edit\n'
        assert git(root,'branch','--show-current')=='main'
