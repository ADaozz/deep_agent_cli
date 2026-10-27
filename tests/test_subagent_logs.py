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
