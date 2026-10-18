"""Read-only discovery: keyset history, old DB, authentication and active slots."""
import base64
import json
import sqlite3
import pytest
from fastapi.testclient import TestClient
from agent.server.app import create_app
from agent.server.store import ServiceStore
from tests.test_server import config, FakeRunner, AUTH, session, submit, wait


def test_run_history_keyset_same_timestamp_and_session_isolation(config):
    with TestClient(create_app(config,factory=lambda *args:FakeRunner())) as client:
        sid=session(client);other=session(client,'two');store=client.app.state.manager.store
        for i in range(123):
            run=store.create_run(f'r-{i:03}',store.session(sid));store.finish(run['id'],'completed',{'output':'x'*2000})
        with store.lock,store.conn:
            store.conn.execute("UPDATE runs SET created_at=?",('2026-10-13T00:00:00+00:00',))
        store.create_run('foreign',store.session(other));store.finish('foreign','completed',{})
        ids=[];cursor=None
        while True:
            params={'limit':17}
            if cursor:params['cursor']=cursor
            response=client.get(f'/v1/sessions/{sid}/runs',params=params,headers=AUTH)
            assert response.status_code==200
            data=response.json();ids.extend(r['id'] for r in data['runs'])
            assert all(set(r)=={'id','session_id','project_id','status','created_at','updated_at'} for r in data['runs'])
            cursor=data['next_cursor']
            if cursor is None:break
        assert ids==[f'r-{i:03}' for i in range(122,-1,-1)]
        first=client.get(f'/v1/sessions/{sid}/runs?limit=1',headers=AUTH).json()
        assert client.get(f'/v1/sessions/{other}/runs',params={'cursor':first['next_cursor']},headers=AUTH).status_code==422


def test_read_queries_auth_empty_unknown_and_bad_cursor(config):
    with TestClient(create_app(config,factory=lambda *args:FakeRunner())) as client:
        sid=session(client)
        path=f'/v1/sessions/{sid}/runs'
        assert client.get(path).status_code==401
        assert client.get('/v1/projects/one/active-run').status_code==401
        assert client.get(path,headers=AUTH).json()=={'runs':[],'next_cursor':None}
        assert client.get('/v1/projects/one/active-run',headers=AUTH).json()=={'run':None}
        for query in ['limit=0','limit=101','cursor=bad!','cursor='+base64.urlsafe_b64encode(b'[]').decode()]:
            assert client.get(path+'?'+query,headers=AUTH).status_code==422
        assert client.get('/v1/sessions/unknown/runs',headers=AUTH).status_code==404
        assert client.get('/v1/projects/unknown/active-run',headers=AUTH).status_code==404
        client.app.state.manager.store.create_session('removed','unconfigured')
        assert client.get('/v1/sessions/removed/runs',headers=AUTH).status_code==404


@pytest.mark.parametrize('text,status',[('block','running'),('human','waiting_human'),('approve','waiting_confirmation')])
def test_active_run_returns_real_nonterminal_snapshot(config,text,status):
    with TestClient(create_app(config,factory=lambda *args:FakeRunner())) as client:
        sid=session(client);rid=submit(client,sid,text);wait(client,rid,status)
        data=client.get('/v1/projects/one/active-run',headers=AUTH).json()['run']
        assert data['id']==rid and data['status']==status and 'result' not in data
        assert client.get('/v1/projects/two/active-run',headers=AUTH).json()=={'run':None}
        client.post(f'/v1/runs/{rid}/cancel',headers=AUTH);wait(client,rid,'cancelled')
        assert client.get('/v1/projects/one/active-run',headers=AUTH).json()=={'run':None}


def test_session_catalog_is_shared_and_workspace_operation_is_exclusive(config):
    from agent.session import SessionStore, try_acquire_workspace
    with TestClient(create_app(config, factory=lambda *args: FakeRunner())) as client:
        sid = session(client)
        catalog = SessionStore.for_workspace(config.projects['one'])
        try:
            assert catalog.get(sid) is not None
            cli = catalog.create_session(title='来自终端')
        finally:
            catalog.close()
        listed = client.get('/v1/sessions?project_id=one', headers=AUTH).json()['sessions']
        assert any(item['id'] == cli.id and item['title'] == '来自终端' for item in listed)
        assert client.get(f'/v1/sessions/{cli.id}', headers=AUTH).status_code == 200
        held = try_acquire_workspace(config.projects['one'])
        assert held is not None
        try:
            rejected = client.post(f'/v1/sessions/{sid}/runs', json={'text': '占用中'}, headers=AUTH)
            assert rejected.status_code == 409
            assert 'workspace' in rejected.json()['detail']
        finally:
            held.release()
        continued = submit(client, cli.id, '终端会话继续')
        wait(client, continued, 'completed')
        assert client.get(f'/v1/runs/{continued}', headers=AUTH).json()['prompt'] == '终端会话继续'
        titled = client.get('/v1/sessions?project_id=one', headers=AUTH).json()['sessions']
        assert any(item['id'] == cli.id and item['title'] == '终端会话继续' for item in titled)
        assert client.request('DELETE', f'/v1/sessions/{sid}', headers=AUTH).json() == {'deleted': True}
        reopened = SessionStore.for_workspace(config.projects['one'])
        try:
            assert reopened.get(sid) is None
            assert reopened.get(cli.id) is not None
        finally:
            reopened.close()


def test_submitted_prompt_is_restored_and_empty_sessions_can_be_discarded(config):
    with TestClient(create_app(config, factory=lambda *args: FakeRunner())) as client:
        sid = session(client)
        rid = submit(client, sid, '审查登录流程')
        wait(client, rid, 'completed')
        run = client.get(f'/v1/runs/{rid}', headers=AUTH).json()
        assert run['prompt'] == '审查登录流程'
        listed = client.get('/v1/sessions?project_id=one', headers=AUTH).json()['sessions']
        assert listed[0]['id'] == sid and listed[0]['title'] == '审查登录流程'
        summary = client.get(f'/v1/sessions/{sid}/runs', headers=AUTH).json()['runs'][0]
        assert 'prompt' not in summary
        empty = session(client)
        assert client.request('DELETE', f'/v1/sessions/{empty}', headers=AUTH).json() == {'deleted': True}
        assert client.get(f'/v1/sessions/{empty}', headers=AUTH).status_code == 404
        assert client.request('DELETE', f'/v1/sessions/{sid}', headers=AUTH).status_code == 409
        resumed = client.post(f'/v1/sessions/{sid}/runs', json={'resume': True}, headers=AUTH)
        assert resumed.status_code == 202
        assert resumed.json()['prompt'] == ''


def test_old_database_gains_prompt_without_rewriting_history(tmp_path):
    path = tmp_path / 'old.sqlite3'
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, project_id TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            result TEXT NOT NULL DEFAULT '{}', interrupt_id TEXT
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL
        );
    """)
    conn.execute("INSERT INTO sessions VALUES ('s','p','2026-10-18T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO runs(id,session_id,project_id,status,created_at,updated_at,result) "
        "VALUES ('r','s','p','completed','2026-10-18T00:00:00+00:00','2026-10-18T00:00:01+00:00','{\"output\":\"old\"}')"
    )
    conn.commit()
    conn.close()
    store = ServiceStore(path, secrets=('sk-live-secret',))
    try:
        assert store.run('r')['prompt'] == ''
        assert store.run('r')['result']['output'] == 'old'
        created = store.create_run('r2', store.session('s'), 'token sk-live-secret stays hidden')
        assert created['prompt'] == 'token [REDACTED] stays hidden'
        assert store.sessions(None)[0]['title'] == 'token [REDACTED] stays hidden'
        assert store.delete_empty_session('s') is False
    finally:
        store.close()


def test_old_sqlite_additive_index_does_not_change_history(tmp_path):
    path=tmp_path/'old.sqlite3'
    store=ServiceStore(path);store.create_session('s','p');run=store.create_run('r',store.session('s'));store.finish('r','completed',{'output':'old'});before=store.events('r',0);store.close()
    conn=sqlite3.connect(path);conn.execute('DROP INDEX runs_session_created');conn.commit();conn.close()
    restored=ServiceStore(path)
    try:
        assert restored.session_runs('s')['runs'][0]['id']=='r'
        assert restored.run('r')['result']['output']=='old'
        assert restored.events('r',0)==before
        assert restored.conn.execute("SELECT name FROM sqlite_master WHERE name='runs_session_created'").fetchone()
    finally:restored.close()


def test_active_snapshot_during_cancellation_and_atomic_finish(config,monkeypatch):
    from threading import Event,Thread
    from agent.runner import RunResult
    entered=Event();release_execution=Event();finishing=Event();release_finish=Event();read_started=Event();read_done=Event();result=[]
    class Controlled(FakeRunner):
        def invoke(self,text,**callbacks):
            entered.set();assert release_execution.wait(5)
            return RunResult(status='cancelled')
    with TestClient(create_app(config,factory=lambda *args:Controlled())) as client:
        manager=client.app.state.manager;original=manager.store.finish
        def finish(*args):
            finishing.set();assert release_finish.wait(5);return original(*args)
        monkeypatch.setattr(manager.store,'finish',finish)
        reader=None
        try:
            rid=submit(client,session(client),'block');assert entered.wait(5)
            assert client.post(f'/v1/runs/{rid}/cancel',headers=AUTH).status_code==202
            assert client.get('/v1/projects/one/active-run',headers=AUTH).json()['run']['status']=='cancelling'
            release_execution.set();assert finishing.wait(5)
            def read():
                read_started.set();result.append(manager.active_run('one'));read_done.set()
            reader=Thread(target=read);reader.start();assert read_started.wait(5)
            assert not read_done.is_set() # Publication holds the admission lock.
            release_finish.set();reader.join(5)
            assert result==[{'run':None}]
        finally:
            release_execution.set();release_finish.set()
            if reader:reader.join(5)


@pytest.mark.parametrize('status',['paused','runtime_config_boundary'])
def test_active_waiting_boundaries_are_not_terminal(config,status):
    from agent.runner import InterruptKind,InterruptState,RunResult
    class Boundary(FakeRunner):
        def invoke(self,text,**callbacks):
            self.state=InterruptState(InterruptKind(status),{})
            return RunResult(status=status)
    with TestClient(create_app(config,factory=lambda *args:Boundary())) as client:
        rid=submit(client,session(client),'boundary');wait(client,rid,status)
        assert client.get('/v1/projects/one/active-run',headers=AUTH).json()['run']['status']==status
        client.post(f'/v1/runs/{rid}/cancel',headers=AUTH);wait(client,rid,'cancelled')


def test_cleanup_failure_is_blocked_without_reporting_a_terminal_run_as_active(config):
    class Failure(FakeRunner):
        def close(self):raise RuntimeError('controlled cleanup failure')
    with pytest.raises(RuntimeError,match='cleanup failed'):
        with TestClient(create_app(config,factory=lambda *args:Failure())) as client:
            sid=session(client);rid=submit(client,sid,'done');wait(client,rid,'failed')
            assert client.get('/v1/projects/one/active-run',headers=AUTH).json()=={'run':None,'blocked':True}
            assert client.post(f'/v1/sessions/{sid}/runs',headers=AUTH,json={'text':'other'}).status_code==409
