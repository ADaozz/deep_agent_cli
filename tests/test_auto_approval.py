import asyncio
from unittest.mock import patch

import pytest
from deepagents.backends import StateBackend
from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.bootstrap.config import Settings
from agent.execution.backend import BackendSelection
from agent.execution.types import ExecutionMode
from agent.runtime.auto_approval import ApprovalJournal, Decision
from agent.runtime.runner import AgentRunner
from tests.conftest import scripted_model


class CountingBackend(StateBackend, SandboxBackendProtocol):
    calls = []

    def execute(self, command, *, timeout=None):
        self.calls.append(command)
        return ExecuteResponse(output="ok", exit_code=0)


def test_auto_mixed_batch_reuses_decisions_and_matches_ids(monkeypatch):
    backend = CountingBackend()
    backend.calls = []
    reviews = []

    async def review(key, trust_env, evidence, cancelled):
        import json
        name = json.loads(evidence)["arguments"]["command"]
        reviews.append(name)
        return Decision(name, "reviewed")

    monkeypatch.setattr("agent.runtime.auto_approval.classify", review)
    model = scripted_model([
        AIMessage(content="", tool_calls=[
            {"name": "execute", "id": "id-" + command, "args": {"command": command}}
            for command in ("allow", "ask", "deny")
        ]),
        AIMessage(content="done"),
    ])
    with patch("agent.bootstrap.factory.select_backend", return_value=BackendSelection(backend, ExecutionMode.SANDBOXED)):
        runner = AgentRunner(model=model, settings=Settings(resolved_deepseek_api_key="test"))
        runner.set_permission_mode("auto")
        pending = runner.invoke("review these operations")
        assert [c["toolCallId"] for c in pending.pending_tool_calls] == ["id-ask"]
        assert backend.calls == []
        assert runner.approve_tool("id-ask").status == "completed"
        assert sorted(backend.calls) == ["allow", "ask"]
        assert reviews == ["allow", "ask", "deny"]
        messages = runner.prepared.graph.get_state({"configurable": {"thread_id": runner.thread_id}}).values["messages"]
        assert any(isinstance(m, ToolMessage) and m.tool_call_id == "id-deny" and m.status == "error" for m in messages)


def test_auto_journal_persists_decision_and_execution_claim(tmp_path):
    path = str(tmp_path / "session.sqlite")
    with SqliteSaver.from_conn_string(path) as saver:
        journal = ApprovalJournal(saver)
        journal.record("digest", Decision("allow", "authorized"))
        assert journal.transition("digest", "pending", "started")
    with SqliteSaver.from_conn_string(path) as saver:
        restored = ApprovalJournal(saver)
        assert restored.lookup("digest").decision == "allow"
        assert not restored.transition("digest", "pending", "started")


def test_auto_missing_key_falls_back_to_ask():
    runner = AgentRunner(model=scripted_model([]), backend=StateBackend())
    assert runner.request_permission_change("auto") == "ask"


def test_subagent_manual_decision_returns_to_parent_without_interrupt(tmp_path, monkeypatch):
    calls = []

    async def review(key, trust_env, evidence, cancelled):
        import json
        calls.append(json.loads(evidence))
        return Decision("ask", "review")

    monkeypatch.setattr("agent.runtime.auto_approval.classify", review)
    backend = CountingBackend()
    backend.calls = []
    model = scripted_model([
        AIMessage(content="", tool_calls=[{"name": "start_task", "id": "delegate", "args": {
            "description": "inspect delegated item", "subagent_type": "general-purpose"}}]),
        AIMessage(content="", tool_calls=[{"name": "execute", "id": "child-call", "args": {"command": "test"}}]),
        AIMessage(content="parent approval required"), AIMessage(content="done"),
    ])
    with patch("agent.bootstrap.factory.select_backend", return_value=BackendSelection(backend, ExecutionMode.SANDBOXED)):
        runner = AgentRunner(model=model, settings=Settings(config_dir=tmp_path, resolved_deepseek_api_key="test"))
        runner.set_permission_mode("auto")
        assert runner.invoke("original parent request").status == "waiting_confirmation"
        assert runner.approve_tool("delegate").status == "completed"
        assert backend.calls == []
        assert calls[0]["user_request"] == "original parent request"
        assert calls[0]["current_task"] == "inspect delegated item"


def test_mcp_auto_trust_is_explicit_and_persistent():
    from agent.mcp.config import parse_server, MCPConfigError

    body = {"transport": "stdio", "command": "example"}
    assert not parse_server("test", body).auto_approve
    trusted = parse_server("test", {**body, "auto_approve": True})
    assert parse_server("test", trusted.to_json()) == trusted
    with pytest.raises(MCPConfigError):
        parse_server("test", {**body, "auto_approve": "false"})


@pytest.mark.parametrize("keep_key", [True, False])
def test_pending_auto_batch_survives_restart(tmp_path, monkeypatch, keep_key):
    import json
    from agent.sessions.store import SessionStore

    calls = []

    async def review(key, trust_env, evidence, cancelled):
        action = json.loads(evidence)["arguments"]["command"]
        calls.append(action)
        return Decision(action, action)

    monkeypatch.setattr("agent.runtime.auto_approval.classify", review)
    backend = CountingBackend()
    backend.calls = []
    db = tmp_path / "session.sqlite"
    settings = Settings(resolved_deepseek_api_key="key")
    with patch("agent.bootstrap.factory.select_backend", return_value=BackendSelection(backend, ExecutionMode.SANDBOXED)):
        store = SessionStore(db)
        runner = AgentRunner(session_store=store, settings=settings, model=scripted_model([
            AIMessage(content="", tool_calls=[{"name": "execute", "id": x, "args": {"command": x}}
                                              for x in ("allow", "ask", "deny")]),
        ]))
        thread = runner.thread_id
        runner.set_permission_mode("auto")
        try:
            assert runner.invoke("review calls").status == "waiting_confirmation"
        finally:
            runner.close()
            store.close()
        store = SessionStore(db)
        restored = AgentRunner(session_store=store, thread_id=thread, settings=settings if keep_key else Settings(),
                               model=scripted_model([AIMessage(content="done")]))
        try:
            assert restored.permission_mode() == ("auto" if keep_key else "ask")
            assert restored.approve_tool("ask").status == "completed"
            assert sorted(backend.calls) == (["allow", "ask"] if keep_key else ["ask"])
            assert calls == ["allow", "ask", "deny"]
        finally:
            restored.close()
            store.close()


@pytest.mark.parametrize(("responses", "outcome", "count"), [
    ([{"decision": "allow", "reason": "authorized"}], "allow", 1),
    ([{"decision": "ask", "reason": "uncertain"}, {"decision": "deny", "reason": "scope"}], "deny", 2),
    ([{"decision": "allow", "reason": "uncertain"}], "ask", 1),
    ([{}], "ask", 1),
    ([None], "ask", 1),
])
def test_independent_classifier(monkeypatch, responses, outcome, count):
    import httpx
    import json
    from agent.runtime.auto_approval import classify

    sent = []
    original = httpx.AsyncClient

    def respond(request):
        assert str(request.url) == "https://api.deepseek.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer unified"
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash"
        sent.append(body)
        item = responses[len(sent) - 1]
        if item is None:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(item)}}]})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    assert asyncio.run(classify("unified", False, "example", lambda: False)).decision == outcome
    assert len(sent) == count


def test_cancelled_classifier_request_is_closed(monkeypatch):
    import httpx
    from agent.models.stream_lifecycle import ModelStreamCancelled
    from agent.runtime.auto_approval import classify

    async def scenario():
        started = asyncio.Event()
        closed = asyncio.Event()
        original = httpx.AsyncClient

        async def respond(request):
            started.set()
            try:
                await asyncio.Future()
            finally:
                closed.set()

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
        with pytest.raises(ModelStreamCancelled):
            await classify("unified", False, "example", started.is_set)
        assert closed.is_set()

    asyncio.run(scenario())


@pytest.mark.sandbox
@pytest.mark.parametrize("mode", ["restricted", "full-access"])
def test_auto_cli_workspace_write_and_per_command_network(tmp_path, mode):
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from agent.cli.app import CliApplication
    from agent.execution.environment import network_rule
    from agent.execution.policy import SandboxConfig

    workspace = tmp_path / "work"
    workspace.mkdir()
    settings = Settings(config_dir=tmp_path / "private", resolved_deepseek_api_key="unit-key",
                        sandbox=SandboxConfig(workspace=workspace, mode=mode))
    target = workspace / "result.txt"
    runner = AgentRunner(settings=settings, model=scripted_model([
        AIMessage(content="", tool_calls=[{"id": "write", "name": "write",
                                          "args": {"file_path": str(target), "content": "verified"}}]),
        AIMessage(content="done"),
    ]))
    try:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            asyncio.run(app.access_selection.select_permission("auto"))
        assert runner.permission_mode() == "auto"
        assert network_rule(runner.permission_mode()) == "per-command"
        assert runner.invoke("write result.txt").status == "completed"
        assert target.read_text() == "verified"
    finally:
        runner.close()


def test_external_tools_follow_server_trust_snapshot():
    from langchain_core.tools import StructuredTool

    invoked = []

    class Catalog:
        def snapshot(self):
            def remote(name):
                def invoke():
                    invoked.append(name)
                    return "done"
                return StructuredTool.from_function(invoke, name=name, description="Remote service")

            return 7, [remote("mcp__trusted__tool"), remote("mcp__manual__tool")]

        def snapshot_trusted_tools(self, revision):
            assert revision == 7
            return {"mcp__trusted__tool"}

        def snapshot_includes_mcp(self, revision):
            return True

        def commit(self, revision):
            pass

        def discard(self, revision):
            pass

    from agent.bootstrap.factory import create_agent
    model = scripted_model([
        AIMessage(content="", tool_calls=[{"name": name, "id": name, "args": {}} for name in
                                         ("mcp__trusted__tool", "mcp__manual__tool")]),
        AIMessage(content="done"),
    ])
    with patch("agent.bootstrap.factory.select_backend", return_value=BackendSelection(StateBackend(), ExecutionMode.SANDBOXED)):
        graph = create_agent(model=model, mcp=Catalog(), settings=Settings(resolved_deepseek_api_key="test"),
                             permission_mode="auto")
        runner = AgentRunner(prepared=graph)
        waiting = runner.invoke("query both servers")
        assert [call["toolCallId"] for call in waiting.pending_tool_calls] == ["mcp__manual__tool"]
        assert runner.reject_tool("mcp__manual__tool").status == "completed"
        assert invoked == ["mcp__trusted__tool"]
