import json
from uuid import uuid4

import pytest
from langchain_core.tools import ToolException

from agent.redaction import SecretRef
from agent.subagents.log_store import MAX_EVENT_BYTES, MAX_PAGE_BYTES, SubAgentLogStore


def test_log_restart_paging_ownership_and_redaction(tmp_path):
    store = SubAgentLogStore(tmp_path, (SecretRef("CFG", "configured-secret-value"),))
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
    assert '$CFG' in raw
    assert 'token=supersecret' in raw
    assert restarted.read('parent-b')['runs'] == []
    assert 'error' in restarted.read('parent-b', run)
    with pytest.raises(ValueError):
        restarted.read('parent-a', '../../etc/passwd')
    for offset, limit in [(-1, 1), (0, 0), (0, 101), (True, 1)]:
        with pytest.raises(ValueError):
            restarted.read('parent-a', run, offset, limit)


def test_read_task_enforces_direct_parent_isolation(tmp_path):
    """Root cannot read A1; A can read A1; A cannot read B's child."""
    from types import SimpleNamespace

    from agent.tools.subagent_tasks import build_subagent_tools

    store = SubAgentLogStore(tmp_path)
    root, agent_a, agent_b = "root-session", "agent-a-run", "agent-b-run"
    a1, b1 = uuid4().hex, uuid4().hex
    store.append(agent_a, a1, "finish", status="completed", result="a1-only")
    store.append(agent_b, b1, "finish", status="completed", result="b1-only")
    tool = next(item for item in build_subagent_tools(
        None, None, store, names=frozenset({"read_task"}), allow_background=False,
    ) if item.name == "read_task")

    def read_as(parent: str, run_id: str | None = None, *, include_logs: bool = False) -> dict:
        runtime = SimpleNamespace(config={"configurable": {"thread_id": parent}}, tool_call_id="read")
        value = tool.func(runtime, run_id=run_id, include_logs=include_logs)
        if isinstance(value, tuple):
            value = value[0]
        return json.loads(value)

    # Direct parent A sees A1; listing is scoped to A.
    assert [item["run_id"] for item in read_as(agent_a)["tasks"]] == [a1]
    page = read_as(agent_a, a1, include_logs=True)
    assert page["logs"]["records"][0]["result"] == "a1-only"
    assert page["result"] == "a1-only"

    # Root cannot see or open A1 (grandchild / other parent's child).
    assert read_as(root)["tasks"] == []
    with pytest.raises(ToolException, match="not found"):
        read_as(root, a1)

    # A cannot open B's direct child even with a known run_id.
    assert [item["run_id"] for item in read_as(agent_a)["tasks"]] == [a1]
    with pytest.raises(ToolException, match="not found"):
        read_as(agent_a, b1)
    assert read_as(agent_b, b1)["result"] == "b1-only"


def test_metadata_reads_the_finish_past_the_first_page(tmp_path):
    store = SubAgentLogStore(tmp_path)
    run = uuid4().hex
    store.append(
        "parent", run, "start", status="running", description="work", mode="sync",
        parent_run_id="parent", tool_call_id="call", depth=1,
    )
    for _ in range(120):
        store.append("parent", run, "tool_finish", status="completed", note="x" * 800)
    store.append(
        "parent", run, "finish", status="failed", result="boom", elapsed_seconds=1.5,
        mode="sync", parent_run_id="parent", tool_call_id="call", depth=1, description="work",
    )
    page = store.read("parent", run, 0, 100)
    assert all(record.get("event") != "finish" for record in page["records"])
    meta = store.task_metadata("parent", run)
    assert meta["status"] == "failed" and meta["result"] == "boom"

    orphan = uuid4().hex
    store.append(
        "parent", orphan, "start", status="running", description="gone", mode="background",
        parent_run_id="parent", tool_call_id="lost", depth=1,
    )
    assert store.task_metadata("parent", orphan)["status"] == "unconfirmed"


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
    deepseek = 'known-deepseek-key'
    store = SubAgentLogStore(tmp_path, (
        SecretRef("MODEL_KEY", known),
        SecretRef("DEEPSEEK", deepseek),
        SecretRef("SHORT", "xy"),
    ))
    run = uuid4().hex
    payload = {
        'password': 'unknown123', 'token': 'unknown123', 'api_key': 'unknown123',
        'nested': [{'value': known}, (deepseek, 'ordinary log')],
        'serialized': json.dumps({'password': 'correct horse battery staple', 'token': deepseek}),
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
    assert record['input']['nested'] == [{'value': '$MODEL_KEY'}, ['$DEEPSEEK', 'ordinary log']]
    assert json.loads(record['input']['serialized']) == {
        'password': 'correct horse battery staple', 'token': '$DEEPSEEK'}
    assert record['input']['message']['content'] == '$MODEL_KEY'
    assert record['input']['short_known_secret'] == '$SHORT'
    assert record['output'] == 'password=unknown123 token=unknown123 bearer unknown123'
    assert known not in raw and deepseek not in raw
    assert store.read('parent', run)['records'] == [record]


def test_known_strings_stay_redacted_when_event_is_truncated(tmp_path):
    store = SubAgentLogStore(tmp_path, (
        SecretRef("MODEL_KEY", "known-model-key"),
        SecretRef("DEEPSEEK", "known-deepseek-key"),
    ))
    run = uuid4().hex
    store.append('parent', run, 'known-model-key', model='known-model-key', status='known-deepseek-key',
                 output='known-model-key ' * MAX_EVENT_BYTES)
    raw = next(tmp_path.rglob('*.jsonl')).read_text()
    assert 'known-model-key' not in raw and 'known-deepseek-key' not in raw
    record = json.loads(raw)
    assert record['truncated']
    assert record['event'] == record['model'] == '$MODEL_KEY'
    assert record['status'] == '$DEEPSEEK'



def test_runtime_logs_operations_without_model_trace_or_raw_tool_output(tmp_path):
    from types import SimpleNamespace
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.tools import StructuredTool
    from agent.runtime.cancellation import emit_tool_output, get_output_emitter, set_output_emitter
    from agent.runtime.control import RunController
    from agent.subagents.manager import SubAgentManager
    from agent.subagents.runtime import SubAgentRuntime
    from tests.conftest import scripted_model

    raw_file, raw_patch, raw_stdout = 'PRIVATE_FILE_CONTENT', 'PRIVATE_PATCH_CONTENT', 'PRIVATE_STDOUT_CONTENT'
    thought = 'PRIVATE_REASONING_CONTENT'
    answer = 'Fixed stats_rules.py. ' + 'summary ' * 1000 + 'FINAL_ANSWER_TAIL'
    def read(file_path: str):
        return raw_file * 1000
    def edit(file_path: str, old_string: str, new_string: str):
        return raw_patch * 1000
    def execute(command: str):
        emit_tool_output('execute-call', raw_stdout * 1000)
        return raw_stdout * 1000, {'exit_code': 0, 'agent_log_path': '/workspace/.deep-agent/logs/exec/execution.log'}
    tools = [StructuredTool.from_function(read, description='Read a file'),
             StructuredTool.from_function(edit, description='Edit a file'),
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
    from agent.subagents.configuration import DEFAULT_MAX_CONCURRENT, DEFAULT_MAX_TOTAL
    runtime = SubAgentRuntime(
        SubAgentManager(parent, max_concurrent=DEFAULT_MAX_CONCURRENT, max_total=DEFAULT_MAX_TOTAL),
        store, lambda controller: SimpleNamespace(graph=Graph()), 'test-model',
    )
    parent_output = []
    previous = get_output_emitter()
    set_output_emitter(lambda *args, **_kw: parent_output.append(args))
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
    assert records[-1]['summary_truncated'] and len(records[-1]['result_summary']) <= 1000
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
    callback.on_tool_start({'name': 'write'}, json.dumps({'file_path': 'file.py', 'content': 'PRIVATE_FILE'}), run_id=file_run)
    callback.on_tool_start({'name': 'execute'}, '', inputs={'command': 'x' * 10000}, run_id=command_run)
    callback.on_tool_end(ToolMessage(content='PRIVATE_STDOUT', tool_call_id='command',
                                   artifact={'exit_code': 7}), run_id=command_run)
    callback.on_tool_error(ValueError('write failed ' * 1000), run_id=file_run)
    callback.on_llm_error(RuntimeError('model service unavailable ' * 1000))
    assert records[0]['target'] == 'file.py' and 'content' not in records[0]
    assert len(records[1]['command']) <= 300
    assert records[2]['tool_name'] == 'execute' and records[2]['exit_code'] == 7
    assert records[2]['status'] == 'failed' and 'PRIVATE_STDOUT' not in json.dumps(records[2])
    assert records[3]['tool_name'] == 'write' and records[3]['event'] == 'tool_error'
    assert len(records[3]['reason']) <= 240 and len(records[4]['reason']) <= 240
    assert records[4]['event'] == 'model_error' and callback.tool_count == 2
    assert 'PRIVATE_FILE' not in json.dumps(records)


def test_callback_does_not_treat_error_topic_bodies_as_failures():
    from uuid import uuid4
    from langchain_core.messages import ToolMessage
    from agent.subagents.runtime import LogCallback
    records = []
    callback = LogCallback(lambda event, **payload: records.append({'event': event, **payload}))
    run_id = uuid4()
    callback.on_tool_start({'name': 'read_file'}, '', inputs={'file_path': 'guide.md'}, run_id=run_id)
    callback.on_tool_end(
        ToolMessage(
            content='Error handling guide\n\n1. Catch exceptions early',
            tool_call_id='read-1',
            name='read_file',
            status='success',
        ),
        run_id=run_id,
    )
    assert records[-1]['status'] == 'completed'
    assert records[-1]['summary'] == 'Completed'


@pytest.mark.parametrize('cancelled', [False, True])
def test_failed_or_cancelled_task_keeps_last_tool_start_and_terminal_summary(tmp_path, cancelled):
    from types import SimpleNamespace
    from langchain_core.messages import HumanMessage
    from agent.runtime.control import RunController
    from agent.subagents.configuration import DEFAULT_MAX_CONCURRENT, DEFAULT_MAX_TOTAL
    from agent.subagents.manager import SubAgentManager
    from agent.subagents.runtime import SubAgentRuntime
    class Graph:
        def __init__(self, controller):
            self.controller = controller
        def stream(self, input, config, **kwargs):
            config['callbacks'][0].on_tool_start({'name': 'read'}, '', inputs={'file_path': 'stats_rules.py'}, run_id='tool')
            if cancelled:
                self.controller.cancel()
            raise RuntimeError('operation failed ' * 1000)
            yield {}
    parent = RunController()
    parent.begin_run()
    store = SubAgentLogStore(tmp_path)
    manager = SubAgentManager(parent, max_concurrent=DEFAULT_MAX_CONCURRENT, max_total=DEFAULT_MAX_TOTAL)
    runtime = SubAgentRuntime(manager, store, lambda controller: SimpleNamespace(graph=Graph(controller)), 'test-model')
    runtime.invoke({'messages': [HumanMessage(content='start_task')]}, {'configurable': {'thread_id': 'parent'}})
    records = store.read('parent', store.read('parent')['runs'][0])['records']
    assert [r['event'] for r in records] == ['start', 'tool_start', 'error', 'finish']
    assert records[1]['tool_name'] == 'read'
    assert records[-1]['status'] == ('cancelled' if cancelled else 'failed')
    assert records[-1]['tool_count'] == 1 and len(records[-2]['reason']) <= 240
    assert manager.active_count == 0



def test_known_keys_are_replaced_before_summary_limits(tmp_path):
    from types import SimpleNamespace
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from agent.runtime.control import RunController
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
    store = SubAgentLogStore(tmp_path, (SecretRef("KNOWN", key),))
    from agent.subagents.configuration import DEFAULT_MAX_CONCURRENT, DEFAULT_MAX_TOTAL
    runtime = SubAgentRuntime(
        SubAgentManager(parent, max_concurrent=DEFAULT_MAX_CONCURRENT, max_total=DEFAULT_MAX_TOTAL),
        store, lambda controller: SimpleNamespace(graph=Graph()), 'test-model',
    )
    result = runtime.invoke({'messages': [HumanMessage(content=description)]},
                            {'configurable': {'thread_id': 'parent'}})
    assert result['messages'][0].content.endswith(answer)
    raw = next(tmp_path.rglob('*.jsonl')).read_text()
    assert key not in raw
    assert '$KNOWN' in raw


def test_runtime_cleans_up_when_log_writes_fail(tmp_path, monkeypatch, caplog):
    import logging
    from types import SimpleNamespace
    from langchain_core.messages import AIMessage, HumanMessage
    from agent.runtime.control import RunController
    from agent.subagents.configuration import DEFAULT_MAX_CONCURRENT, DEFAULT_MAX_TOTAL
    from agent.subagents.manager import SubAgentManager
    from agent.subagents.runtime import SubAgentRuntime

    store = SubAgentLogStore(tmp_path)

    def fail_append(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "append", fail_append)
    cleaned = []
    monkeypatch.setattr(
        "agent.execution.environment.reset_environment_cache",
        lambda token: cleaned.append(("env", token)),
    )

    def fail_close(model):
        cleaned.append(("http", model))
        raise OSError("close failed")

    monkeypatch.setattr("agent.models.stream_lifecycle.close_model_http_clients", fail_close)
    model = object()

    class Graph:
        def stream(self, graph_input, config, **kwargs):
            yield {"messages": [AIMessage(content="done")]}

    parent = RunController()
    manager = SubAgentManager(parent, max_concurrent=DEFAULT_MAX_CONCURRENT, max_total=DEFAULT_MAX_TOTAL)
    runtime = SubAgentRuntime(
        manager, store,
        lambda controller: SimpleNamespace(graph=Graph(), owns_model_clients=True, model=model),
        "test-model",
    )
    monkeypatch.setattr(runtime, "_borrow_environment", lambda: "env-token")
    child = RunController()
    child.begin_run()
    with caplog.at_level(logging.ERROR, logger="agent.subagents.runtime"):
        result = runtime._run(
            {"messages": [HumanMessage(content="work")]},
            "parent", uuid4().hex, child, tool_call_id="call",
        )
    assert result["messages"][0].additional_kwargs["status"] == "completed"
    assert result["messages"][0].content == "done"
    assert cleaned == [("env", "env-token"), ("http", model)]
    assert "Subagent log write failed" in caplog.text
    assert "Subagent model client cleanup failed" in caplog.text


def test_runtime_releases_resources_when_the_task_is_interrupted(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from langchain_core.messages import HumanMessage
    from agent.runtime.control import RunController
    from agent.subagents.configuration import DEFAULT_MAX_CONCURRENT, DEFAULT_MAX_TOTAL
    from agent.subagents.manager import SubAgentManager
    from agent.subagents.runtime import SubAgentRuntime

    cleaned = []
    monkeypatch.setattr(
        "agent.execution.environment.reset_environment_cache",
        lambda token: cleaned.append(token),
    )
    monkeypatch.setattr(
        "agent.models.stream_lifecycle.close_model_http_clients",
        lambda model: cleaned.append(model),
    )
    model = object()

    class Graph:
        def stream(self, graph_input, config, **kwargs):
            raise KeyboardInterrupt()
            yield {}

    parent = RunController()
    manager = SubAgentManager(parent, max_concurrent=DEFAULT_MAX_CONCURRENT, max_total=DEFAULT_MAX_TOTAL)
    store = SubAgentLogStore(tmp_path)
    runtime = SubAgentRuntime(
        manager, store,
        lambda controller: SimpleNamespace(graph=Graph(), owns_model_clients=True, model=model),
        "test-model",
    )
    monkeypatch.setattr(runtime, "_borrow_environment", lambda: "env-token")
    child = RunController()
    child.begin_run()
    with pytest.raises(KeyboardInterrupt):
        runtime._run(
            {"messages": [HumanMessage(content="work")]},
            "parent", uuid4().hex, child, tool_call_id="call",
        )
    assert cleaned == ["env-token", model]


def test_task_metadata_recovers_public_fields_from_finish_only(tmp_path):
    store = SubAgentLogStore(tmp_path)
    run = uuid4().hex
    store.append(
        "parent", run, "finish", status="completed", result="from-finish",
        mode="background", parent_run_id="owner", tool_call_id="tc", depth=2,
        description="recovered", elapsed_seconds=3,
    )
    meta = store.task_metadata("parent", run)
    assert meta["status"] == "completed"
    assert meta["result"] == "from-finish"
    assert meta["mode"] == "background"
    assert meta["parent_run_id"] == "owner"
    assert meta["tool_call_id"] == "tc"
    assert meta["depth"] == 2
    assert meta["description"] == "recovered"
    assert meta["started_at"] == ""
    assert meta["elapsed_seconds"] == 3

    legacy = uuid4().hex
    store.append(
        "parent", legacy, "finish", status="completed", summary="legacy-only",
        description="old", mode="sync", parent_run_id="parent", tool_call_id="x", depth=1,
    )
    legacy_meta = store.task_metadata("parent", legacy)
    assert legacy_meta["status"] == "completed"
    assert "result" not in legacy_meta
    assert "result_summary" not in legacy_meta


def test_read_task_merges_live_tasks_with_history_before_paging(tmp_path):
    import os
    from types import SimpleNamespace
    from agent.runtime.control import RunController
    from agent.subagents.configuration import DEFAULT_MAX_CONCURRENT, DEFAULT_MAX_TOTAL
    from agent.subagents.manager import SubAgentManager
    from agent.tools.subagent_tasks import build_subagent_tools

    store = SubAgentLogStore(tmp_path)
    parent = RunController()
    manager = SubAgentManager(parent, max_concurrent=DEFAULT_MAX_CONCURRENT, max_total=DEFAULT_MAX_TOTAL)
    historical, logged, live_only = uuid4().hex, uuid4().hex, uuid4().hex
    store.append(
        "parent", historical, "finish", status="completed", result="history",
        description="old", mode="sync", parent_run_id="parent", tool_call_id="h", depth=1,
    )
    store.append(
        "parent", logged, "finish", status="completed", result="stale-log",
        description="logged", mode="sync", parent_run_id="parent", tool_call_id="l", depth=1,
    )
    directory = next(tmp_path.rglob("*.jsonl")).parent
    os.utime(directory / f"{historical}.jsonl", (1, 1))
    os.utime(directory / f"{logged}.jsonl", (2, 2))
    logged_task = manager.open_record(
        run_id=logged, parent_run_id="parent", description="live", tool_call_id="l",
        controller=RunController(), mode="sync", depth=1,
    )
    manager.record_terminal(logged, "completed", "live-result", 1.0)
    manager.open_record(
        run_id=live_only, parent_run_id="parent", description="only", tool_call_id="o",
        controller=RunController(), mode="background", depth=1,
    )
    tool = next(item for item in build_subagent_tools(
        None, manager, store, names=frozenset({"read_task"}), allow_background=False,
    ) if item.name == "read_task")
    opened = []
    original = store.task_metadata

    def tracking(parent_id, run_id):
        opened.append(run_id)
        return original(parent_id, run_id)

    store.task_metadata = tracking

    def read(**kwargs):
        runtime = SimpleNamespace(config={"configurable": {"thread_id": "parent"}}, tool_call_id="read")
        value = tool.func(runtime, **kwargs)
        if isinstance(value, tuple):
            value = value[0]
        return json.loads(value)

    first = read(limit=1)
    assert [item["run_id"] for item in first["tasks"]] == [live_only]
    assert first["tasks"][0]["status"] == "running"
    assert first["next_offset"] == 1
    assert opened == []
    second = read(offset=1, limit=1)
    assert second["tasks"][0]["run_id"] == logged
    assert second["tasks"][0]["status"] == "completed"
    assert second["next_offset"] == 2
    assert opened == []
    third = read(offset=2, limit=1)
    assert third["tasks"][0]["run_id"] == historical
    assert opened == [historical]
    assert read(run_id=logged)["result"] == "live-result"
    assert logged_task.result == "live-result"


def test_read_task_uses_the_snapshot_taken_under_the_manager_lock(tmp_path):
    from types import SimpleNamespace
    from agent.runtime.control import RunController
    from agent.subagents.configuration import DEFAULT_MAX_CONCURRENT, DEFAULT_MAX_TOTAL
    from agent.subagents.manager import SubAgentManager
    from agent.tools.subagent_tasks import build_subagent_tools

    parent = RunController()
    manager = SubAgentManager(parent, max_concurrent=DEFAULT_MAX_CONCURRENT, max_total=DEFAULT_MAX_TOTAL)
    record = manager.open_record(
        run_id=uuid4().hex, parent_run_id="parent", description="live", tool_call_id="call",
        controller=RunController(), mode="background", depth=1,
    )
    snapshot = manager.tasks_for("parent")
    record.status = "completed"
    record.result = "changed after the lock"
    assert snapshot[0]["status"] == "running"
    assert snapshot[0]["result"] is None
    record.status = "running"
    record.result = None

    tool = next(item for item in build_subagent_tools(
        None, manager, SubAgentLogStore(tmp_path), names=frozenset({"read_task"}), allow_background=False,
    ) if item.name == "read_task")
    original = manager.tasks_for

    def snapshot_then_mutate(parent_id, run_id=None):
        copied = original(parent_id, run_id)
        record.status = "completed"
        record.result = "changed after the lock"
        return copied

    manager.tasks_for = snapshot_then_mutate

    def read(**kwargs):
        runtime = SimpleNamespace(config={"configurable": {"thread_id": "parent"}}, tool_call_id="read")
        value = tool.func(runtime, **kwargs)
        if isinstance(value, tuple):
            value = value[0]
        return json.loads(value)

    listed = read(limit=1)
    assert listed["tasks"][0]["run_id"] == record.run_id
    assert listed["tasks"][0]["status"] == "running"
    record.status = "running"
    record.result = None
    detail = read(run_id=record.run_id)
    assert detail["status"] == "running"
    assert "result" not in detail


def test_store_reads_stay_consistent_while_appends_continue(tmp_path):
    import threading

    store = SubAgentLogStore(tmp_path)
    run = uuid4().hex
    stop = threading.Event()
    errors = []

    def writer():
        index = 0
        while not stop.is_set():
            store.append("parent", run, "tool", note=str(index))
            index += 1

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(40):
            listed = store.list_runs("parent")
            meta = store.task_metadata("parent", run)
            page = store.read("parent", run, 0, 10)
            assert listed == [run]
            assert meta is None or meta["run_id"] == run
            assert all(record["run_id"] == run for record in page["records"])
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)
    finally:
        stop.set()
        thread.join()
    assert errors == []


def test_restore_keeps_checkpoint_result_and_confirmed_status(tmp_path):
    from agent.cli.state import CliState, ToolBlock

    store = SubAgentLogStore(tmp_path)
    truncated, confirmed = uuid4().hex, uuid4().hex
    store.append(
        "root", truncated, "finish", result_summary="short summary", summary_truncated=True,
        description="task", mode="sync", parent_run_id="root", tool_call_id="card", depth=1,
    )
    store.append(
        "root", confirmed, "finish", status="completed", result="LOG RESULT",
        description="other", mode="sync", parent_run_id="root", tool_call_id="other", depth=1,
    )
    state = CliState()
    state.blocks.extend([
        ToolBlock(
            tool_call_id="card", name="start_task", arguments={"description": "task"},
            output="FULL CHECKPOINT RESULT", status="completed",
        ),
        ToolBlock(
            tool_call_id="other", name="start_task", arguments={"description": "other"},
            output="CHECKPOINT", status="cancelled",
        ),
    ])
    state.restore_task_tree(store, "root")
    restored = state.subagent_nodes[truncated]
    assert restored.status == "completed"
    assert restored.result == "FULL CHECKPOINT RESULT"
    assert state.blocks[0].output == "FULL CHECKPOINT RESULT"
    assert state.blocks[0].status == "completed"
    assert state.subagent_nodes[confirmed].status == "completed"
    assert state.blocks[1].status == "completed"
    assert state.blocks[1].output == "LOG RESULT"


def test_cancelled_background_finish_wakes_an_idle_agent():
    from types import SimpleNamespace
    from agent.cli.run_events_controller import RunEventsController
    from agent.cli.state import CliState
    from agent.cli.view_state import DialogState, RunPresentation
    from agent.runtime.contracts import RunEvent

    woken = []
    state = CliState()
    controller = RunEventsController(
        request_summary=lambda request: None,
        _allow_confirm_open=lambda: False,
        _announce_mcp_switch=lambda: None,
        _note_interrupt_behind_allow_confirm=lambda status: None,
        _reopen_pending_interaction=lambda: None,
        _show_allow_confirm_status=lambda: None,
        _start_queued_run=lambda: woken.append("wake"),
        _working_messages=lambda: SimpleNamespace(reset=lambda: None),
        buffer=SimpleNamespace(),
        dialogs=DialogState(),
        run_view=RunPresentation(),
        runner=SimpleNamespace(),
        set_status=lambda *args, **kwargs: None,
        state=state,
        invalidate=lambda: None,
    )
    event = RunEvent("subagent_finished", result={"status": "cancelled", "run_id": "child"})
    controller._apply_event(event)
    assert woken == ["wake"]
    state.running = True
    controller._apply_event(event)
    assert woken == ["wake"]
