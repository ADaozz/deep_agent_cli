"""Background delegation: the parent keeps working and is notified on completion."""
import asyncio
from contextlib import closing
from dataclasses import replace
import re
import threading
import time
from typing import Any

import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent.bootstrap.config import Settings
from agent.bootstrap.factory import create_agent
from agent.cli.rendering import render_transcript
from agent.cli.state import CliState, MessageBlock, ToolBlock
from agent.execution.backend import BackendSelection
from agent.execution.types import ExecutionMode
from agent.models.profile import ModelProfile
from agent.runtime.contracts import RunEvent
from agent.runtime.runner import AgentRunner
from agent.sessions.store import SessionStore
from agent.sessions.transcript import messages_to_transcript, settle_restored_tools
from agent.subagents.configuration import SubAgentConfig
from agent.subagents.notifications import NOTIFICATION_KEY, notification_message, parse_notification
from tests.cli_support import plain
from tests.conftest import ScriptedToolModel, graph_tool_names, scripted_model


BACKGROUND = {'start_background_task', 'check_background_task', 'cancel_background_task'}


class GatedModel(ScriptedToolModel):
    """A child model that answers only after the test opens its gate."""
    gate: Any = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        assert self.gate.wait(10)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class ReactiveModel(ScriptedToolModel):
    """A parent model that decides from the conversation, e.g. to reuse a run ID."""
    respond: Any = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        return ChatResult(generations=[ChatGeneration(message=self.respond(messages))])


def start_call(identifier='bg', description='child work'):
    return {'id': identifier, 'name': 'start_background_task', 'args': {'description': description}}


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError('condition not reached')


class Harness:
    """A runner whose children use their own gated models, never the parent's script."""

    def __init__(self, tmp_path, monkeypatch, parent, answers=('child final',), *,
                 max_concurrent=2, store=None, extra_tools=None):
        import agent.bootstrap.factory as factory
        self.gate = threading.Event()
        self.events: list[RunEvent] = []
        self.listeners: list[Any] = []
        answers = iter(answers)
        monkeypatch.setattr(factory, 'select_backend',
                            lambda cfg: BackendSelection(StateBackend(), ExecutionMode.SANDBOXED))
        monkeypatch.setattr(factory, 'build_chat_model', lambda profile, **kwargs: GatedModel(
            messages=iter([AIMessage(content=next(answers))]), gate=self.gate))
        self.cfg = Settings(
            config_dir=tmp_path / 'private',
            llm_profiles=(ModelProfile('default', 'parent'), ModelProfile('child', 'child')),
            subagents=SubAgentConfig(model='child', max_concurrent=max_concurrent),
        )
        model = parent if not isinstance(parent, list) else scripted_model(parent)
        if extra_tools:
            self.runner = AgentRunner(
                prepared=create_agent(model=model, settings=self.cfg, skills=[], extra_tools=extra_tools),
                settings=self.cfg, on_event=self.emit)
        else:
            self.runner = AgentRunner(model=model, settings=self.cfg, on_event=self.emit, session_store=store)
        self.manager = self.runner.prepared.subagent_manager

    def emit(self, event):
        self.events.append(event)
        for listener in self.listeners:
            listener(event)

    def finished(self):
        return [event for event in self.events if event.type == 'subagent_finished']

    def messages(self):
        return self.runner.prepared.graph.get_state(self.runner._thread_config()).values['messages']

    def close(self):
        self.gate.set()
        self.runner.close()


def test_start_returns_immediately_and_idle_delivery_wakes_parent(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch, [
        AIMessage(content='', tool_calls=[start_call()]),
        AIMessage(content='working meanwhile'),
        AIMessage(content='reported child result'),
    ])
    runner = harness.runner
    try:
        result = runner.invoke('parent private request')
        # The child has not answered, yet the parent finished its turn.
        assert result.status == 'completed' and result.output == 'working meanwhile'
        assert harness.manager.background_count == 1 and runner.background_task_count() == 1
        launch = next(m for m in harness.messages() if isinstance(m, ToolMessage))
        assert launch.content.startswith('Background subagent started (run_id=')
        assert not harness.finished() and runner.control.pending_notification_count() == 0
        assert runner.deliver_notifications() is None

        # A sandbox reconfiguration would drop the child's processes.
        runner.configuration.pending_sandbox_mode = 'full-access'
        assert runner.apply_pending_config_if_idle() is False
        runner.configuration.pending_sandbox_mode = None

        harness.gate.set()
        wait_until(lambda: harness.finished())
        event = harness.finished()[0]
        assert event.result['status'] == 'completed' and event.result['tool_call_id'] == 'bg'
        assert 'child final' in event.content
        assert harness.manager.background_count == 0 and harness.manager.active_count == 0
        assert runner.control.pending_notification_count() == 1
        # Host results are not user input: nothing to reclaim, nothing blocks a switch.
        assert runner.control.pending_message_texts() == [] and runner.take_unapplied_messages() == []

        woken = runner.deliver_notifications()
        assert woken.status == 'completed' and woken.output == 'reported child result'
        assert runner.control.pending_notification_count() == 0
        notice = harness.messages()[-2]
        assert isinstance(notice, HumanMessage) and 'child final' in notice.content
        assert notice.additional_kwargs[NOTIFICATION_KEY]['tool_call_id'] == 'bg'
        assert 'parent private request' not in next(
            (tmp_path / 'private' / 'subagents').rglob('*.jsonl')).read_text()

        blocks = messages_to_transcript(harness.messages())
        assert [b.content for b in blocks if b.kind == 'user'] == ['parent private request']
        card = next(b for b in blocks if b.kind == 'tool')
        assert card.name == 'start_background_task' and card.status == 'completed'
        assert 'child final' in card.content and not card.is_error
    finally:
        harness.close()


def test_result_is_injected_at_the_next_model_boundary_of_a_running_parent(tmp_path, monkeypatch):
    holder = {}

    @tool
    def wait_for_child() -> str:
        """Block the parent until its background child reported."""
        holder['harness'].gate.set()
        wait_until(lambda: holder['harness'].runner.control.pending_notification_count() == 1)
        return 'waited'

    harness = holder['harness'] = Harness(tmp_path, monkeypatch, [
        AIMessage(content='', tool_calls=[start_call()]),
        AIMessage(content='', tool_calls=[{'id': 'wait', 'name': 'wait_for_child', 'args': {}}]),
        AIMessage(content='saw the result in the same run'),
    ], extra_tools=[wait_for_child])
    try:
        result = harness.runner.invoke('go')
        assert result.status == 'completed' and result.output == 'saw the result in the same run'
        kinds = [type(m).__name__ for m in harness.messages()]
        assert kinds == ['HumanMessage', 'AIMessage', 'ToolMessage', 'AIMessage', 'ToolMessage',
                         'HumanMessage', 'AIMessage']
        assert parse_notification(harness.messages()[5])[0]['status'] == 'completed'
        assert harness.runner.control.pending_notification_count() == 0
        assert harness.runner.deliver_notifications() is None
    finally:
        harness.close()


def test_result_arriving_after_the_final_answer_continues_the_same_invoke(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch, [
        AIMessage(content='', tool_calls=[start_call()]),
        AIMessage(content='first answer'),
        AIMessage(content='second answer'),
    ])

    boundaries = harness.runner._resume_config_boundaries

    def report_after_last_model_boundary(result, *args):
        # The graph already returned: only the run loop can still pick this up.
        if not harness.finished():
            harness.gate.set()
            wait_until(lambda: harness.runner.control.pending_notification_count() == 1)
        return boundaries(result, *args)

    monkeypatch.setattr(harness.runner, '_resume_config_boundaries', report_after_last_model_boundary)
    try:
        assert harness.runner.invoke('go').output == 'second answer'
        assert [e.type for e in harness.events].count('turn_completed') == 2
        assert [e.type for e in harness.events].count('run_completed') == 1
        assert harness.runner.control.pending_notification_count() == 0
    finally:
        harness.close()


def test_background_and_synchronous_tasks_share_the_concurrency_limit(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch, [
        AIMessage(content='', tool_calls=[start_call('first')]),
        AIMessage(content='', tool_calls=[
            start_call('second'),
            {'id': 'sync', 'name': 'task', 'args': {'description': 'x', 'subagent_type': 'general-purpose'}},
        ]),
        AIMessage(content='limit reported'),
    ], max_concurrent=1)
    try:
        assert harness.runner.invoke('go').output == 'limit reported'
        results = {m.tool_call_id: m.content for m in harness.messages() if isinstance(m, ToolMessage)}
        assert results['first'].startswith('Background subagent started')
        assert 'concurrency limit (1)' in results['second'] and results['second'].startswith('Subagent failed:')
        assert 'concurrency limit (1)' in results['sync']
        assert harness.manager.background_count == 1
        cards = {b.tool_call_id: b for b in messages_to_transcript(harness.messages()) if b.kind == 'tool'}
        assert cards['second'].status == 'error' and cards['first'].status == 'running'
    finally:
        harness.close()


def test_cancel_and_check_tools_use_the_run_id_and_send_no_notification(tmp_path, monkeypatch):
    def respond(messages):
        last = messages[-1]
        if isinstance(last, ToolMessage) and last.name == 'start_background_task':
            run_id = re.search(r'run_id=([0-9a-f]{32})', last.content).group(1)
            return AIMessage(content='', tool_calls=[
                {'id': 'list', 'name': 'check_background_task', 'args': {}},
                {'id': 'stop', 'name': 'cancel_background_task', 'args': {'run_id': run_id}},
                {'id': 'missing', 'name': 'cancel_background_task', 'args': {'run_id': 'f' * 32}},
            ])
        if isinstance(last, ToolMessage):
            return AIMessage(content='stopped it')
        return AIMessage(content='', tool_calls=[start_call()])

    harness = Harness(tmp_path, monkeypatch, ReactiveModel(messages=iter([]), respond=respond))
    try:
        assert harness.runner.invoke('go').output == 'stopped it'
        results = {m.tool_call_id: m.content for m in harness.messages() if isinstance(m, ToolMessage)}
        assert '"status": "running"' in results['list'] and 'child work' in results['list']
        assert results['stop'].startswith('Cancellation requested')
        assert 'not found' in results['missing']
        harness.gate.set()
        wait_until(lambda: harness.finished())
        assert harness.finished()[0].result['status'] == 'cancelled'
        assert harness.runner.control.pending_notification_count() == 0
        assert harness.manager.active_count == 0
        task = harness.manager.background_tasks()[0]
        assert task.status == 'cancelled' and harness.manager.background_task(task.run_id, 'other') is None
    finally:
        harness.close()


def test_idle_cancel_stops_background_tasks_without_waking_parent(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch, [
        AIMessage(content='', tool_calls=[start_call('a', 'one'), start_call('b', 'two')]),
        AIMessage(content='idle now'),
    ], answers=('one', 'two'))
    try:
        assert harness.runner.invoke('go').output == 'idle now'
        assert harness.manager.background_count == 2
        harness.runner.request_cancel()
        harness.gate.set()
        wait_until(lambda: len(harness.finished()) == 2)
        assert {e.result['status'] for e in harness.finished()} == {'cancelled'}
        assert harness.runner.control.pending_notification_count() == 0
        assert harness.manager.background_count == 0 and harness.manager.active_count == 0
    finally:
        harness.close()


def test_leaving_the_session_drops_tasks_and_their_results(tmp_path, monkeypatch):
    with closing(SessionStore(tmp_path / 'sessions.sqlite3')) as store:
        harness = Harness(tmp_path, monkeypatch, [
            AIMessage(content='', tool_calls=[start_call()]),
            AIMessage(content='idle now'),
            AIMessage(content='new conversation'),
        ], store=store)
        try:
            assert harness.runner.invoke('go').output == 'idle now'
            task = harness.manager.background_tasks()[0]
            harness.runner.control.notify({'run_id': 'f' * 32, 'tool_call_id': 'x', 'status': 'completed',
                                           'parent': harness.runner.thread_id, 'content': 'stale'})
            harness.runner.new_session()
            assert harness.manager.background_count == 0
            assert harness.runner.control.pending_notification_count() == 0
            harness.gate.set()
            task.thread.join(5)
            assert not task.thread.is_alive() and harness.manager.active_count == 0
            assert not harness.finished() and harness.runner.control.pending_notification_count() == 0
            assert harness.runner.invoke('fresh').output == 'new conversation'
            assert not any(parse_notification(m) for m in harness.messages())
        finally:
            harness.close()


def test_close_stops_running_background_tasks(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch, [
        AIMessage(content='', tool_calls=[start_call()]), AIMessage(content='idle now')])
    assert harness.runner.invoke('go').output == 'idle now'
    task = harness.manager.background_tasks()[0]
    threading.Timer(0.2, harness.gate.set).start()
    harness.runner.close()
    assert not task.thread.is_alive() and harness.manager.active_count == 0


def test_configuration_and_child_tool_isolation(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='subagents.background'):
        SubAgentConfig(background='yes')
    cfg = Settings(config_dir=tmp_path)
    model = scripted_model([AIMessage(content='done')])
    enabled = create_agent(model=model, backend=StateBackend(), skills=[], settings=cfg)
    assert BACKGROUND <= graph_tool_names(enabled.graph)
    for subagents in (SubAgentConfig(background=False), SubAgentConfig(enabled=False)):
        prepared = create_agent(model=model, backend=StateBackend(), skills=[],
                                settings=replace(cfg, subagents=subagents))
        assert not BACKGROUND & graph_tool_names(prepared.graph)
    assert 'task' in graph_tool_names(create_agent(
        model=model, backend=StateBackend(), skills=[],
        settings=replace(cfg, subagents=SubAgentConfig(background=False))).graph)

    import agent.bootstrap.factory as factory
    children = []
    original = factory.build_agent

    def capture(spec, *args, **kwargs):
        prepared = original(spec, *args, **kwargs)
        if spec.role == 'subagent':
            children.append(prepared)
        return prepared

    monkeypatch.setattr(factory, 'build_agent', capture)
    harness = Harness(tmp_path, monkeypatch, [
        AIMessage(content='', tool_calls=[start_call()]), AIMessage(content='idle now')])
    try:
        harness.runner.invoke('go')
        harness.gate.set()
        wait_until(lambda: harness.finished())
        assert not (BACKGROUND | {'task', 'read_subagent_log'}) & graph_tool_names(children[0].graph)
    finally:
        harness.close()


def test_unverified_backend_refuses_before_starting(tmp_path):
    cfg = Settings(config_dir=tmp_path)
    model = scripted_model([AIMessage(content='', tool_calls=[start_call()]), AIMessage(content='refused')])
    runner = AgentRunner(prepared=create_agent(model=model, backend=StateBackend(), skills=[], settings=cfg))
    try:
        assert runner.invoke('go').output == 'refused'
        messages = runner.prepared.graph.get_state(runner._thread_config()).values['messages']
        content = next(m.content for m in messages if isinstance(m, ToolMessage))
        assert content.startswith('Subagent failed:') and 'verified SANDBOXED' in content
        assert runner.background_task_count() == 0
    finally:
        runner.close()


# Presentation ---------------------------------------------------------------

def launch(state, identifier='bg', description='Index the docs'):
    state.apply(RunEvent(type='tool_started', name='start_background_task', tool_call_id=identifier,
                         arguments={'description': description}))


def finish(state, identifier='bg', content='[subagent run_id=abc]\nIndexed 12 files'):
    state.apply(RunEvent(type='subagent_finished', content=content,
                         result={'tool_call_id': identifier, 'status': 'completed'}))


@pytest.mark.parametrize('finished_first', [False, True])
def test_card_stays_running_after_launch_and_run_end_until_finished(finished_first):
    state = CliState()
    state.apply(RunEvent(type='run_started'))
    launch(state)
    launched = RunEvent(type='tool_completed', name='start_background_task', tool_call_id='bg',
                        content='Background subagent started (run_id=abc).')
    if finished_first:
        # A fast child may report before the launch result reaches the view.
        finish(state)
        state.apply(launched)
        assert state.subagent_count == 0
    else:
        state.apply(launched)
        card = state.blocks[0]
        assert card.status == 'running' and card.output == '' and state.subagent_count == 1
        state.apply(RunEvent(type='interaction_requested'))
        state.apply(RunEvent(type='run_completed'))
        assert card.status == 'running' and state.subagent_count == 1
        text = plain(render_transcript(state, 100))
        assert 'Running SubAgent · Index the docs' in text and 'general-purpose · background' in text
        finish(state)
    card = state.blocks[0]
    assert isinstance(card, ToolBlock) and card.status == 'completed' and not card.is_error
    assert card.output.endswith('Indexed 12 files') and state.subagent_count == 0
    text = plain(render_transcript(state, 100))
    assert 'Running SubAgent' not in text and '└ result' in text and 'Indexed 12 files' in text


def test_refused_launch_cancelled_task_and_parent_cancel_settle_cards():
    state = CliState()
    launch(state, 'refused')
    state.apply(RunEvent(type='tool_completed', name='start_background_task', tool_call_id='refused',
                         content='Subagent failed: Subagent concurrency limit (2) reached'))
    launch(state, 'stopped')
    finish(state, 'stopped', 'Subagent cancelled (run_id=abc): Cancelled by parent')
    launch(state, 'failed')
    finish(state, 'failed', 'Subagent failed (run_id=abc): model unavailable')
    launch(state, 'esc')
    finish(state, 'unknown')
    state.apply(RunEvent(type='run_cancelled'))
    assert [(b.tool_call_id, b.status, b.is_error) for b in state.blocks] == [
        ('refused', 'error', True), ('stopped', 'cancelled', True),
        ('failed', 'error', True), ('esc', 'cancelled', True)]
    assert state.subagent_count == 0


def test_restore_pairs_notification_with_card_and_interrupts_unreported_tasks():
    calls = AIMessage(content='', tool_calls=[start_call('done', 'Finished one'), start_call('lost', 'Lost one')])
    messages = [
        HumanMessage(content='two background jobs'), calls,
        ToolMessage(content='Background subagent started (run_id=a).', tool_call_id='done',
                    name='start_background_task'),
        ToolMessage(content='Background subagent started (run_id=b).', tool_call_id='lost',
                    name='start_background_task'),
        AIMessage(content='started both'),
        notification_message({'run_id': 'a', 'tool_call_id': 'done', 'status': 'failed',
                              'content': 'Subagent failed (run_id=a): boom\n\nsecond paragraph'}),
        AIMessage(content='one failed'),
    ]
    blocks = messages_to_transcript(messages)
    settle_restored_tools(blocks)
    assert [b.content for b in blocks if b.kind == 'user'] == ['two background jobs']
    done, lost = [b for b in blocks if b.kind == 'tool']
    assert done.status == 'error' and done.is_error
    assert done.content == 'Subagent failed (run_id=a): boom\n\nsecond paragraph'
    assert lost.status == 'interrupted' and lost.content == '' and lost.arguments['description'] == 'Lost one'
    state = CliState()
    state.load_transcript(blocks)
    assert state.subagent_count == 0
    text = plain(render_transcript(state, 100))
    assert 'Automatic notification' not in text and 'Lost one' in text


def test_tui_wakes_idle_parent_and_escape_cancels_idle_background(tmp_path, monkeypatch):
    from agent.cli.app import CliApplication

    async def scenario() -> None:
        harness = Harness(tmp_path, monkeypatch, [
            AIMessage(content='', tool_calls=[start_call('first', 'first job')]),
            AIMessage(content='working meanwhile'),
            AIMessage(content='', tool_calls=[start_call('second', 'second job')]),
            AIMessage(content='reported first result'),
        ], answers=('first done', 'second done'))
        second_gate = threading.Event()

        async def until(predicate) -> None:
            async def poll() -> None:
                while not predicate():
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(poll(), timeout=8)

        def card(identifier):
            return next(b for b in app.state.blocks
                        if isinstance(b, ToolBlock) and b.tool_call_id == identifier)

        def texts(kind):
            return [b.content for b in app.state.blocks if isinstance(b, MessageBlock) and b.kind == kind]

        with create_pipe_input() as pipe:
            # The CLI owns the runner's idle event stream, as in agent.cli.main.
            harness.runner.on_event = None
            app = CliApplication(harness.runner, input=pipe, output=DummyOutput())
            running = asyncio.create_task(app.run_async())
            try:
                await until(lambda: app.application.is_running)
                pipe.send_text('go\r')
                await until(lambda: 'working meanwhile' in texts('assistant') and not app.state.running)
                assert card('first').status == 'running' and app.state.subagent_count == 1
                assert app._status_visible()
                status = ''.join(text for _, text in app._status_text())
                assert '1 subagent' in status and 'Background' in status

                # The next child must stay busy so Escape has something to stop.
                import agent.bootstrap.factory as factory
                build = factory.build_chat_model
                def gated(profile, **kwargs):
                    model = build(profile, **kwargs)
                    model.gate = second_gate
                    return model
                monkeypatch.setattr(factory, 'build_chat_model', gated)

                harness.gate.set()
                await until(lambda: 'reported first result' in texts('assistant') and not app.state.running)
                assert card('first').status == 'completed' and 'first done' in card('first').output
                assert texts('user') == ['go']
                assert card('second').status == 'running' and app.state.subagent_count == 1
                assert harness.runner.control.pending_notification_count() == 0

                pipe.send_text('\x1b')
                await until(lambda: harness.manager.background_tasks()[-1].controller.cancel_requested)
                second_gate.set()
                await until(lambda: card('second').status == 'cancelled')
                assert app.state.subagent_count == 0 and not app.state.running
                assert 'reported first result' == texts('assistant')[-1]
            finally:
                second_gate.set()
                app.exit()
                await asyncio.wait_for(running, timeout=5)
                harness.close()

    asyncio.run(scenario())
