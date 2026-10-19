from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, TypedDict

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import _messages_delta_reducer
import pytest

from agent.server.app import create_app
from agent.server.config import ServerConfig
from agent.server.transcripts import saved_transcript, TranscriptRequestError
from agent.session import SessionStore


def save(store, session_id, messages, stamp=None):
    checkpoint=empty_checkpoint()
    if stamp: checkpoint['ts']=stamp
    checkpoint['channel_values']={'messages':messages}
    checkpoint['channel_versions']={'messages':'1'}
    store.checkpointer.put({'configurable':{'thread_id':session_id,'checkpoint_ns':''}},checkpoint,
                           {'source':'update','step':0,'parents':{}},{'messages':'1'})
    return checkpoint


def test_saved_cli_messages_tools_and_pagination_are_read_only(tmp_path):
    path=tmp_path/'sessions.sqlite3'
    store=SessionStore(path)
    session=store.create_session(title='旧会话')
    messages=[HumanMessage(content='运行 pwd',id='u'),
              AIMessage(content='',id='a',tool_calls=[{'id':'call','name':'execute','args':{'command':'pwd'}}]),
              ToolMessage(content='/workspace',tool_call_id='call',name='execute',id='t'),
              AIMessage(content='当前目录是工作区。',id='answer')]
    save(store,session.id,messages)
    db=path.read_bytes();wal=Path(str(path)+'-wal').read_bytes()
    latest=saved_transcript(path,session.id,limit=2)
    assert [block['kind'] for block in latest['blocks']]==['tool','assistant']
    assert latest['blocks'][0]['content']=='/workspace'
    assert latest['blocks'][0]['status']=='completed'
    assert 'pwd' in latest['blocks'][0]['arguments']
    assert latest['next_cursor']
    assert path.read_bytes()==db and Path(str(path)+'-wal').read_bytes()==wal
    # Pagination stays pinned to the checkpoint even if newer content is saved.
    save(store,session.id,[*messages,HumanMessage(content='new',id='new')])
    older=saved_transcript(path,session.id,limit=2,cursor=latest['next_cursor'])
    assert [block['kind'] for block in older['blocks']]==['user']
    assert older['blocks'][0]['content']=='运行 pwd'
    assert older['next_cursor'] is None
    db=path.read_bytes();wal=Path(str(path)+'-wal').read_bytes()
    saved_transcript(path,session.id)
    assert path.read_bytes()==db and Path(str(path)+'-wal').read_bytes()==wal
    store.close()


def test_restores_delta_checkpoint_history_without_invoking_the_graph(tmp_path):
    path=tmp_path/'delta.sqlite3';store=SessionStore(path)
    session=store.create_session(title='delta')
    class State(TypedDict):
        messages: Annotated[list, DeltaChannel(_messages_delta_reducer)]
    calls=[]
    def respond(state):
        calls.append('respond')
        return {'messages':[AIMessage(content='已恢复的回答',id='answer')]}
    graph=StateGraph(State);graph.add_node('respond',respond);graph.add_edge(START,'respond');graph.add_edge('respond',END)
    compiled=graph.compile(checkpointer=store.checkpointer)
    config={'configurable':{'thread_id':session.id}}
    compiled.invoke({'messages':[HumanMessage(content='旧提问',id='user')]},config)
    checkpoint=store.checkpointer.get_tuple(config)
    assert 'messages' not in checkpoint.checkpoint['channel_values']
    result=saved_transcript(path,session.id)
    assert [block['content'] for block in result['blocks']]==['旧提问','已恢复的回答']
    assert calls==['respond']
    store.close()


def test_cli_history_stops_before_first_web_run_without_duplicates(tmp_path):
    path=tmp_path/'sessions.sqlite3';store=SessionStore(path);session=store.create_session(title='mixed')
    boundary=datetime.now(timezone.utc)
    cli=[HumanMessage(content='CLI 提问',id='u'),AIMessage(content='CLI 回答',id='a')]
    save(store,session.id,cli,(boundary-timedelta(seconds=1)).isoformat())
    save(store,session.id,[*cli,HumanMessage(content='Web 提问',id='web')],(boundary+timedelta(seconds=1)).isoformat())
    result=saved_transcript(path,session.id,before=boundary.isoformat())
    assert [block['content'] for block in result['blocks']]==['CLI 提问','CLI 回答']
    assert saved_transcript(path,session.id,before=(boundary-timedelta(days=1)).isoformat())['blocks']==[]
    store.close()


def test_missing_empty_and_invalid_history(tmp_path):
    missing=tmp_path/'missing.sqlite3'
    assert saved_transcript(missing,'s')['blocks']==[] and not missing.exists()
    store=SessionStore(tmp_path/'empty.sqlite3');session=store.create_session()
    assert saved_transcript(store.path,session.id)['blocks']==[]
    with pytest.raises(TranscriptRequestError): saved_transcript(store.path,session.id,cursor='../other')
    with pytest.raises(TranscriptRequestError): saved_transcript(store.path,session.id,limit=0)
    store.close()


def test_transcript_endpoint_authentication_and_empty_http_history(tmp_path):
    path=tmp_path/'saved.sqlite3';store=SessionStore(path);session=store.create_session(title='CLI')
    save(store,session.id,[HumanMessage(content='真实旧提问'),AIMessage(content='真实旧回答')])
    root=tmp_path/'repo';root.mkdir()
    config=ServerConfig({'app':root},tmp_path/'model.yaml',tmp_path/'state','history-test')
    auth={'Authorization':'Bearer history-test'}
    with TestClient(create_app(config,factory=lambda *args:None)) as client:
        manager=client.app.state.manager
        manager.settings_by_project['app']=SimpleNamespace(state_path=path)
        manager.store.create_session(session.id,'app')
        endpoint=f'/v1/sessions/{session.id}/transcript'
        assert client.get(endpoint).status_code==401
        assert client.get(f'/v1/sessions/{session.id}/runs',headers=auth).json()['runs']==[]
        response=client.get(endpoint,headers=auth)
        assert response.status_code==200 and len(response.json()['blocks'])==2
        assert client.get(endpoint,headers=auth,params={'cursor':'bad'}).status_code==422
        assert client.post(endpoint,headers=auth,json={}).status_code==405
        assert not manager.active
    store.close()
