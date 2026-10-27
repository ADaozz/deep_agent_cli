from dataclasses import replace

from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, ToolMessage

from agent.config import Settings, SubAgentConfig
from agent.factory import create_agent
from agent.runner import AgentRunner
from agent.sandbox import BackendSelection, ExecutionMode
from agent.session import messages_to_transcript
from tests.conftest import graph_tool_names, scripted_model


def test_native_task_returns_only_final_result_and_isolates_tools(tmp_path, monkeypatch):
    import agent.factory as factory
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
        AIMessage(content='', tool_calls=[{'name': 'task', 'args': {
            'description': 'Only delegated instructions', 'subagent_type': 'general-purpose'}, 'id': 'delegate'}]),
        AIMessage(content='private child final'),
        AIMessage(content='public parent answer'),
    ])
    events, deltas = [], []
    runner = AgentRunner(prepared=create_agent(model=model, skills=[], settings=cfg, mcp=mcp),
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
        assert not {'task', 'request_human_input', 'read_subagent_log'} & graph_tool_names(child.graph)
        assert not any(name.startswith("mcp") for name in graph_tool_names(child.graph))
        assert "mcp__server__tool" in graph_tool_names(runner.prepared.graph)
        assert mcp.snapshots == 1
        assert not child.interrupt_on
        state = runner.prepared.graph.get_state(runner._thread_config()).values
        assert [m.content for m in state['messages'] if isinstance(m, AIMessage)] == ['', 'public parent answer']
        assert len([m for m in state['messages'] if isinstance(m, ToolMessage)]) == 1
        tool_result = next(m for m in state['messages'] if isinstance(m, ToolMessage))
        assert 'private child final' in tool_result.content
        assert 'run_id=' in tool_result.content
        assert not [block for block in messages_to_transcript(state['messages']) if block.kind == 'tool']
        assert [e.result['count'] for e in events if e.type == 'subagents_changed'] == [1, 0]
        logs = list((tmp_path / 'private' / 'subagents').rglob('*.jsonl'))
        assert len(logs) == 1
        assert 'model_start' in logs[0].read_text()
        assert 'private child final' in logs[0].read_text()
        assert 'parent secret conversation' not in logs[0].read_text()
    finally:
        runner.close()


def test_disabled_task_and_custom_backend_refusal(tmp_path):
    cfg = Settings(config_dir=tmp_path)
    prepared = create_agent(model=scripted_model([AIMessage(content='done')]), backend=StateBackend(),
                            skills=[], settings=replace(cfg, subagents=SubAgentConfig(enabled=False)))
    assert not {'task', 'read_subagent_log'} & graph_tool_names(prepared.graph)
    model = scripted_model([
        AIMessage(content='', tool_calls=[{'name': 'task', 'args': {
            'description': 'do work', 'subagent_type': 'general-purpose'}, 'id': 'd'}]),
        AIMessage(content='reported refusal'),
    ])
    runner = AgentRunner(prepared=create_agent(model=model, backend=StateBackend(), skills=[], settings=cfg))
    try:
        assert runner.invoke('go').status == 'completed'
        messages = runner.prepared.graph.get_state(runner._thread_config()).values['messages']
        assert 'verified SANDBOXED' in next(m.content for m in messages if isinstance(m, ToolMessage))
        assert runner.prepared.subagent_manager.active_count == 0
    finally:
        runner.close()


def test_real_sandbox_native_concurrency_and_command_cancel(tmp_path):
    import threading
    import time
    import pytest
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import HumanMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from agent.config import SandboxConfig
    from agent.sandbox import SandboxUnavailableError
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
                    'name': 'task', 'id': name, 'args': {
                        'description': name, 'subagent_type': 'general-purpose'}} for name in names])
            return ChatResult(generations=[ChatGeneration(message=answer)])

    cfg = Settings(sandbox=SandboxConfig(workspace=tmp_path / 'workspace'), config_dir=tmp_path / 'private')
    cfg.sandbox.workspace.mkdir()
    try:
        runner = AgentRunner(model=ParallelModel(), settings=cfg)
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
        AIMessage(content='', tool_calls=[{'name': 'read_subagent_log', 'id': 'log', 'args': {'run_id': run_id}}]),
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
    import agent.factory as factory
    from agent.config import ModelProfile
    monkeypatch.setattr(factory, 'select_backend', lambda cfg: BackendSelection(StateBackend(), ExecutionMode.SANDBOXED))
    def responses(label):
        return scripted_model([
            AIMessage(content='', tool_calls=[{'name': 'task', 'id': label, 'args': {
                'description': label, 'subagent_type': 'general-purpose'}}]),
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
    monkeypatch.setattr('agent.runner.build_chat_model', lambda *args, **kwargs: second)
    runner = AgentRunner(model=first, settings=cfg)
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
    import agent.factory as factory
    from agent.config import ModelProfile
    monkeypatch.setattr(factory, 'select_backend', lambda cfg: BackendSelection(StateBackend(), ExecutionMode.SANDBOXED))
    parent = scripted_model([
        AIMessage(content='', tool_calls=[{'name': 'task', 'id': 'd', 'args': {
            'description': 'child', 'subagent_type': 'general-purpose'}}]), AIMessage(content='parent')])
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
        assert 'task' not in graph_tool_names(runner.prepared.graph)
    finally:
        runner.close()



def test_resumed_pending_task_result_stays_hidden(tmp_path, monkeypatch):
    import agent.factory as factory
    from agent.cli.state import CliState, ToolBlock
    from langchain_core.messages import HumanMessage
    monkeypatch.setattr(factory, 'select_backend', lambda cfg: BackendSelection(StateBackend(), ExecutionMode.SANDBOXED))
    cfg = Settings(config_dir=tmp_path)
    runner = AgentRunner(model=scripted_model([AIMessage(content='private result'), AIMessage(content='public answer')]), settings=cfg)
    try:
        messages = [HumanMessage(content='old parent input'), AIMessage(content='', tool_calls=[{
            'name': 'task', 'id': 'restored-task', 'args': {
                'description': 'pending delegated task', 'subagent_type': 'general-purpose'}}])]
        runner.prepared.graph.update_state(runner._thread_config(), {'messages': messages}, as_node='model')
        ui = CliState()
        ui.load_transcript(messages_to_transcript(messages))
        result = runner._stream(None, on_event=ui.apply)
        assert result.status == 'completed', result.error
        assert result.output == 'public answer'
        assert not any(isinstance(block, ToolBlock) for block in ui.blocks)
        checkpoint = runner.prepared.graph.get_state(runner._thread_config()).values['messages']
        assert 'private result' in next(m.content for m in checkpoint if isinstance(m, ToolMessage))
    finally:
        runner.close()
