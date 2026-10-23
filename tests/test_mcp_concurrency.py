"""Shared MCP sessions queue same-server calls and recover from dead SDK sessions."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from deepagents.backends import StateBackend
from fastmcp import FastMCP
from langchain_core.messages import AIMessage, ToolMessage

from agent.factory import create_agent
from agent.mcp.manager import MCPManager
from agent.runner import AgentRunner
from agent.sandbox import BackendSelection, ExecutionMode
from tests.conftest import scripted_model
from tests.mcp_fixture import HttpFixture, text_of


@pytest.fixture
def concurrent_server():
    stats = {'active': 0, 'maximum': 0, 'calls': []}

    def server_factory(auth):
        server = FastMCP('concurrent-fixture', auth=auth)

        async def work(value: int, delay: float = .03) -> str:
            """Return an identifier after a delay."""
            stats['active'] += 1
            stats['maximum'] = max(stats['maximum'], stats['active'])
            stats['calls'].append(value)
            try:
                await asyncio.sleep(delay)
                return f'completed:{value}'
            finally:
                stats['active'] -= 1

        for index in range(6):
            server.tool(name=f'work_{index}')(work)
        return server

    server = HttpFixture(server_factory=server_factory)
    try:
        yield server, stats
    finally:
        server.close()


def _manager(path, server):
    manager = MCPManager(path, connect_timeout=5, call_timeout=8)
    result = manager.add_from_fields(name='local', transport='streamable_http', url=server.url)
    assert result.ok, result.message
    return manager


def test_six_tool_model_batch_is_safe_and_next_call_still_works(tmp_path, concurrent_server):
    server, stats = concurrent_server
    manager = _manager(tmp_path / 'mcp.json', server)
    model = scripted_model([
        AIMessage(content='', tool_calls=[{
            'id': f'batch-{i}', 'name': f'mcp__local__work_{i}', 'args': {'value': i},
        } for i in range(6)]),
        AIMessage(content='batch complete'),
        AIMessage(content='', tool_calls=[{
            'id': 'after-batch', 'name': 'mcp__local__work_0', 'args': {'value': 9},
        }]),
        AIMessage(content='still healthy'),
    ])
    runner = None
    try:
        with patch('agent.factory.select_backend', return_value=BackendSelection(StateBackend(), ExecutionMode.SANDBOXED)):
            prepared = create_agent(model=model, interrupt_on={}, mcp=manager)
            runner = AgentRunner(prepared=prepared, mcp_manager=manager)
            first = runner.invoke('run the six tools together')
            assert first.status == 'completed', first.error
            messages = runner.prepared.graph.get_state(runner._thread_config()).values['messages']
            outputs = [m for m in messages if isinstance(m, ToolMessage)]
            assert len(outputs) == 6
            assert all(m.status == 'success' for m in outputs)
            assert sorted(stats['calls']) == list(range(6))
            assert stats['maximum'] == 1
            assert runner.invoke('call once more').status == 'completed'
            assert stats['calls'][-1] == 9
    finally:
        runner.close() if runner else manager.close()


def test_sync_and_async_calls_share_one_server_queue(tmp_path, concurrent_server):
    server, stats = concurrent_server
    manager = _manager(tmp_path / 'mcp.json', server)
    tools = manager.get('local').tools

    async def run():
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(tools[i].invoke, {'value': i}) for i in range(3)]
            results = await asyncio.gather(
                *[asyncio.wrap_future(f) for f in futures],
                *[tools[i].ainvoke({'value': i}) for i in range(3, 6)],
            )
        assert all(f'completed:{i}' in text_of(results[i]) for i in range(6))

    try:
        asyncio.run(run())
        assert stats['maximum'] == 1
        assert 'completed:99' in text_of(tools[0].invoke({'value': 99}))
    finally:
        manager.close()


async def _break_pinned_session(client):
    # Reproduce the SDK invariant in the report: its runner exits while the
    # manager still holds an outer context, leaving a nonzero nesting counter.
    task = client._session_state.session_task
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not client.is_connected()
    assert client._session_state.nesting_counter == 1


def test_existing_tool_recovers_dead_session_without_replaying_calls(tmp_path, http_mcp):
    manager = _manager(tmp_path / 'mcp.json', http_mcp)
    client = manager._states['local'].client
    tool = next(t for t in manager.get('local').tools if t.name.endswith('__echo'))
    try:
        assert 'before' in text_of(tool.invoke({'text': 'before'}))
        manager._bridge.call(_break_pinned_session(client), timeout=5)
        assert 'recovered' in text_of(tool.invoke({'text': 'recovered'}))
        assert 'healthy' in text_of(asyncio.run(tool.ainvoke({'text': 'healthy'})))
    finally:
        manager.close()


def test_identical_add_rebuilds_a_dead_client_without_remove(tmp_path, http_mcp):
    manager = _manager(tmp_path / 'mcp.json', http_mcp)
    client = manager._states['local'].client
    try:
        manager._bridge.call(_break_pinned_session(client), timeout=5)
        result = manager.add_from_fields(name='local', transport='streamable_http', url=http_mcp.url)
        assert result.ok, result.message
        assert manager._states['local'].client is not client
        tool = next(t for t in manager.get('local').tools if t.name.endswith('__echo'))
        assert 'reconnected' in text_of(tool.invoke({'text': 'reconnected'}))
    finally:
        manager.close()


def test_cancelling_queued_call_never_executes_it_or_blocks_next_call(tmp_path, concurrent_server):
    server, stats = concurrent_server
    manager = _manager(tmp_path / 'mcp.json', server)
    tool = manager.get('local').tools[0]

    async def run():
        first = asyncio.create_task(tool.ainvoke({'value': 1, 'delay': .3}))
        async with asyncio.timeout(3):
            while 1 not in stats['calls']:
                await asyncio.sleep(.01)
        queued = asyncio.create_task(tool.ainvoke({'value': 2}))
        await asyncio.sleep(.05)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert 'completed:1' in text_of(await first)
        assert 'completed:3' in text_of(await tool.ainvoke({'value': 3}))
        assert stats['calls'] == [1, 3]

    try:
        asyncio.run(run())
    finally:
        manager.close()


def test_distinct_mcp_sessions_can_still_progress_concurrently(tmp_path, concurrent_server):
    server, stats = concurrent_server
    manager = _manager(tmp_path / 'mcp.json', server)
    try:
        assert manager.add_from_fields(name='other', transport='streamable_http', url=server.url).ok
        local = manager.get('local').tools[0]
        other = manager.get('other').tools[0]

        async def run():
            return await asyncio.gather(local.ainvoke({'value': 1, 'delay': .2}),
                                        other.ainvoke({'value': 2, 'delay': .2}))

        results = asyncio.run(run())
        assert 'completed:1' in text_of(results[0])
        assert 'completed:2' in text_of(results[1])
        assert stats['maximum'] == 2
    finally:
        manager.close()
