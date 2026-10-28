import concurrent.futures
import threading
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.control import RunController
from agent.subagents.log_store import SubAgentLogStore
from agent.subagents.manager import SubAgentManager
from agent.subagents.runtime import SubAgentRuntime


def test_atomic_limit_releases_on_success_failure_and_cancel():
    parent = RunController()
    parent.begin_run()
    manager = SubAgentManager(parent)
    started = threading.Barrier(3)
    finish = threading.Event()

    def work():
        with manager.running() as (_, child):
            started.wait(timeout=3)
            finish.wait(3)
            return child.cancel_requested

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(work) for _ in range(2)]
        started.wait(timeout=3)
        assert manager.active_count == 2
        with pytest.raises(RuntimeError, match='concurrency limit'):
            with manager.running():
                pytest.fail('third task admitted')
        parent.cancel()
        finish.set()
        assert all(f.result(3) for f in futures)
    assert manager.active_count == 0
    parent.end_run()
    parent.begin_run()
    with pytest.raises(ValueError):
        with manager.running():
            raise ValueError('failure')
    assert manager.active_count == 0
    with manager.running():
        assert manager.active_count == 1
    assert manager.active_count == 0


def test_native_runtime_parallel_and_parent_cancel(tmp_path):
    parent = RunController()
    parent.begin_run()
    manager = SubAgentManager(parent)
    barrier = threading.Barrier(3)
    controllers = []

    def prepare(controller):
        controllers.append(controller)

        class Graph:
            def stream(self, input, config, **kwargs):
                assert len(input['messages']) == 1
                assert config['callbacks']
                barrier.wait(3)
                assert controller._current_scope().cancel_event.wait(3)
                yield {'messages': [AIMessage(content='should be discarded after cancel')]}
        return SimpleNamespace(graph=Graph())

    runtime = SubAgentRuntime(manager, SubAgentLogStore(tmp_path), prepare, 'fake')
    def run():
        return runtime.invoke({'messages': [HumanMessage(content='delegated')]},
                              {'configurable': {'thread_id': 'parent'}})
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        barrier.wait(3)
        third = run()
        assert 'concurrency limit' in third['messages'][0].content
        parent.cancel()
        assert all('cancelled' in future.result(5)['messages'][0].content for future in futures)
    assert manager.active_count == 0
    assert len(controllers) == 2
    assert len(list(tmp_path.rglob('*.jsonl'))) == 2


def test_child_stream_is_cancelled_and_parent_waits_for_close():
    parent = RunController()
    parent.begin_run()
    child = RunController()
    child.begin_run()
    parent.register_child(child)
    class Stream:
        closed = False
        def cancel(self):
            self.cancelled = True
        def wait_closed(self, timeout):
            return self.closed
    stream = Stream()
    assert child.register_model_stream(stream)
    parent.cancel()
    assert stream.cancelled
    child.unregister_model_stream(stream)
    assert not parent.wait_model_streams_closed(0)
    stream.closed = True
    assert parent.wait_model_streams_closed(0)


@pytest.mark.parametrize('api', ['responses', 'chat_completions'])
def test_parent_cancel_closes_real_child_http_stream(api, tmp_path, monkeypatch):
    from dataclasses import replace
    from deepagents.backends import StateBackend
    import agent.factory as factory
    from agent.factory import create_agent
    from agent.runner import AgentRunner
    from agent.sandbox import BackendSelection, ExecutionMode
    from agent.llm import build_chat_model
    from agent.llm_stream import close_model_http_clients, live_stream_count
    from tests.model_stream_server import ScriptedSSEServer, Turn
    from tests.test_model_stream_cancel import Harness, settings_for
    call = {'id': 'delegate', 'name': 'task', 'args': {
        'description': 'child-only instructions', 'subagent_type': 'general-purpose'}}
    with ScriptedSSEServer(api, [Turn(tool_calls=[call]), Turn(mode='hang')]) as server:
        cfg = replace(settings_for(server), config_dir=tmp_path)
        backend = StateBackend()
        monkeypatch.setattr(factory, 'select_backend', lambda cfg: BackendSelection(backend, ExecutionMode.SANDBOXED))
        parent_model = build_chat_model(cfg.active_profile)
        child_models = []
        def build_child(profile, **kwargs):
            child = build_chat_model(profile, **kwargs)
            child_models.append(child)
            return child
        monkeypatch.setattr(factory, 'build_chat_model', build_child)
        runner = AgentRunner(prepared=create_agent(model=parent_model, settings=cfg, skills=[]), settings=cfg)
        harness = Harness(runner).start(runner.invoke, 'parent private history')
        try:
            child_connection = server.wait_for_connection(1)
            assert child_connection.hanging.wait(5)
            tools = {tool['function']['name'] if 'function' in tool else tool.get('name')
                     for tool in child_connection.body['tools']}
            assert not {'task', 'request_human_input', 'read_subagent_log'} & tools
            import json
            assert 'parent private history' not in json.dumps(child_connection.body)
            assert runner.prepared.subagent_manager.active_count == 1
            runner.request_cancel()
            assert harness.join(8)
            assert harness.error is None
            assert harness.result.status == 'cancelled'
            assert child_connection.closed.wait(3)
            assert child_connection.closed_by_client_at is not None
            assert live_stream_count() == 0
            assert runner.prepared.subagent_manager.active_count == 0
            assert 'assistant_delta' not in harness.types()
            assert len(child_models) == 1 and child_models[0] is not parent_model
            assert child_models[0].http_client.is_closed
            assert child_models[0].http_async_client.is_closed
            assert not parent_model.http_async_client.is_closed
        finally:
            runner.close()
            close_model_http_clients(parent_model)


@pytest.mark.parametrize('api', ['responses', 'chat_completions'])
def test_independent_child_clients_close_if_graph_assembly_fails(api, tmp_path, monkeypatch):
    import agent.factory as factory
    from deepagents.backends import StateBackend
    from agent.config import ModelProfile, Settings, SubAgentConfig
    from agent.llm import build_chat_model
    from agent.runner import AgentRunner
    from agent.sandbox import BackendSelection, ExecutionMode
    from tests.conftest import scripted_model
    cfg = Settings(config_dir=tmp_path, llm_profiles=(ModelProfile('default', 'parent'), ModelProfile('child', 'child', api=api)),
                   subagents=SubAgentConfig(model='child'))
    created = []
    original = factory.build_agent
    def assemble(spec, *args, **kwargs):
        if spec.role == 'subagent':
            raise RuntimeError('child assembly failed')
        return original(spec, *args, **kwargs)
    def build(profile, **kwargs):
        model = build_chat_model(profile, **kwargs)
        created.append(model)
        return model
    monkeypatch.setattr(factory, 'build_agent', assemble)
    monkeypatch.setattr(factory, 'build_chat_model', build)
    monkeypatch.setattr(factory, 'select_backend', lambda cfg: BackendSelection(StateBackend(), ExecutionMode.SANDBOXED))
    parent = scripted_model([
        AIMessage(content='', tool_calls=[{'id': 'd', 'name': 'task', 'args': {
            'description': 'task', 'subagent_type': 'general-purpose'}}]), AIMessage(content='reported failure')])
    runner = AgentRunner(model=parent, settings=cfg)
    try:
        assert runner.invoke('go').output == 'reported failure'
        assert len(created) == 1
        assert created[0].http_client.is_closed and created[0].http_async_client.is_closed
        assert runner.prepared.subagent_manager.active_count == 0
    finally:
        runner.close()
