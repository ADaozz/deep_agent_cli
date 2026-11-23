import concurrent.futures
import threading
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.runtime.control import RunController
from agent.subagents.log_store import SubAgentLogStore
from agent.subagents.manager import SubAgentManager
from agent.subagents.runtime import SubAgentRuntime


def test_atomic_limit_releases_on_success_failure_and_cancel():
    parent = RunController()
    parent.begin_run()
    manager = SubAgentManager(parent, max_concurrent=2, max_total=10)
    started = threading.Barrier(3)
    finish = threading.Event()

    def work():
        with manager.running('session') as (_, child):
            started.wait(timeout=3)
            finish.wait(3)
            return child.cancel_requested

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(work) for _ in range(2)]
        started.wait(timeout=3)
        assert manager.active_count == 2
        with pytest.raises(RuntimeError, match='per-parent concurrency limit'):
            with manager.running('session'):
                pytest.fail('third task admitted')
        parent.cancel()
        finish.set()
        assert all(f.result(3) for f in futures)
    assert manager.active_count == 0
    parent.end_run()
    parent.begin_run()
    with pytest.raises(ValueError):
        with manager.running('session'):
            raise ValueError('failure')
    assert manager.active_count == 0
    with manager.running('session'):
        assert manager.active_count == 1
    assert manager.active_count == 0


def test_native_runtime_parallel_and_parent_cancel(tmp_path):
    parent = RunController()
    parent.begin_run()
    manager = SubAgentManager(parent, max_concurrent=2, max_total=10)
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


def test_per_parent_and_total_concurrency_limits():
    parent = RunController()
    parent.begin_run()
    manager = SubAgentManager(parent, max_concurrent=2, max_total=3)
    mid = RunController()
    mid.begin_run()
    held = []
    try:
        held.append(manager.admit('root', parent))
        held.append(manager.admit('root', parent))
        assert manager.active_children('root') == 2
        with pytest.raises(RuntimeError, match='per-parent concurrency limit'):
            manager.admit('root', parent)
        # A different parent may still admit until the tree-wide total is hit.
        held.append(manager.admit('mid', mid))
        assert manager.active_count == 3
        with pytest.raises(RuntimeError, match='total concurrency limit'):
            manager.admit('other', mid)
    finally:
        for run_id, _ in held:
            manager.release(run_id)
        mid.end_run()
        parent.end_run()


def test_cancel_owner_recurses_to_registered_children():
    root = RunController()
    root.begin_run()
    manager = SubAgentManager(root, max_concurrent=4, max_total=10)
    level1_id, level1 = manager.admit('session', root)
    level2_id, level2 = manager.admit(level1_id, level1)
    try:
        level1.cancel()
        assert level2.cancel_requested
    finally:
        manager.release(level2_id)
        manager.release(level1_id)
        root.end_run()


def test_cancel_isolates_sibling_subtrees():
    """Cancel A cancels A1; sibling B and Root keep running."""
    root = RunController()
    root.begin_run()
    manager = SubAgentManager(root, max_concurrent=4, max_total=10)
    a_id, a = manager.admit('session', root)
    b_id, b = manager.admit('session', root)
    a1_id, a1 = manager.admit(a_id, a)
    try:
        assert not root.cancel_requested
        a.cancel()
        assert a.cancel_requested and a1.cancel_requested
        assert not b.cancel_requested
        assert not root.cancel_requested
        assert manager.active_count == 3  # still admitted until release
    finally:
        manager.release(a1_id)
        manager.release(a_id)
        manager.release(b_id)
        root.end_run()


def test_per_parent_and_total_limits_under_nested_load():
    """Root fills 4 direct children; nested parents hit per-parent 4 and tree-wide 10."""
    root = RunController()
    root.begin_run()
    manager = SubAgentManager(root, max_concurrent=4, max_total=10)
    level1 = []
    level2 = []
    try:
        for _ in range(4):
            level1.append(manager.admit('session', root))
        assert manager.active_children('session') == 4
        with pytest.raises(RuntimeError, match='per-parent concurrency limit'):
            manager.admit('session', root)

        parent_a_id, parent_a = level1[0]
        parent_b_id, parent_b = level1[1]
        # Fill parent_a to its direct-child cap while total still has room (4+4=8).
        for _ in range(4):
            level2.append(manager.admit(parent_a_id, parent_a))
        assert manager.active_children(parent_a_id) == 4
        assert manager.active_count == 8
        with pytest.raises(RuntimeError, match='per-parent concurrency limit'):
            manager.admit(parent_a_id, parent_a)

        # parent_b consumes the remaining two total slots, then total cap fires.
        level2.append(manager.admit(parent_b_id, parent_b))
        level2.append(manager.admit(parent_b_id, parent_b))
        assert manager.active_count == 10
        with pytest.raises(RuntimeError, match='total concurrency limit'):
            manager.admit(parent_b_id, parent_b)
        with pytest.raises(RuntimeError, match='total concurrency limit'):
            manager.admit(parent_a_id, parent_a)
    finally:
        for run_id, _ in reversed(level2 + level1):
            manager.release(run_id)
        root.end_run()


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
    import agent.bootstrap.factory as factory
    from agent.bootstrap.factory import create_agent
    from agent.runtime.runner import AgentRunner
    from agent.execution.backend import BackendSelection
    from agent.execution.types import ExecutionMode
    from agent.models.client import build_chat_model
    from agent.models.stream_lifecycle import close_model_http_clients, live_stream_count
    from tests.model_stream_server import ScriptedSSEServer, Turn
    from tests.test_model_stream_cancel import Harness, settings_for
    call = {'id': 'delegate', 'name': 'start_task', 'args': {
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
        runner = AgentRunner(prepared=create_agent(model=parent_model, settings=cfg, skills=[], permission_mode='allow'), settings=cfg)
        harness = Harness(runner).start(runner.invoke, 'parent private history')
        try:
            child_connection = server.wait_for_connection(1)
            assert child_connection.hanging.wait(5)
            tools = {tool['function']['name'] if 'function' in tool else tool.get('name')
                     for tool in child_connection.body['tools']}
            # Default max_subagent_depth=2: depth-1 keeps start_task + direct-child log reader.
            assert 'start_task' in tools and 'read_task_log' in tools
            assert not {'request_human_input', 'start_background_task'} & tools
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
    import agent.bootstrap.factory as factory
    from deepagents.backends import StateBackend
    from agent.models.profile import ModelProfile
    from agent.bootstrap.config import Settings
    from agent.subagents.configuration import SubAgentConfig
    from agent.models.client import build_chat_model
    from agent.runtime.runner import AgentRunner
    from agent.execution.backend import BackendSelection
    from agent.execution.types import ExecutionMode
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
        AIMessage(content='', tool_calls=[{'id': 'd', 'name': 'start_task', 'args': {
            'description': 'start_task', 'subagent_type': 'general-purpose'}}]), AIMessage(content='reported failure')])
    runner = AgentRunner(model=parent, settings=cfg)
    runner.set_permission_mode('allow')
    try:
        assert runner.invoke('go').output == 'reported failure'
        assert len(created) == 1
        assert created[0].http_client.is_closed and created[0].http_async_client.is_closed
        assert runner.prepared.subagent_manager.active_count == 0
    finally:
        runner.close()


def test_ainvoke_cancel_targets_child_not_parent_or_siblings(tmp_path):
    import asyncio

    async def scenario() -> None:
        root = RunController()
        root.begin_run()
        owner = RunController()
        owner.begin_run()
        manager = SubAgentManager(root, max_concurrent=4, max_total=10)
        _sibling_id, sibling = manager.admit("session-root", root)
        started = threading.Event()
        proceed = threading.Event()
        child_holder: dict = {}

        def prepare(controller):
            child_holder["controller"] = controller

            class Graph:
                def stream(self, input, config, **kwargs):
                    started.set()
                    assert proceed.wait(5)
                    if controller.cancel_requested:
                        raise RuntimeError("Cancelled by parent")
                    yield {"messages": [AIMessage(content="should not finish")]}
            return SimpleNamespace(graph=Graph(), owns_model_clients=False)

        runtime = SubAgentRuntime(
            manager, SubAgentLogStore(tmp_path), prepare, "fake", owner_controller=owner,
        )
        task = asyncio.create_task(runtime.ainvoke(
            {"messages": [HumanMessage(content="work")]},
            {"configurable": {"thread_id": "session-a"}},
        ))
        # Do not block the event loop — ainvoke needs it to schedule to_thread.
        deadline = asyncio.get_running_loop().time() + 3
        while not started.is_set():
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        child = child_holder["controller"]
        task.cancel()
        # Confirm cancel reached this child before releasing the graph gate.
        cancel_deadline = asyncio.get_running_loop().time() + 3
        while not child.cancel_requested:
            if asyncio.get_running_loop().time() >= cancel_deadline:
                break
            await asyncio.sleep(0.01)
        assert child.cancel_requested
        assert not owner.cancel_requested
        assert not root.cancel_requested
        assert not sibling.cancel_requested
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Wait for release to reclaim the slot (sibling still occupies one).
        reclaim_deadline = asyncio.get_running_loop().time() + 3
        while manager.active_count != 1:
            if asyncio.get_running_loop().time() >= reclaim_deadline:
                break
            await asyncio.sleep(0.01)
        assert manager.active_count == 1
        manager.release(_sibling_id)
        root.end_run()

    asyncio.run(scenario())


def test_release_holds_slot_until_streams_close(tmp_path, monkeypatch):
    import agent.subagents.manager as manager_mod

    monkeypatch.setattr(manager_mod, "STREAM_CLOSE_WAIT", 0.05)
    root = RunController()
    root.begin_run()
    manager = SubAgentManager(root, max_concurrent=4, max_total=1)

    class StuckStream:
        closed = False
        on_closed = None

        def cancel(self):
            return None

        def wait_closed(self, timeout):
            return self.closed

        def mark_closed(self):
            self.closed = True
            if self.on_closed is not None:
                self.on_closed()

    run_id, child = manager.admit("session", root)
    stream = StuckStream()
    assert child.register_model_stream(stream)

    release_done = threading.Event()

    def do_release():
        manager.release(run_id)
        release_done.set()

    threading.Thread(target=do_release, daemon=True).start()
    assert release_done.wait(2)
    assert manager.active_count == 1
    assert child in root._children
    with pytest.raises(RuntimeError, match="total concurrency limit"):
        manager.admit("session", root)

    # Stream close lifecycle notifies the controller → automatic reap.
    stream.mark_closed()
    assert manager.active_count == 0
    assert child not in root._children
    freed_id, _ = manager.admit("session", root)
    manager.release(freed_id)
    root.end_run()


def test_background_thread_start_failure_releases_slot(tmp_path, monkeypatch):
    root = RunController()
    root.begin_run()
    manager = SubAgentManager(root, max_concurrent=4, max_total=2)
    runtime = SubAgentRuntime(
        manager, SubAgentLogStore(tmp_path),
        lambda controller: SimpleNamespace(graph=SimpleNamespace(stream=lambda *a, **k: iter([]))),
        "fake", owner_controller=root,
    )

    def boom(self):
        raise RuntimeError("cannot start thread")

    monkeypatch.setattr(threading.Thread, "start", boom)
    with pytest.raises(RuntimeError, match="cannot start thread"):
        runtime.start_background("bg work", parent="session", tool_call_id="t1")
    assert manager.active_count == 0
    assert manager.background_count == 0
    root.end_run()


def test_subagent_error_messages_redact_known_secrets(tmp_path):
    from agent.redaction import SecretRef

    secret = "configured-api-key-value-xyz"
    store = SubAgentLogStore(tmp_path, (SecretRef("llm.models.deepseek.api_key", secret),))
    root = RunController()
    root.begin_run()
    manager = SubAgentManager(root)

    def prepare(controller):
        raise RuntimeError(f"upstream failed with {secret}")

    runtime = SubAgentRuntime(manager, store, prepare, "fake", owner_controller=root)
    result = runtime.invoke(
        {"messages": [HumanMessage(content="go")]},
        {"configurable": {"thread_id": "session"}},
    )
    text = result["messages"][0].content
    assert secret not in text
    assert "$llm.models.deepseek.api_key" in text
    root.end_run()
