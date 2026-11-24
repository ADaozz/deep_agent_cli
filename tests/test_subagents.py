from dataclasses import replace

import pytest

from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, ToolMessage

from agent.bootstrap.config import Settings
from agent.subagents.configuration import SubAgentConfig
from agent.bootstrap.factory import create_agent
from agent.runtime.runner import AgentRunner
from agent.execution.backend import BackendSelection
from agent.execution.types import ExecutionMode
from agent.sessions.transcript import messages_to_transcript
from tests.conftest import graph_tool_names, scripted_model

START_TOOLS = {'start_task'}
QUERY_TOOLS = {'read_task', 'wait_task', 'cancel_task'}


def test_native_task_returns_only_final_result_and_isolates_tools(tmp_path, monkeypatch):
    import agent.bootstrap.factory as factory
    backend = StateBackend()
    monkeypatch.setattr(factory, 'select_backend', lambda cfg: BackendSelection(backend, ExecutionMode.SANDBOXED))
    cfg = Settings(config_dir=tmp_path / 'private')
    from langchain_core.tools import StructuredTool
    class MCP:
        snapshots = 0
        def snapshot(self):
            self.snapshots += 1
            return 1, [StructuredTool.from_function(lambda: "host", name="mcp__server__tool", description="Host-only tool")]
        def snapshot_includes_mcp(self, revision):
            return True
        def commit(self, revision):
            pass
        def discard(self, revision):
            pass
    mcp = MCP()
    child_prepared = []
    original = factory.build_agent

    def capture(spec, *args, **kwargs):
        prepared = original(spec, *args, **kwargs)
        if spec.role == 'subagent':
            child_prepared.append(prepared)
        return prepared

    monkeypatch.setattr(factory, 'build_agent', capture)
    model = scripted_model([
        AIMessage(content='', tool_calls=[{'name': 'start_task', 'args': {
            'description': 'Only delegated instructions'}, 'id': 'delegate'}]),
        AIMessage(content='private child final'),
        AIMessage(content='public parent answer'),
    ])
    events, deltas = [], []
    runner = AgentRunner(prepared=create_agent(model=model, skills=[], settings=cfg, mcp=mcp, permission_mode='allow'),
                         settings=cfg, on_event=events.append,
                         on_delta=lambda kind, text: deltas.append(text))
    try:
        result = runner.invoke('parent secret conversation')
        assert result.status == 'completed', result.error
        assert result.output == 'public parent answer'
        assert 'private child final' not in deltas
        child = child_prepared[0]
        assert child.backend is runner.prepared.backend
        assert child.execution_mode is ExecutionMode.SANDBOXED
        # Default max_subagent_depth=2: a sync child may start_task and read its own children.
        child_tools = graph_tool_names(child.graph)
        assert 'start_task' in child_tools and 'read_task' in child_tools
        assert not {'request_human_input', 'wait_task', 'cancel_task'} & child_tools
        child_start = child.graph.nodes['tools'].bound.tools_by_name['start_task']
        assert set(child_start.args) == {'description'}
        assert 'run_in_background' not in child_start.description
        assert not any(name.startswith("mcp") for name in graph_tool_names(child.graph))
        assert "mcp__server__tool" in graph_tool_names(runner.prepared.graph)
        assert mcp.snapshots == 1
        assert not child.interrupt_on
        assert child.spec.depth == 1
        state = runner.prepared.graph.get_state(runner._thread_config()).values
        assert [m.content for m in state['messages'] if isinstance(m, AIMessage)] == ['', 'public parent answer']
        assert len([m for m in state['messages'] if isinstance(m, ToolMessage)]) == 1
        tool_result = next(m for m in state['messages'] if isinstance(m, ToolMessage))
        assert 'private child final' in tool_result.content
        assert '"run_id"' in tool_result.content
        cards = [block for block in messages_to_transcript(state['messages']) if block.kind == 'tool']
        assert len(cards) == 1 and cards[0].name == 'start_task'
        assert cards[0].arguments['description'] == 'Only delegated instructions'
        assert cards[0].content == tool_result.content
        # admit → 1; release enters closing (still 1); streams clear → 0
        assert [e.result['count'] for e in events if e.type == 'subagents_changed'] == [1, 1, 0]
        logs = list((tmp_path / 'private' / 'subagents').rglob('*.jsonl'))
        assert len(logs) == 1
        import json
        records = [json.loads(line) for line in logs[0].read_text().splitlines()]
        assert [record['event'] for record in records] == ['start', 'finish']
        assert records[-1]['tool_count'] == 0
        assert 'private child final' in logs[0].read_text()
        assert 'parent secret conversation' not in logs[0].read_text()
    finally:
        runner.close()


def test_subagent_model_requests_stay_out_of_parent_tool_metrics(tmp_path, monkeypatch):
    import agent.bootstrap.factory as factory
    backend = StateBackend()
    monkeypatch.setattr(factory, 'select_backend', lambda cfg: BackendSelection(backend, ExecutionMode.SANDBOXED))
    cfg = Settings(config_dir=tmp_path / 'private')
    model = scripted_model([
        AIMessage(content='', tool_calls=[{'name': 'start_task', 'args': {
            'description': 'List files'}, 'id': 'delegate'}]),
        AIMessage(content='', tool_calls=[{'name': 'ls', 'args': {'path': '/'}, 'id': 'child-ls'}]),
        AIMessage(content='child final'),
        AIMessage(content='parent answer'),
    ])
    events = []
    runner = AgentRunner(prepared=create_agent(model=model, skills=[], settings=cfg, permission_mode='allow'),
                         settings=cfg, on_event=events.append)
    try:
        result = runner.invoke('delegate a listing')
        assert result.status == 'completed', result.error
        recorded = [e.result['tool_calls'] for e in events if e.type == 'model_metrics']
        assert recorded == [['start_task'], []]
    finally:
        runner.close()


def test_subagent_depth_gates_nested_start_task(tmp_path, monkeypatch):
    import agent.bootstrap.factory as factory
    from agent.bootstrap.factory import AgentSpec, build_agent
    from agent.runtime.control import RunController
    from agent.runtime.permissions import PermissionMode

    backend = StateBackend()
    selected = BackendSelection(backend, ExecutionMode.SANDBOXED)
    monkeypatch.setattr(factory, 'select_backend', lambda cfg: selected)

    def tools_at(depth: int, max_depth: int) -> set[str]:
        cfg = Settings(config_dir=tmp_path / f'd{depth}-{max_depth}',
                       subagents=SubAgentConfig(max_subagent_depth=max_depth))
        prepared = build_agent(
            AgentSpec(role='main' if depth == 0 else 'subagent', depth=depth, settings=cfg,
                      backend=backend, skills=()),
            scripted_model([AIMessage(content='ok')]), PermissionMode.ALLOW, None, RunController(),
            lambda: False, backend_selection=selected, settings=cfg,
        )
        return graph_tool_names(prepared.graph)

    assert 'start_task' in tools_at(0, 1) and 'read_task' in tools_at(0, 1)
    assert 'start_task' not in tools_at(1, 1) and 'read_task' not in tools_at(1, 1)
    assert 'start_task' in tools_at(0, 2) and 'start_task' in tools_at(1, 2)
    assert 'read_task' in tools_at(1, 2)
    assert 'wait_task' not in tools_at(1, 2) and 'cancel_task' not in tools_at(1, 2)
    assert 'start_task' not in tools_at(2, 2) and 'read_task' not in tools_at(2, 2)
    # Live delegation with max_subagent_depth=1 still yields a leaf child.
    by_depth: dict[int, object] = {}
    original = factory.build_agent

    def capture(spec, *args, **kwargs):
        prepared = original(spec, *args, **kwargs)
        by_depth[spec.depth] = prepared
        return prepared

    monkeypatch.setattr(factory, 'build_agent', capture)
    cfg = Settings(config_dir=tmp_path / 'live', subagents=SubAgentConfig(max_subagent_depth=1))
    model = scripted_model([
        AIMessage(content='', tool_calls=[{'name': 'start_task', 'args': {
            'description': 'leaf'}, 'id': 'd'}]),
        AIMessage(content='child done'),
        AIMessage(content='parent done'),
    ])
    runner = AgentRunner(prepared=create_agent(model=model, skills=[], settings=cfg, permission_mode='allow'),
                         settings=cfg)
    try:
        assert runner.invoke('go').status == 'completed'
        assert by_depth[1].spec.depth == 1
        assert 'start_task' not in graph_tool_names(by_depth[1].graph)
    finally:
        runner.close()


def test_disabled_task_and_unverified_backend_offer_no_delegation(tmp_path):
    cfg = Settings(config_dir=tmp_path)
    prepared = create_agent(model=scripted_model([AIMessage(content='done')]), backend=StateBackend(),
                            skills=[], settings=replace(cfg, subagents=SubAgentConfig(enabled=False)))
    assert not {'start_task', 'read_task', 'wait_task', 'cancel_task'} & graph_tool_names(prepared.graph)
    model = scripted_model([
        AIMessage(content='', tool_calls=[{'name': 'start_task', 'args': {
            'description': 'do work'}, 'id': 'd'}]),
        AIMessage(content='reported refusal'),
    ])
    # An unverified backend can only run in ask, and ask does not delegate.
    runner = AgentRunner(prepared=create_agent(model=model, backend=StateBackend(), skills=[], settings=cfg))
    try:
        assert runner.permission_mode() == 'ask'
        assert not START_TOOLS & graph_tool_names(runner.prepared.graph)
        assert runner.invoke('go').status == 'completed'
        messages = runner.prepared.graph.get_state(runner._thread_config()).values['messages']
        reply = next(m for m in messages if isinstance(m, ToolMessage))
        assert reply.status == 'error' and 'not a valid tool' in reply.content
        assert runner.prepared.subagent_manager.active_count == 0
    finally:
        runner.close()


@pytest.mark.parametrize(('mode', 'delegates'), [('ask', False), ('auto', True), ('allow', True)])
def test_delegation_and_child_permission_follow_the_parent_mode(tmp_path, monkeypatch, mode, delegates):
    import agent.bootstrap.factory as factory
    monkeypatch.setattr(factory, 'select_backend', lambda cfg: BackendSelection(StateBackend(), ExecutionMode.SANDBOXED))
    children = []
    original = factory.build_agent

    def capture(spec, *args, **kwargs):
        prepared = original(spec, *args, **kwargs)
        if spec.role == 'subagent':
            children.append(prepared)
        return prepared

    monkeypatch.setattr(factory, 'build_agent', capture)
    cfg = Settings(config_dir=tmp_path, resolved_deepseek_api_key='unit-key')
    model = scripted_model([
        AIMessage(content='', tool_calls=[{'name': 'start_task', 'id': 'd', 'args': {
            'description': 'do work'}}]),
        AIMessage(content='child final'),
        AIMessage(content='parent final'),
    ])
    runner = AgentRunner(prepared=create_agent(model=model, skills=[], settings=cfg, permission_mode=mode), settings=cfg)
    try:
        assert runner.permission_mode() == mode
        names = graph_tool_names(runner.prepared.graph)
        assert (START_TOOLS <= names) if delegates else not (START_TOOLS & names)
        # Tasks started before a switch to ask can still be inspected and stopped.
        assert QUERY_TOOLS <= names
        assert runner.invoke('go').status == 'completed'
        assert [child.permission_mode for child in children] == ([mode] if delegates else [])
    finally:
        runner.close()


def test_real_sandbox_native_concurrency_and_command_cancel(tmp_path):
    import threading
    import time
    import pytest
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import HumanMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from agent.execution.policy import SandboxConfig
    from agent.execution.types import SandboxUnavailableError
    from tests.test_model_stream_cancel import Harness

    class ParallelModel(BaseChatModel):
        @property
        def _llm_type(self):
            return 'parallel-test'
        def bind_tools(self, tools, **kwargs):
            return self
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            human = next(m.content for m in reversed(messages) if isinstance(m, HumanMessage))
            if isinstance(messages[-1], ToolMessage):
                answer = AIMessage(content='completed result')
            elif human.startswith('child-'):
                duration = 30 if human.endswith('cancel') else 1
                answer = AIMessage(content='', tool_calls=[{
                    'name': 'execute', 'id': 'execute-child', 'args': {
                        'command': f'touch {human}.started; sleep {duration}; touch {human}.finished'}}])
            else:
                names = ['child-cancel'] if human == 'cancel' else ['child-1', 'child-2', 'child-3']
                answer = AIMessage(content='', tool_calls=[{
                    'name': 'start_task', 'id': name, 'args': {
                        'description': name}} for name in names])
            return ChatResult(generations=[ChatGeneration(message=answer)])

    cfg = Settings(
        sandbox=SandboxConfig(workspace=tmp_path / 'workspace'),
        config_dir=tmp_path / 'private',
        subagents=SubAgentConfig(max_concurrent=2, max_subagent_depth=1),
    )
    cfg.sandbox.workspace.mkdir()
    try:
        runner = AgentRunner(model=ParallelModel(), settings=cfg)
        runner.set_permission_mode('allow')
    except SandboxUnavailableError as exc:
        pytest.skip(str(exc))
    try:
        result = runner.invoke('parallel')
        assert result.status == 'completed', result.error
        messages = runner.prepared.graph.get_state(runner._thread_config()).values['messages']
        results = [m.content for m in messages if isinstance(m, ToolMessage)]
        assert sum('concurrency limit' in text for text in results) == 1
        assert sum('completed result' in text for text in results) == 2
        assert len(list(cfg.sandbox.workspace.glob('*.finished'))) == 2
        assert runner.prepared.subagent_manager.active_count == 0
        pool = runner.sandbox_pool
        assert 'pool alive' in runner.prepared.backend.execute('printf "pool alive"').output
        assert runner.sandbox_pool is pool

        harness = Harness(runner).start(runner.invoke, 'cancel')
        harness.wait_for(lambda: (cfg.sandbox.workspace / 'child-cancel.started').exists())
        runner.request_cancel()
        assert harness.join(8)
        assert harness.error is None
        assert harness.result.status == 'cancelled'
        assert runner.prepared.subagent_manager.active_count == 0
        assert not (cfg.sandbox.workspace / 'child-cancel.finished').exists()
        assert 'still alive' in runner.prepared.backend.execute('printf "still alive"').output
    finally:
        runner.close()


def test_log_tool_reads_only_its_parent_after_restart(tmp_path):
    from uuid import uuid4
    from agent.subagents.log_store import SubAgentLogStore
    run_id = uuid4().hex
    SubAgentLogStore(tmp_path / 'subagents').append('parent', run_id, 'finish', status='completed', output='saved event')
    cfg = Settings(config_dir=tmp_path)
    model = scripted_model([
        AIMessage(content='', tool_calls=[{'name': 'read_task', 'id': 'log', 'args': {'run_id': run_id, 'include_logs': True}}]),
        AIMessage(content='read completed'),
    ])
    runner = AgentRunner(model=model, backend=StateBackend(), settings=cfg, thread_id='parent')
    try:
        assert runner.invoke('query previous run').status == 'completed'
        messages = runner.prepared.graph.get_state(runner._thread_config()).values['messages']
        assert 'saved event' in next(m.content for m in messages if isinstance(m, ToolMessage))
    finally:
        runner.close()


def test_subagent_model_inheritance_updates_after_main_rebuild(tmp_path, monkeypatch):
    import agent.bootstrap.factory as factory
    from agent.models.profile import ModelProfile
    monkeypatch.setattr(factory, 'select_backend', lambda cfg: BackendSelection(StateBackend(), ExecutionMode.SANDBOXED))
    def responses(label):
        return scripted_model([
            AIMessage(content='', tool_calls=[{'name': 'start_task', 'id': label, 'args': {
                'description': label}}]),
            AIMessage(content=f'child-{label}'), AIMessage(content=f'parent-{label}')])
    first, second = responses('first'), responses('second')
    cfg = Settings(config_dir=tmp_path, llm_profiles=(ModelProfile('first', 'first'), ModelProfile('second', 'second')),
                   llm_default='first')
    seen = []
    original = factory.build_agent
    def capture(spec, model, *args, **kwargs):
        if spec.role == 'subagent':
            seen.append(model)
        return original(spec, model, *args, **kwargs)
    monkeypatch.setattr(factory, 'build_agent', capture)
    monkeypatch.setattr('agent.runtime.runner.build_chat_model', lambda *args, **kwargs: second)
    runner = AgentRunner(model=first, settings=cfg)
    runner.set_permission_mode('allow')
    try:
        manager = runner.prepared.subagent_manager
        assert runner.invoke('first').output == 'parent-first'
        runner.switch_model('second')
        assert runner.invoke('second').output == 'parent-second'
        assert seen == [first, second]
        assert runner.prepared.subagent_manager is manager
    finally:
        runner.close()


def test_subagent_model_and_effort_override(tmp_path, monkeypatch):
    import agent.bootstrap.factory as factory
    from agent.models.profile import ModelProfile
    monkeypatch.setattr(factory, 'select_backend', lambda cfg: BackendSelection(StateBackend(), ExecutionMode.SANDBOXED))
    parent = scripted_model([
        AIMessage(content='', tool_calls=[{'name': 'start_task', 'id': 'd', 'args': {
            'description': 'child'}}]), AIMessage(content='parent')])
    child = scripted_model([AIMessage(content='child')])
    cfg = Settings(config_dir=tmp_path, llm_profiles=(ModelProfile('default', 'parent'),
                   ModelProfile('other', 'child', reasoning_efforts=('low',))),
                   subagents=SubAgentConfig(model='other', reasoning_effort='low'))
    calls = []
    def build(profile, **kwargs):
        calls.append((profile.id, kwargs['reasoning_effort']))
        return child
    monkeypatch.setattr(factory, 'build_chat_model', build)
    runner = AgentRunner(model=parent, settings=cfg)
    runner.set_permission_mode('allow')
    try:
        assert runner.invoke('go').output == 'parent'
        assert calls == [('other', 'low')]
    finally:
        runner.close()


def test_prepared_runner_honors_subagent_disable_setting(tmp_path):
    cfg = Settings(config_dir=tmp_path, subagents=SubAgentConfig(enabled=False))
    runner = AgentRunner(prepared=create_agent(model=scripted_model([AIMessage(content='done')]),
                         backend=StateBackend(), skills=[]), settings=cfg)
    try:
        assert 'start_task' not in graph_tool_names(runner.prepared.graph)
    finally:
        runner.close()



def test_resumed_pending_task_result_updates_original_card(tmp_path, monkeypatch):
    import agent.bootstrap.factory as factory
    from agent.cli.state import CliState, ToolBlock
    from langchain_core.messages import HumanMessage
    monkeypatch.setattr(factory, 'select_backend', lambda cfg: BackendSelection(StateBackend(), ExecutionMode.SANDBOXED))
    cfg = Settings(config_dir=tmp_path)
    runner = AgentRunner(model=scripted_model([AIMessage(content='private result'), AIMessage(content='public answer')]), settings=cfg)
    runner.set_permission_mode('allow')
    assert runner.apply_pending_config_if_idle()
    try:
        messages = [HumanMessage(content='old parent input'), AIMessage(content='', tool_calls=[{
            'name': 'start_task', 'id': 'restored-task', 'args': {
                'description': 'pending delegated task'}}])]
        runner.prepared.graph.update_state(runner._thread_config(), {'messages': messages}, as_node='model')
        ui = CliState()
        ui.load_transcript(messages_to_transcript(messages))
        result = runner._stream(None, on_event=ui.apply)
        assert result.status == 'completed', result.error
        assert result.output == 'public answer'
        cards = [block for block in ui.blocks if isinstance(block, ToolBlock)]
        assert len(cards) == 1 and cards[0].name == 'start_task'
        assert cards[0].arguments['description'] == 'pending delegated task'
        assert cards[0].status == 'completed' and 'private result' in cards[0].output
        checkpoint = runner.prepared.graph.get_state(runner._thread_config()).values['messages']
        assert 'private result' in next(m.content for m in checkpoint if isinstance(m, ToolMessage))
    finally:
        runner.close()


@pytest.mark.parametrize('api', ['responses', 'chat_completions'])
@pytest.mark.parametrize('override_model', [False, True])
@pytest.mark.parametrize('parent_effort,child_effort,expected', [
    ('high', 'default', None), ('low', 'default', None),
    ('high', 'low', 'low'), ('low', 'high', 'high'), ('high', None, None),
])
def test_child_request_effort_is_independent_of_parent(
    api, override_model, parent_effort, child_effort, expected, tmp_path, monkeypatch,
):
    import agent.bootstrap.factory as factory
    from agent.models.profile import ModelProfile
    from agent.models.client import build_chat_model
    from agent.models.stream_lifecycle import close_model_http_clients
    from tests.model_stream_server import ScriptedSSEServer, Turn
    call = {'name': 'start_task', 'id': 'delegate', 'args': {
        'description': 'child-only task'}}
    with ScriptedSSEServer(api, [Turn(tool_calls=[call]), Turn(text='child answer'), Turn(text='parent answer')]) as server:
        main = ModelProfile('main', 'main-model', api=api, base_url=server.base_url,
                            reasoning_efforts=('low', 'high'), max_retries=0)
        # A different child with no effort capabilities reproduces the review
        # finding: the main model's high must never reach this profile.
        other = ModelProfile('other', 'child-model', api=api, base_url=server.base_url,
                             reasoning_efforts=('low', 'high') if expected else (), max_retries=0)
        cfg = Settings(llm_profiles=(main, other), llm_default='main', config_dir=tmp_path,
                       subagents=SubAgentConfig(model='other' if override_model else None, reasoning_effort=child_effort))
        parent = build_chat_model(main, reasoning_effort=parent_effort)
        children = []
        def build(profile, **kwargs):
            child = build_chat_model(profile, **kwargs)
            children.append(child)
            return child
        monkeypatch.setattr(factory, 'build_chat_model', build)
        monkeypatch.setattr(factory, 'select_backend', lambda cfg: BackendSelection(StateBackend(), ExecutionMode.SANDBOXED))
        runner = AgentRunner(model=parent, settings=cfg)
        runner.set_permission_mode('allow')
        try:
            assert runner.invoke('delegate').output == 'parent answer'
            messages = runner.prepared.graph.get_state(runner._thread_config()).values['messages']
            assert 'child answer' in next(m.content for m in messages if isinstance(m, ToolMessage))
            assert len(server.connections) == 3
            request = server.connections[1].body
            assert request['model'] == ('child-model' if override_model else 'main-model')
            if api == 'responses':
                assert server.connections[0].body['reasoning']['effort'] == parent_effort
                if expected is None:
                    assert 'effort' not in (request.get('reasoning') or {})
                else:
                    assert request['reasoning']['effort'] == expected
            else:
                assert server.connections[0].body['reasoning_effort'] == parent_effort
                if expected is None:
                    assert 'reasoning_effort' not in request
                else:
                    assert request['reasoning_effort'] == expected
            assert len(children) == 1 and children[0] is not parent
            assert children[0].http_client.is_closed and children[0].http_async_client.is_closed
            assert not parent.http_client.is_closed and not parent.http_async_client.is_closed
        finally:
            runner.close()
            close_model_http_clients(parent)


def test_custom_injected_model_is_not_replaced_by_default_profile(tmp_path, monkeypatch):
    import agent.bootstrap.factory as factory
    monkeypatch.setattr(factory, 'select_backend', lambda cfg: BackendSelection(StateBackend(), ExecutionMode.SANDBOXED))
    def unexpected_build(*args, **kwargs):
        pytest.fail('custom injected model silently replaced by configured default')
    monkeypatch.setattr(factory, 'build_chat_model', unexpected_build)
    model = scripted_model([
        AIMessage(content='', tool_calls=[{'name': 'start_task', 'id': 'd', 'args': {
            'description': 'custom task'}}]),
        AIMessage(content='custom child'), AIMessage(content='custom parent')])
    runner = AgentRunner(model=model, settings=Settings(config_dir=tmp_path))
    runner.set_permission_mode('allow')
    try:
        assert runner.invoke('go').output == 'custom parent'
    finally:
        runner.close()


def test_custom_model_requires_explicit_profile_for_effort_override(tmp_path, monkeypatch):
    import agent.bootstrap.factory as factory
    from agent.models.profile import ModelProfile
    monkeypatch.setattr(factory, 'select_backend', lambda cfg: BackendSelection(StateBackend(), ExecutionMode.SANDBOXED))
    cfg = Settings(config_dir=tmp_path, llm_profiles=(ModelProfile('default', 'configured', reasoning_efforts=('low',)),),
                   subagents=SubAgentConfig(reasoning_effort='low'))
    model = scripted_model([
        AIMessage(content='', tool_calls=[{'name': 'start_task', 'id': 'd', 'args': {
            'description': 'custom task'}}]), AIMessage(content='reported')])
    monkeypatch.setattr(factory, 'build_chat_model', lambda *args, **kwargs: pytest.fail('unexpected profile fallback'))
    runner = AgentRunner(model=model, settings=cfg)
    runner.set_permission_mode('allow')
    try:
        assert runner.invoke('go').output == 'reported'
        messages = runner.prepared.graph.get_state(runner._thread_config()).values['messages']
        assert 'custom BaseChatModel requires subagents.model' in next(m.content for m in messages if isinstance(m, ToolMessage))
    finally:
        runner.close()


def test_log_redacts_only_configured_model_and_deepseek_keys(tmp_path, monkeypatch):
    import agent.bootstrap.factory as factory
    from agent.models.profile import ModelProfile
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.setenv('UNKNOWN_SERVICE_TOKEN', 'unknown-output-key')
    monkeypatch.setattr(factory, 'select_backend', lambda cfg: BackendSelection(StateBackend(), ExecutionMode.SANDBOXED))
    cfg = Settings(config_dir=tmp_path, resolved_deepseek_api_key='known-deepseek-key', llm_profiles=(
        ModelProfile('default', 'main', api_key='known-main-key'),
        ModelProfile('unused', 'unused', api_key='known-unused-key')))
    description = 'known-main-key known-unused-key known-deepseek-key password=unknown-output-key'
    parent = scripted_model([
        AIMessage(content='', tool_calls=[{'name': 'start_task', 'id': 'd', 'args': {
            'description': description}}]),
        AIMessage(content='child result'), AIMessage(content='parent result')])
    runner = AgentRunner(model=parent, settings=cfg)
    runner.set_permission_mode('allow')
    try:
        assert runner.invoke('go').output == 'parent result'
        log = next((tmp_path / 'subagents').rglob('*.jsonl')).read_text()
        assert all(key not in log for key in ('known-main-key', 'known-unused-key', 'known-deepseek-key'))
        assert 'password=unknown-output-key' in log
        assert '$llm.api_key' in log or '$llm.models' in log
        assert '$llm.models.deepseek.api_key' in log
    finally:
        runner.close()
