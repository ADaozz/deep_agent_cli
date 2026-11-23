"""Public task cards show delegation and final results, never child internals."""
from contextlib import closing

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from deepagents.backends import StateBackend
import pytest

from agent.cli.previews import build_task_preview
from agent.cli.rendering import _tool_status_marker, render_transcript
from agent.cli.state import CliState, ToolBlock
from agent.cli.display import UiDisplayLimits
from agent.runtime.runner import AgentRunner
from agent.runtime.contracts import RunEvent
from agent.sessions.store import SessionStore
from agent.sessions.transcript import messages_to_transcript
from tests.cli_support import plain, with_painted_app
from tests.conftest import scripted_model


def start(state, identifier, description):
    state.apply(RunEvent(type='tool_started', name='start_task', tool_call_id=identifier,
                         arguments={'description': description, 'subagent_type': 'general-purpose'}))


@pytest.mark.parametrize('scheduler_first', [False, True])
def test_counter_tracks_cards_when_scheduler_and_tool_events_arrive_separately(scheduler_first):
    state = CliState()
    state.apply(RunEvent(type='run_started'))

    def assert_count(expected):
        assert state.subagent_count == expected
        assert plain(render_transcript(state, 100)).count('Running SubAgent') == expected

    start(state, 'a', 'First task')
    assert_count(1)
    state.apply(RunEvent(type='subagents_changed', result={'count': 1}))
    start(state, 'b', 'Second task')
    assert_count(2)
    state.apply(RunEvent(type='subagents_changed', result={'count': 2}))
    for identifier, remaining in [('a', 1), ('b', 0)]:
        events = [
            RunEvent(type='subagents_changed', result={'count': remaining}),
            RunEvent(type='tool_completed', name='start_task', tool_call_id=identifier, content='Result'),
        ]
        if not scheduler_first:
            events.reverse()
        expected = remaining + 1
        for event in events:
            state.apply(event)
            if event.type == 'tool_completed':
                expected = remaining
            assert_count(expected)
    # Late/replayed runtime events cannot revive a completed card's count.
    state.apply(RunEvent(type='subagents_changed', result={'count': 2}))
    assert_count(0)


@pytest.mark.parametrize('terminal_event', ['run_completed', 'run_failed', 'run_cancelled', 'interaction_requested'])
def test_counter_settles_with_cards_on_parent_terminal_event(terminal_event):
    state = CliState()
    state.apply(RunEvent(type='run_started'))
    start(state, 'a', 'First task')
    start(state, 'b', 'Second task')
    state.apply(RunEvent(type=terminal_event))
    assert state.subagent_count == 0
    assert 'Running SubAgent' not in plain(render_transcript(state, 100))


def test_counter_ignores_other_tools_duplicate_starts_and_failed_delegations():
    state = CliState()
    start(state, 'a', 'Task')
    start(state, 'a', 'Task')
    state.apply(RunEvent(type='tool_started', name='read', tool_call_id='file'))
    assert state.subagent_count == 1
    state.apply(RunEvent(type='tool_completed', tool_call_id='a',
                         content='Subagent failed: Subagent concurrency limit reached'))
    assert state.subagent_count == 0
    start(state, 'b', 'Another task')
    assert state.subagent_count == 1
    state.clear()
    assert state.subagent_count == 0


def test_parallel_tasks_update_separately_and_keep_running_spinner(monkeypatch):
    state = CliState()
    state.apply(RunEvent(type='run_started'))
    start(state, 'a', 'Fix median3')
    start(state, 'b', 'Fix is_leap_year')
    state.apply(RunEvent(type='subagents_changed', result={'count': 2}))
    text = plain(render_transcript(state, 100))
    assert text.count('Running SubAgent') == 2
    assert 'Fix median3' in text and 'Fix is_leap_year' in text
    assert 'Explored' not in text
    first, second = state.blocks
    state.apply(RunEvent(type='tool_completed', name='start_task', tool_call_id='a', content='All median tests passed'))
    state.apply(RunEvent(type='subagents_changed', result={'count': 1}))
    assert state.blocks[0] is first and state.blocks[1] is second
    assert first.status == 'completed' and second.status == 'running'
    assert first.output == 'All median tests passed' and second.output == ''
    text = plain(render_transcript(state, 100))
    assert text.count('Running SubAgent') == 1 and 'All median tests passed' in text
    assert '└ result' not in text
    assert state.subagent_count == 1
    monkeypatch.setattr('agent.cli.rendering._spinner_tick', lambda: 0)
    assert _tool_status_marker('running') == ('⠋', 'green')
    monkeypatch.setattr('agent.cli.rendering._spinner_tick', lambda: 1)
    assert _tool_status_marker('running') == ('⠙', 'green')
    assert _tool_status_marker('completed') == ('•', 'green')


@pytest.mark.parametrize('result,status', [
    ('[subagent run_id=abc]\nSubagent failed: quoted source text', 'completed'),
    ('[subagent run_id=abc]\nThe cancel path returns cancelled by user.\nReview complete.', 'completed'),
    ('Subagent failed (run_id=abc): model unavailable', 'error'),
    ('Subagent failed: Subagent concurrency limit reached', 'error'),
    ('Subagent cancelled (run_id=abc): Cancelled by parent', 'cancelled'),
])
def test_runner_and_unnamed_history_results_share_status(result, status):
    calls = [AIMessage(content='', tool_calls=[{'id': 'd', 'name': 'start_task', 'args': {
        'description': 'Fix median3', 'subagent_type': 'general-purpose'}}])]
    message = ToolMessage(content=result, tool_call_id='d')
    runner = AgentRunner(model=scripted_model([]), backend=StateBackend())
    state = CliState()
    events = []
    def emit(event):
        events.append(event)
        state.apply(event)
    try:
        runner._emit_update_events({'model': {'messages': calls}}, emit)
        runner._emit_update_events({'tools': {'messages': [message]}}, emit)
        card = state.blocks[0]
        assert isinstance(card, ToolBlock) and card.status == status
        assert events[-1].name == 'start_task'
        assert events[-1].is_error == (status != 'completed')
        restored = CliState()
        restored.load_transcript(messages_to_transcript([*calls, message]))
        other = restored.blocks[0]
        assert (other.name, other.arguments, other.output, other.status, other.is_error) == (
            card.name, card.arguments, card.output, card.status, card.is_error)
        rendered = plain(render_transcript(state, 100))
        assert plain(render_transcript(restored, 100)) == rendered
        if status == 'completed':
            assert '— failed' not in rendered and '— cancelled' not in rendered
    finally:
        runner.close()


def test_long_result_folds_tail_and_control_o_expands_full_text():
    output = '\n'.join(f'line {n}' for n in range(40))
    def scenario(app):
        app.state.clear()
        start(app.state, 'd', 'Fix median3')
        app.state.apply(RunEvent(type='tool_completed', name='start_task', tool_call_id='d', content=output))
        limits = UiDisplayLimits(tail_lines=3)
        collapsed = plain(render_transcript(app.state, 100, limits=limits))
        assert 'Ctrl+A to expand' in collapsed and 'line 0' not in collapsed
        assert 'line 39' in collapsed and app.state.blocks[0].output == output
        assert '└ result' not in collapsed
        binding = next(b for b in app.bindings.bindings if b.keys == ('c-a',))
        binding.handler(None)
        expanded = plain(render_transcript(app.state, 100, limits=limits))
        assert app.state.tasks_expanded
        assert 'line 0' in expanded and 'line 39' in expanded
        assert 'output lines hidden' not in expanded
    with_painted_app('task-result-expand', scenario, lines=0)


@pytest.mark.parametrize('event,status', [('run_cancelled', 'cancelled'), ('run_failed', 'error'), ('run_completed', 'interrupted')])
def test_unfinished_tasks_keep_description_without_fabricating_results(event, status):
    state = CliState()
    start(state, 'd', 'Fix median3')
    state.apply(RunEvent(type=event, content=''))
    card = state.blocks[0]
    assert card.status == status and card.output == ''
    text = plain(render_transcript(state, 100))
    assert 'Fix median3' in text and 'Running SubAgent' not in text
    assert '└ result' not in text
    if status == 'cancelled':
        assert 'cancelled' in text


def test_title_is_bounded_and_does_not_modify_delegated_description():
    description = 'Fix\n  median3\t' + 'details ' * 100
    block = ToolBlock('d', 'start_task', {'description': description})
    preview = build_task_preview(block)
    assert preview.target.startswith('Fix median3 ') and len(preview.target) == 120
    assert '\n' not in preview.target and preview.target.endswith('…')
    assert block.arguments['description'] == description


def test_resume_persists_independent_tasks_and_unfinished_description(tmp_path):
    path = tmp_path / 'sessions.sqlite3'
    calls = AIMessage(content='', tool_calls=[{'id': name, 'name': 'start_task', 'args': {
        'description': f'Fix {name}', 'subagent_type': 'general-purpose'}} for name in ['a', 'b']])
    with closing(SessionStore(path)) as store:
        runner = AgentRunner(model=scripted_model([]), backend=StateBackend(), session_store=store)
        identifier = runner.thread_id
        runner.prepared.graph.update_state(runner._thread_config(), {'messages': [
            HumanMessage(content='Two independent fixes'), calls,
            ToolMessage(content='Subagent failed: unavailable', tool_call_id='a'),
        ]}, as_node='tools')
        runner.close()
    with closing(SessionStore(path)) as store:
        runner = AgentRunner(model=scripted_model([]), backend=StateBackend(), session_store=store)
        try:
            snapshot = runner.switch_session(identifier)
            cards = [b for b in snapshot.transcript if b.kind == 'tool']
            assert len(cards) == 2
            assert cards[0].status == 'error' and cards[0].content == 'Subagent failed: unavailable'
            assert cards[1].status == 'interrupted' and cards[1].content == ''
            assert cards[1].arguments['description'] == 'Fix b'
        finally:
            runner.close()


def test_child_internal_tools_and_thinking_do_not_create_main_cards(tmp_path, monkeypatch):
    import agent.bootstrap.factory as factory
    from agent.bootstrap.config import Settings
    from agent.execution.backend import BackendSelection
    from agent.execution.types import ExecutionMode
    backend = StateBackend()
    monkeypatch.setattr(factory, 'select_backend', lambda cfg: BackendSelection(backend, ExecutionMode.SANDBOXED))
    model = scripted_model([
        AIMessage(content='', tool_calls=[{'name': 'start_task', 'id': 'delegate', 'args': {
            'description': 'Inspect isolated file', 'subagent_type': 'general-purpose'}}]),
        AIMessage(content='', additional_kwargs={'reasoning_content': 'private child thinking'},
                  tool_calls=[{'name': 'read', 'id': 'private-read', 'args': {'file_path': '/private-child.txt'}}]),
        AIMessage(content='Final child result'), AIMessage(content='Parent final answer'),
    ])
    events = []
    state = CliState()
    def emit(event):
        events.append(event)
        state.apply(event)
    runner = AgentRunner(model=model, settings=Settings(config_dir=tmp_path), on_event=emit)
    runner.set_permission_mode('allow')
    try:
        result = runner.invoke('Inspect independent tasks')
        assert result.status == 'completed'
        cards = [b for b in state.blocks if isinstance(b, ToolBlock)]
        assert len(cards) == 1 and cards[0].name == 'start_task'
        assert 'Final child result' in cards[0].output
        assert not any(e.tool_call_id == 'private-read' for e in events)
        text = plain(render_transcript(state, 100))
        assert 'private child thinking' not in text and '/private-child.txt' not in text
        saved = runner.prepared.graph.get_state(runner._thread_config()).values['messages']
        restored = [b for b in messages_to_transcript(saved) if b.kind == 'tool']
        assert len(restored) == 1 and restored[0].name == 'start_task'
    finally:
        runner.close()


def _task_update(state, **fields):
    payload = {
        'run_id': 'a', 'parent_run_id': 'root', 'depth': 1, 'description': '执行与取消链路',
        'status': 'running', 'tool_call_id': 'card-a', 'elapsed_seconds': 1.2,
        'input_tokens': 20, 'output_tokens': 8, 'tps': 12.5,
    }
    payload.update(fields)
    state.apply(RunEvent(type='subagent_update', result=payload))


def test_nested_tasks_update_in_place_and_do_not_finish_the_parent():
    state = CliState()
    state.apply(RunEvent(type='run_started'))
    start(state, 'card-a', '执行与取消链路')
    start(state, 'card-b', '审批与权限链路')
    _task_update(state)
    _task_update(
        state, run_id='a1', parent_run_id='a', depth=2, description='RunController 取消传播',
        tool_call_id='inner-1', elapsed_seconds=0.2, input_tokens=3, output_tokens=1, tps=8,
    )
    _task_update(
        state, run_id='a2', parent_run_id='a', depth=2, description='异步调用与资源回收',
        tool_call_id='inner-2', elapsed_seconds=0.1, input_tokens=0, output_tokens=0, tps=None,
    )
    # Two root cards plus two running children. A closing HTTP stream is not a third source.
    state.apply(RunEvent(type='subagents_changed', result={'count': 9}))
    assert state.subagent_count == 4
    text = plain(render_transcript(state, 120))
    assert '▾' not in text and '▸' not in text
    assert 'Running SubAgent · RunController 取消传播' in text
    assert 'Running SubAgent · 异步调用与资源回收' in text
    assert 'general-purpose' not in text and 'tok' not in text and 'tps' not in text
    child = next(line for line in text.splitlines() if 'RunController 取消传播' in line).rstrip()
    parent = next(line for line in text.splitlines() if '执行与取消链路' in line).rstrip()
    # Nested rows reuse the root title; only the tree prefix is added.
    assert child[child.index('Running SubAgent'):] == 'Running SubAgent · RunController 取消传播'
    assert parent[parent.index('Running SubAgent'):] == 'Running SubAgent · 执行与取消链路'
    assert '├─' in child and child.index('Running SubAgent') > child.index('├─')
    assert '审批与权限链路' in text and 'RunController 取消传播' in text.split('审批与权限链路')[0]

    _task_update(
        state, run_id='a1', parent_run_id='a', depth=2, description='RunController 取消传播',
        status='completed', elapsed_seconds=0.4, input_tokens=3, output_tokens=1, tps=8,
        result='取消传播已核对',
    )
    assert state.blocks[0].status == 'running' and state.blocks[1].status == 'running'
    assert state.subagent_count == 3
    text = plain(render_transcript(state, 120))
    assert 'SubAgent · RunController 取消传播' in text and 'Running SubAgent · 异步调用与资源回收' in text
    assert '▾' not in text
    answer = next(line for line in text.splitlines() if '取消传播已核对' in line)
    assert '│' in answer and '├' not in answer
    assert answer.index('取消传播已核对') == text.splitlines()[text.splitlines().index(answer) - 1].index('SubAgent')

    _task_update(
        state, run_id='a2', parent_run_id='a', depth=2, description='异步调用与资源回收',
        status='completed', elapsed_seconds=0.5, input_tokens=4, output_tokens=2, tps=11,
    )
    assert state.blocks[0].status == 'running'
    assert state.subagent_count == 2
    settled = plain(render_transcript(state, 120))
    assert 'SubAgent · RunController 取消传播' in settled and 'SubAgent · 异步调用与资源回收' in settled
    assert '▾' not in settled and '▸' not in settled

    state.apply(RunEvent(
        type='tool_completed', name='start_task', tool_call_id='card-a',
        content='[subagent run_id=a]\n汇总完成，其中引用了 cancelled by user。',
    ))
    parent, other = state.blocks[0], state.blocks[1]
    assert parent.status == 'completed' and not parent.is_error
    assert other.status == 'running'
    done = plain(render_transcript(state, 120))
    assert '汇总完成' in done and '— failed' not in done
    assert '└ result' not in done
    assert 'SubAgent · RunController 取消传播' in done


def test_task_card_leaves_one_blank_line_before_the_next_tool():
    state = CliState()
    start(state, 'a', '隔离')
    state.apply(RunEvent(
        type='tool_completed', name='start_task', tool_call_id='a',
        content='\n'.join(f'line {n}' for n in range(5)),
    ))
    start(state, 'b', '重绘')
    lines = [line.rstrip() for line in plain(
        render_transcript(state, 80, limits=UiDisplayLimits(tail_lines=3)),
    ).splitlines()]
    last = max(index for index, line in enumerate(lines) if 'line 4' in line)
    blanks = 0
    for line in lines[last + 1:]:
        if line:
            break
        blanks += 1
    assert blanks == 1
    assert '重绘' in lines[last + 1 + blanks]
    assert 'line 0' not in '\n'.join(lines) and 'line 2' in lines[last - 2]


def test_failed_child_shows_its_reason_on_its_own_row():
    state = CliState()
    start(state, 'card-b', '审批与权限链路')
    _task_update(state, run_id='b', tool_call_id='card-b', description='审批与权限链路', parent_run_id='root')
    _task_update(
        state, run_id='b1', parent_run_id='b', depth=2, description='Ask / Auto 审批',
        tool_call_id='b1-call', status='running', elapsed_seconds=0.2,
    )
    _task_update(
        state, run_id='b2', parent_run_id='b', depth=2, description='Flash 审批与恢复',
        tool_call_id='b2-call', status='failed', reason='model unavailable', elapsed_seconds=0.3,
    )
    assert state.blocks[0].status == 'running' and not state.blocks[0].is_error
    assert state.subagent_count == 2
    text = plain(render_transcript(state, 120))
    assert '▾' not in text and 'Running SubAgent · Ask / Auto 审批' in text
    assert 'SubAgent · Flash 审批与恢复' in text
    assert '— failed' in text and 'model unavailable' in text
    failed = next(line for line in text.splitlines() if 'Flash 审批与恢复' in line)
    reason = next(line for line in text.splitlines() if 'model unavailable' in line)
    assert '├' not in reason and reason.startswith(' ' * failed.index('SubAgent'))
    # A late progress tick must not reopen the failed row or add it back to the count.
    _task_update(
        state, run_id='b2', parent_run_id='b', depth=2, description='Flash 审批与恢复',
        status='running', tool_call_id='b2-call',
    )
    assert state.subagent_count == 2
    _task_update(
        state, run_id='b1', parent_run_id='b', depth=2, description='Ask / Auto 审批',
        status='completed', tool_call_id='b1-call', elapsed_seconds=0.4,
    )
    assert state.blocks[0].status == 'running'
    assert state.subagent_count == 1
    settled = plain(render_transcript(state, 120))
    assert '▾' not in settled and '▸' not in settled
    assert '— failed' in settled and 'model unavailable' in settled
