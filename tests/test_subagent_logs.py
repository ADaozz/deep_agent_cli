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
    assert 'supersecret' not in raw
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



def test_redaction_preserves_json_escaping_and_structured_credentials(tmp_path):
    store = SubAgentLogStore(tmp_path, ('secret"with\\quotes',))
    run = uuid4().hex
    store.append('parent', run, 'tool', input={'password': 'neverpersist', 'nested': {'api-key': 'anothersecret'}},
                 output='token=value"quoted password=abc\\def')
    raw = next(tmp_path.rglob('*.jsonl')).read_text()
    assert 'neverpersist' not in raw and 'anothersecret' not in raw
    assert 'value' not in raw and 'abc' not in raw
    assert json.loads(raw)['input']['password'] == '[REDACTED]'
    assert len(store.read('parent', run)['records']) == 1
