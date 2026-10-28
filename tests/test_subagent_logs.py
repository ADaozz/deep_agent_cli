import json
from uuid import uuid4

import pytest

from agent.subagents.log_store import MAX_EVENT_BYTES, MAX_PAGE_BYTES, SubAgentLogStore


def test_log_restart_paging_ownership_and_redaction(tmp_path):
    store = SubAgentLogStore(tmp_path, ('configured-secret-value',))
    run = uuid4().hex
    for index in range(5):
        store.append('parent-a', run, 'tool', output=f'configured-secret-value token=supersecret {index}')
    restarted = SubAgentLogStore(tmp_path)
    assert restarted.read('parent-a')['runs'] == [run]
    page = restarted.read('parent-a', run, 1, 2)
    assert len(page['records']) == 2
    assert page['next_offset'] == 3
    raw = json.dumps(page)
    assert 'configured-secret-value' not in raw
    assert 'token=supersecret' in raw
    assert restarted.read('parent-b')['runs'] == []
    assert 'error' in restarted.read('parent-b', run)
    with pytest.raises(ValueError):
        restarted.read('parent-a', '../../etc/passwd')
    for offset, limit in [(-1, 1), (0, 0), (0, 101), (True, 1)]:
        with pytest.raises(ValueError):
            restarted.read('parent-a', run, offset, limit)


def test_log_bounds_and_crash_tail(tmp_path):
    store = SubAgentLogStore(tmp_path)
    run = uuid4().hex
    for _ in range(10):
        store.append('parent', run, 'model', output='文' * MAX_EVENT_BYTES)
    path = next(tmp_path.rglob('*.jsonl'))
    assert max(len(line.encode()) for line in path.read_text().splitlines()) < MAX_EVENT_BYTES
    page = store.read('parent', run)
    assert page['records'][0]['truncated']
    assert len(json.dumps(page, ensure_ascii=False).encode()) < MAX_PAGE_BYTES + 1000
    with path.open('a') as output:
        output.write('{"partial":')
    assert len(store.read('parent', run, offset=9)['records']) == 1



def test_only_known_strings_are_redacted_in_nested_and_serialized_values(tmp_path):
    from langchain_core.messages import AIMessage
    known = 'model-key"with\\quotes'
    tavily = 'known-tavily-key'
    store = SubAgentLogStore(tmp_path, (known, tavily, 'xy', ''))
    run = uuid4().hex
    payload = {
        'password': 'unknown123', 'token': 'unknown123', 'api_key': 'unknown123',
        'nested': [{'value': known}, (tavily, 'ordinary log')],
        'serialized': json.dumps({'password': 'correct horse battery staple', 'token': tavily}),
        'message': AIMessage(content=known),
        'short_known_secret': 'xy',
    }
    store.append('parent', run, 'tool', input=payload,
                 output='password=unknown123 token=unknown123 bearer unknown123')
    raw = next(tmp_path.rglob('*.jsonl')).read_text()
    record = json.loads(raw)
    assert record['input']['password'] == 'unknown123'
    assert record['input']['token'] == 'unknown123'
    assert record['input']['api_key'] == 'unknown123'
    assert record['input']['nested'] == [{'value': '[REDACTED]'}, ['[REDACTED]', 'ordinary log']]
    assert json.loads(record['input']['serialized']) == {
        'password': 'correct horse battery staple', 'token': '[REDACTED]'}
    assert record['input']['message']['content'] == '[REDACTED]'
    assert record['input']['short_known_secret'] == '[REDACTED]'
    assert record['output'] == 'password=unknown123 token=unknown123 bearer unknown123'
    assert known not in raw and tavily not in raw
    assert store.read('parent', run)['records'] == [record]


def test_known_strings_stay_redacted_when_event_is_truncated(tmp_path):
    store = SubAgentLogStore(tmp_path, ('known-model-key', 'known-tavily-key'))
    run = uuid4().hex
    store.append('parent', run, 'known-model-key', model='known-model-key', status='known-tavily-key',
                 output='known-model-key ' * MAX_EVENT_BYTES)
    raw = next(tmp_path.rglob('*.jsonl')).read_text()
    assert 'known-model-key' not in raw and 'known-tavily-key' not in raw
    record = json.loads(raw)
    assert record['truncated'] and record['model'] == record['status'] == record['event'] == '[REDACTED]'



def test_runtime_logs_operations_without_model_trace_or_raw_tool_output(tmp_path):
    from types import SimpleNamespace
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.tools import StructuredTool
    from agent.cancel import emit_tool_output, get_output_emitter, set_output_emitter
    from agent.control import RunController
    from agent.subagents.manager import SubAgentManager
    from agent.subagents.runtime import SubAgentRuntime
    from tests.conftest import scripted_model

    raw_file, raw_patch, raw_stdout = 'PRIVATE_FILE_CONTENT', 'PRIVATE_PATCH_CONTENT', 'PRIVATE_STDOUT_CONTENT'
    thought = 'PRIVATE_REASONING_CONTENT'
    answer = 'Fixed stats_rules.py. ' + 'summary ' * 1000 + 'FINAL_ANSWER_TAIL'
    def read_file(file_path: str):
        return raw_file * 1000
    def edit_file(file_path: str, old_string: str, new_string: str):
        return raw_patch * 1000
    def execute(command: str):
        emit_tool_output('execute-call', raw_stdout * 1000)
        return raw_stdout * 1000, {'exit_code': 0, 'agent_log_path': '/workspace/.deep-agent/logs/exec/execution.log'}
    tools = [StructuredTool.from_function(read_file, description='Read a file'),
             StructuredTool.from_function(edit_file, description='Edit a file'),
             StructuredTool.from_function(execute, description='Execute a command', response_format='content_and_artifact')]
    class Graph:
        def stream(self, input, config, **kwargs):
            calls = [
                {'file_path': 'stats_rules.py'},
                {'file_path': 'stats_rules.py', 'old_string': raw_file, 'new_string': raw_patch},
                {'command': 'python3 -m unittest tests.test_stats_rules -v'},
            ]
            for index, (tool, args) in enumerate(zip(tools, calls)):
                tool.invoke({'type': 'tool_call', 'id': f'tool-{index}', 'name': tool.name, 'args': args}, config=config)
            # Thousands of streamed tokens and reasoning must add zero log rows.
            model = scripted_model([AIMessage(content=answer, additional_kwargs={'reasoning_content': thought})])
            for _ in model.stream(input['messages'], config=config):
                pass
            yield {'messages': [AIMessage(content=answer, additional_kwargs={'reasoning_content': thought})]}
    parent = RunController()
    parent.begin_run()
    store = SubAgentLogStore(tmp_path)
    runtime = SubAgentRuntime(SubAgentManager(parent), store, lambda controller: SimpleNamespace(graph=Graph()), 'test-model')
    parent_output = []
    previous = get_output_emitter()
    set_output_emitter(lambda *args: parent_output.append(args))
    try:
        result = runtime.invoke({'messages': [HumanMessage(content='Fix stats_rules.py')]},
                                {'configurable': {'thread_id': 'parent'}})
    finally:
        set_output_emitter(previous)
    assert result['messages'][0].content.endswith(answer)
    records = store.read('parent', store.read('parent')['runs'][0])['records']
    assert [r['event'] for r in records] == ['start', 'tool_start', 'tool_finish', 'tool_start',
                                           'tool_finish', 'tool_start', 'tool_finish', 'finish']
    assert records[0]['description'] == 'Fix stats_rules.py'
    assert records[1]['target'] == records[3]['target'] == 'stats_rules.py'
    assert records[5]['command'] == 'python3 -m unittest tests.test_stats_rules -v'
    assert records[6]['exit_code'] == 0 and records[6]['status'] == 'completed'
    assert records[6]['log_path'].endswith('execution.log')
    assert records[-1]['tool_count'] == 3 and records[-1]['status'] == 'completed'
    assert records[-1]['summary_truncated'] and len(records[-1]['summary']) <= 1000
    assert records[-1]['elapsed_seconds'] >= 0
    for start, finish in [(1, 2), (3, 4), (5, 6)]:
        assert records[start]['tool_run_id'] == records[finish]['tool_run_id']
        assert records[finish]['elapsed_seconds'] >= 0
    raw = next(tmp_path.rglob('*.jsonl')).read_text()
    assert all(value not in raw for value in (raw_file, raw_patch, raw_stdout, thought, 'FINAL_ANSWER_TAIL'))
    assert not parent_output


def test_callback_bounds_arguments_errors_and_keeps_parallel_tool_identity():
    from uuid import uuid4
    from langchain_core.messages import ToolMessage
    from agent.subagents.runtime import LogCallback
    records = []
    callback = LogCallback(lambda event, **payload: records.append({'event': event, **payload}))
    file_run, command_run = uuid4(), uuid4()
    callback.on_tool_start({'name': 'write_file'}, json.dumps({'file_path': 'file.py', 'content': 'PRIVATE_FILE'}), run_id=file_run)
    callback.on_tool_start({'name': 'execute'}, '', inputs={'command': 'x' * 10000}, run_id=command_run)
    callback.on_tool_end(ToolMessage(content='PRIVATE_STDOUT', tool_call_id='command',
                                   artifact={'exit_code': 7}), run_id=command_run)
    callback.on_tool_error(ValueError('write failed ' * 1000), run_id=file_run)
    callback.on_llm_error(RuntimeError('model service unavailable ' * 1000))
    assert records[0]['target'] == 'file.py' and 'content' not in records[0]
    assert len(records[1]['command']) <= 300
    assert records[2]['tool_name'] == 'execute' and records[2]['exit_code'] == 7
    assert records[2]['status'] == 'failed' and 'PRIVATE_STDOUT' not in json.dumps(records[2])
    assert records[3]['tool_name'] == 'write_file' and records[3]['event'] == 'tool_error'
    assert len(records[3]['reason']) <= 240 and len(records[4]['reason']) <= 240
    assert records[4]['event'] == 'model_error' and callback.tool_count == 2
    assert 'PRIVATE_FILE' not in json.dumps(records)


@pytest.mark.parametrize('cancelled', [False, True])
def test_failed_or_cancelled_task_keeps_last_tool_start_and_terminal_summary(tmp_path, cancelled):
    from types import SimpleNamespace
    from langchain_core.messages import HumanMessage
    from agent.control import RunController
    from agent.subagents.manager import SubAgentManager
    from agent.subagents.runtime import SubAgentRuntime
    class Graph:
        def __init__(self, controller):
            self.controller = controller
        def stream(self, input, config, **kwargs):
            config['callbacks'][0].on_tool_start({'name': 'read_file'}, '', inputs={'file_path': 'stats_rules.py'}, run_id='tool')
            if cancelled:
                self.controller.cancel()
            raise RuntimeError('operation failed ' * 1000)
            yield {}
    parent = RunController()
    parent.begin_run()
    store = SubAgentLogStore(tmp_path)
    manager = SubAgentManager(parent)
    runtime = SubAgentRuntime(manager, store, lambda controller: SimpleNamespace(graph=Graph(controller)), 'test-model')
    runtime.invoke({'messages': [HumanMessage(content='task')]}, {'configurable': {'thread_id': 'parent'}})
    records = store.read('parent', store.read('parent')['runs'][0])['records']
    assert [r['event'] for r in records] == ['start', 'tool_start', 'error', 'finish']
    assert records[1]['tool_name'] == 'read_file'
    assert records[-1]['status'] == ('cancelled' if cancelled else 'failed')
    assert records[-1]['tool_count'] == 1 and len(records[-2]['reason']) <= 240
    assert manager.active_count == 0



def test_known_keys_are_replaced_before_summary_limits(tmp_path):
    from types import SimpleNamespace
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from agent.control import RunController
    from agent.subagents.manager import SubAgentManager
    from agent.subagents.runtime import SubAgentRuntime

    key = 'KNOWN_SECRET_BOUNDARY_VALUE_' * 10
    description = 'd' * 990 + key
    answer = 'a' * 990 + key
    class Graph:
        def stream(self, input, config, **kwargs):
            callback = config['callbacks'][0]
            callback.on_tool_start({'name': 'execute'}, '', inputs={'command': 'c' * 290 + key}, run_id='tool')
            callback.on_tool_end(ToolMessage(content='', tool_call_id='tool', artifact={'exit_code': 0}), run_id='tool')
            callback.on_llm_error(RuntimeError('e' * 230 + key))
            yield {'messages': [AIMessage(content=answer)]}
    parent = RunController()
    parent.begin_run()
    store = SubAgentLogStore(tmp_path, (key,))
    runtime = SubAgentRuntime(SubAgentManager(parent), store, lambda controller: SimpleNamespace(graph=Graph()), 'test-model')
    result = runtime.invoke({'messages': [HumanMessage(content=description)]},
                            {'configurable': {'thread_id': 'parent'}})
    assert result['messages'][0].content.endswith(answer)
    raw = next(tmp_path.rglob('*.jsonl')).read_text()
    assert 'KNOWN' not in raw
    assert '[REDACTED]' in raw
