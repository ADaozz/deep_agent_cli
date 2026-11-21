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
