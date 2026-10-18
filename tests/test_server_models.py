"""Model selection shares CLI Runner persistence; no external model requests."""
import time

import pytest
import yaml
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent.server.app import create_app
from agent.server.config import ServerConfig
from tests.conftest import scripted_model

AUTH = {"Authorization": "Bearer model-service-test"}


@pytest.fixture
def model_config(tmp_path, monkeypatch):
    project = tmp_path / 'workspace'
    project.mkdir()
    path = tmp_path / 'model.yaml'
    path.write_text(yaml.safe_dump({
        'llm': {'default': 'OrgA/one', 'models': {
            'OrgA': {'api_key': 'never-return-this-key', 'base_url': 'http://private.example/v1', 'models': {
                'one': {'model': 'model-one', 'reasoning_efforts': ['low', 'high']}, 'two': {'model': 'model-two'},
            }},
            'OrgB': {'api_key': 'other-secret-key', 'models': {'three': {'model': 'model-three', 'reasoning_efforts': ['medium', 'high']}}},
        }},
        'paths': {'config_dir': str(tmp_path / 'agent-config')},
    }))
    def model(profile, **kwargs):
        return scripted_model([AIMessage(content=f"{profile.id}:{kwargs.get('reasoning_effort') or 'default'}")])
    monkeypatch.setattr('agent.runner.build_chat_model', model)
    return ServerConfig({'app': project}, path, tmp_path / 'state', 'model-service-test')


def test_catalog_filters_credentials_and_validates_choice(model_config):
    with TestClient(create_app(model_config)) as client:
        assert client.get('/v1/projects/app/models').status_code == 401
        response = client.get('/v1/projects/app/models', headers=AUTH)
        assert response.status_code == 200
        assert 'never-return-this-key' not in response.text
        assert 'private.example' not in response.text
        data = response.json()
        assert [(m['source'], m['model']) for m in data['models']] == [('OrgA', 'model-one'), ('OrgA', 'model-two'), ('OrgB', 'model-three')]
        assert data['models'][1]['reasoning_efforts'] == ['default']
        assert data['current']['model_id'] == 'OrgA/one'
        sid = client.post('/v1/sessions', json={'project_id': 'app'}, headers=AUTH).json()['id']
        for body in ({'model_id': 'missing', 'reasoning_effort': 'high'}, {'model_id': 'OrgA/one', 'reasoning_effort': 'extreme'}, {'model_id': 'OrgA/o', 'reasoning_effort': 'low'}):
            assert client.post(f'/v1/sessions/{sid}/model', json=body, headers=AUTH).status_code == 422
        assert client.get('/v1/projects/missing/models', headers=AUTH).status_code == 404
        assert client.get('/v1/sessions/missing/model', headers=AUTH).status_code == 404


def test_selected_model_and_effort_persist_and_execute_after_restart(model_config):
    with TestClient(create_app(model_config)) as client:
        sid = client.post('/v1/sessions', json={'project_id': 'app'}, headers=AUTH).json()['id']
        changed = client.post(f'/v1/sessions/{sid}/model', headers=AUTH, json={'model_id': 'OrgB/three', 'reasoning_effort': 'high'})
        assert changed.status_code == 200, changed.text
        assert changed.json() == {'model_id': 'OrgB/three', 'model': 'model-three', 'source': 'OrgB', 'reasoning_effort': 'high'}
    with TestClient(create_app(model_config)) as client:
        assert client.get(f'/v1/sessions/{sid}/model', headers=AUTH).json() == changed.json()
        rid = client.post(f'/v1/sessions/{sid}/runs', headers=AUTH, json={'text': 'use selected model'}).json()['id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = client.get(f'/v1/runs/{rid}', headers=AUTH).json()
            if run['status'] in {'completed', 'failed'}:
                break
            time.sleep(.01)
        assert run['status'] == 'completed', run
        assert run['result']['output'] == 'OrgB/three:high'


def test_busy_session_rejects_switch_without_touching_active_runner(model_config, monkeypatch):
    from agent.server.runtime import ActiveRun
    with TestClient(create_app(model_config)) as client:
        sid = client.post('/v1/sessions', json={'project_id': 'app'}, headers=AUTH).json()['id']
        manager = client.app.state.manager
        manager.active['test-reservation'] = ActiveRun('app')
        try:
            response = client.post(f'/v1/sessions/{sid}/model', headers=AUTH, json={'model_id': 'OrgA/two', 'reasoning_effort': 'default'})
            assert response.status_code == 409
            assert client.get(f'/v1/sessions/{sid}/model', headers=AUTH).json()['model_id'] == 'OrgA/one'
        finally:
            manager.active.pop('test-reservation')


def test_failed_switch_keeps_actual_model_and_redacts_provider_error(model_config, monkeypatch):
    original = __import__('agent.runner', fromlist=['build_chat_model']).build_chat_model
    def fail_target(profile, **kwargs):
        if profile.id == 'OrgB/three':
            raise RuntimeError('provider rejected never-return-this-key')
        return original(profile, **kwargs)
    monkeypatch.setattr('agent.runner.build_chat_model', fail_target)
    with TestClient(create_app(model_config)) as client:
        sid = client.post('/v1/sessions', json={'project_id': 'app'}, headers=AUTH).json()['id']
        response = client.post(f'/v1/sessions/{sid}/model', headers=AUTH, json={'model_id': 'OrgB/three', 'reasoning_effort': 'high'})
        assert response.status_code == 503
        assert 'never-return-this-key' not in response.text
        assert client.get(f'/v1/sessions/{sid}/model', headers=AUTH).json()['model_id'] == 'OrgA/one'
