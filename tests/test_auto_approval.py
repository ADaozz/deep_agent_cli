import asyncio
from dataclasses import replace
import json
import sqlite3
from unittest.mock import patch

import pytest
from deepagents.backends import StateBackend
from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.bootstrap.config import Settings
from agent.bootstrap.factory import AgentSpec
from agent.execution.backend import BackendSelection
from agent.execution.policy import BindMount, SandboxConfig
from agent.execution.types import ExecutionMode
from agent.runtime.auto_approval import ApprovalJournal, AutoApprovalMiddleware, Decision, delegating_user_request
from agent.runtime.control import RunController
from agent.runtime.permissions import ask_interrupt_on
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
    deny_finished = asyncio.Event()

    async def review(key, trust_env, evidence, cancelled):
        name = json.loads(evidence)["action"]["args"]["command"]
        if name == "deny":
            deny_finished.set()
        else:
            await asyncio.wait_for(deny_finished.wait(), timeout=2)
        reviews.append(name)
        return Decision(name, "reviewed")

    monkeypatch.setattr("agent.runtime.auto_approval.classify", review)
    model = scripted_model([
        AIMessage(content="", tool_calls=[
            {"name": "execute", "id": "id-" + command, "args": {"command": command, "network": True}}
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
        assert reviews[0] == "deny"
        assert sorted(reviews) == ["allow", "ask", "deny"]
        messages = runner.prepared.graph.get_state({"configurable": {"thread_id": runner.thread_id}}).values["messages"]
        assert any(isinstance(m, ToolMessage) and m.tool_call_id == "id-deny" and m.status == "error" for m in messages)


def test_auto_journal_persists_decision_and_execution_claim(tmp_path):
    path = str(tmp_path / "session.sqlite")
    with SqliteSaver.from_conn_string(path) as saver:
        journal = ApprovalJournal(saver)
        journal.record("digest", Decision("allow", "authorized"), "policy")
        assert journal.claim("digest", "pending", "policy") is None
    with SqliteSaver.from_conn_string(path) as saver:
        restored = ApprovalJournal(saver)
        assert restored.lookup("digest").decision == "allow"
        assert "重复执行" in restored.claim("digest", "pending", "different")
        assert restored.transition("digest", "started", "finished")
        assert "重复执行" in restored.claim("digest", "pending", "policy")


@pytest.mark.parametrize(("mode", "reviewed"), [("restricted", []), ("full-access", ["local"])])
def test_offline_command_skips_review_only_in_restricted_sandbox(tmp_path, monkeypatch, mode, reviewed):
    reviews = []

    async def review(key, trust_env, evidence, cancelled):
        reviews.append(json.loads(evidence)["action"]["args"]["command"])
        return Decision("allow", "reviewed")

    monkeypatch.setattr("agent.runtime.auto_approval.classify", review)
    backend = CountingBackend()
    backend.calls = []
    settings = Settings(resolved_deepseek_api_key="test", sandbox=SandboxConfig(workspace=tmp_path, mode=mode))
    model = scripted_model([
        AIMessage(content="", tool_calls=[{"name": "execute", "id": "local", "args": {"command": "local"}}]),
        AIMessage(content="done"),
    ])
    with patch("agent.bootstrap.factory.select_backend", return_value=BackendSelection(backend, ExecutionMode.SANDBOXED)):
        runner = AgentRunner(model=model, settings=settings)
        runner.set_permission_mode("auto")
        assert runner.invoke("run the local check").status == "completed"
        assert backend.calls == ["local"]
        assert reviews == reviewed


def test_blocked_call_returns_reason_and_third_block_in_a_row_goes_to_human(monkeypatch):
    async def review(key, trust_env, evidence, cancelled):
        return Decision("deny", "操作超出用户授权范围。")

    monkeypatch.setattr("agent.runtime.auto_approval.classify", review)
    backend = CountingBackend()
    backend.calls = []
    model = scripted_model([
        *(AIMessage(content="", tool_calls=[{"name": "execute", "id": name,
                                            "args": {"command": name, "network": True}}])
          for name in ("first", "second", "third")),
        AIMessage(content="done"),
    ])
    with patch("agent.bootstrap.factory.select_backend", return_value=BackendSelection(backend, ExecutionMode.SANDBOXED)):
        runner = AgentRunner(model=model, settings=Settings(resolved_deepseek_api_key="test"))
        runner.set_permission_mode("auto")
        pending = runner.invoke("publish the report")
        assert [call["toolCallId"] for call in pending.pending_tool_calls] == ["third"]
        assert backend.calls == []
        messages = runner.prepared.graph.get_state(runner._thread_config()).values["messages"]
        blocked = {m.tool_call_id: m.content for m in messages if isinstance(m, ToolMessage) and m.status == "error"}
        assert set(blocked) == {"first", "second"}
        assert all("操作超出用户授权范围。" in text and "不要改写命令绕过" in text for text in blocked.values())
        assert runner.approve_tool("third").status == "completed"
        assert backend.calls == ["third"]


def test_block_counters_reset_on_allow_and_after_reaching_a_limit():
    journal = ApprovalJournal(object())
    allow, deny, ask = (Decision(kind, "reason") for kind in ("allow", "deny", "ask"))
    assert [journal.count_review("a", d) for d in (deny, deny, ask, allow, deny, deny, deny)] == [False] * 6 + [True]
    # Threads are counted separately, and reaching a limit starts both counts over.
    assert [journal.count_review("b", d) for d in (deny, deny)] == [False, False]
    assert [journal.count_review("a", d) for d in (deny, deny, deny)] == [False, False, True]
    # Blocks that never come three in a row still reach the per-session limit.
    pattern = [deny, deny, allow] * 9 + [deny, deny]
    assert [journal.count_review("c", d) for d in pattern] == [False] * 28 + [True]


def test_auto_missing_key_falls_back_to_ask():
    runner = AgentRunner(model=scripted_model([]), backend=StateBackend())
    assert runner.request_permission_change("auto") == "ask"


def test_subagent_manual_decision_returns_to_parent_without_interrupt(tmp_path, monkeypatch):
    calls = []

    async def review(key, trust_env, evidence, cancelled):
        calls.append(json.loads(evidence))
        return Decision("ask", "review")

    monkeypatch.setattr("agent.runtime.auto_approval.classify", review)
    backend = CountingBackend()
    backend.calls = []
    model = scripted_model([
        AIMessage(content="", tool_calls=[{"name": "start_task", "id": "delegate", "args": {
            "description": "inspect delegated item"}}]),
        AIMessage(content="", tool_calls=[{"name": "execute", "id": "child-call",
                                          "args": {"command": "test", "network": True}}]),
        AIMessage(content="done"),
    ])
    with patch("agent.bootstrap.factory.select_backend", return_value=BackendSelection(backend, ExecutionMode.SANDBOXED)):
        runner = AgentRunner(model=model, settings=Settings(config_dir=tmp_path, resolved_deepseek_api_key="test"))
        runner.set_permission_mode("auto")
        assert runner.invoke("original parent request").status == "completed"
        assert backend.calls == []
        assert calls[0]["user_messages"] == ["original parent request"]
        assert calls[0]["delegated_task"] == "inspect delegated item"
        messages = runner.prepared.graph.get_state(runner._thread_config()).values["messages"]
        reply = next(m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == "delegate")
        assert "execute (child-call)" in reply.content
        assert "需要人工审批" in reply.content


def test_delegation_context_preserves_root_user_on_nested_agent():
    controller = RunController()
    controller.approval_user_request = "real user authorization"
    middleware = AutoApprovalMiddleware(
        spec=AgentSpec(role="subagent", depth=1),
        backend=object(),
        controller=controller,
        journal=ApprovalJournal(InMemorySaver()),
        gated={},
    )
    request = type("Req", (), {
        "tool_call": {"name": "start_task"},
        "state": {"messages": [HumanMessage(content="expanded model-written task")]},
    })()
    with middleware.delegation_context(request):
        assert delegating_user_request.get() == "real user authorization"


def test_depth2_flash_evidence_keeps_root_user_not_mid_task(tmp_path, monkeypatch):
    """二级 SubAgent 的 user_messages 必须是 Root 用户原文，不能是一级委派描述。"""
    calls = []

    async def review(key, trust_env, evidence, cancelled):
        calls.append(json.loads(evidence))
        return Decision("ask", "review")

    monkeypatch.setattr("agent.runtime.auto_approval.classify", review)
    from agent.subagents.configuration import SubAgentConfig

    backend = CountingBackend()
    backend.calls = []
    model = scripted_model([
        AIMessage(content="", tool_calls=[{"name": "start_task", "id": "mid", "args": {
            "description": "expanded scope beyond user request"}}]),
        AIMessage(content="", tool_calls=[{"name": "start_task", "id": "leaf", "args": {
            "description": "leaf delegated work"}}]),
        AIMessage(content="", tool_calls=[{"name": "execute", "id": "leaf-call",
                                          "args": {"command": "test", "network": True}}]),
        AIMessage(content="mid done"),
        AIMessage(content="root done"),
    ])
    settings = Settings(
        config_dir=tmp_path,
        resolved_deepseek_api_key="test",
        subagents=SubAgentConfig(max_subagent_depth=2),
    )
    with patch("agent.bootstrap.factory.select_backend", return_value=BackendSelection(backend, ExecutionMode.SANDBOXED)):
        runner = AgentRunner(model=model, settings=settings)
        runner.set_permission_mode("auto")
        assert runner.invoke("only edit local files").status == "completed"
        assert calls, "expected Flash review on depth-2 execute"
        assert calls[0]["user_messages"] == ["only edit local files"]
        assert calls[0]["delegated_task"] == "leaf delegated work"
        assert "expanded scope" not in calls[0]["user_messages"][0]


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
    from agent.sessions.store import SessionStore

    calls = []

    async def review(key, trust_env, evidence, cancelled):
        action = json.loads(evidence)["action"]["args"]["command"]
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
            AIMessage(content="", tool_calls=[{"name": "execute", "id": x, "args": {"command": x, "network": True}}
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


def test_ask_mounts_replay_guard_only_for_the_thread_with_auto_history(tmp_path, monkeypatch):
    from agent.sessions.store import SessionStore

    async def review(key, trust_env, evidence, cancelled):
        return Decision("allow", "authorized")

    monkeypatch.setattr("agent.runtime.auto_approval.classify", review)
    backend = CountingBackend()
    backend.calls = []
    with patch("agent.bootstrap.factory.select_backend", return_value=BackendSelection(backend, ExecutionMode.SANDBOXED)):
        store = SessionStore(tmp_path / "session.sqlite")
        runner = AgentRunner(session_store=store, settings=Settings(resolved_deepseek_api_key="key"), model=scripted_model([
            AIMessage(content="", tool_calls=[{"name": "execute", "id": "c", "args": {"command": "c", "network": True}}]),
            AIMessage(content="done"),
        ]))
        try:
            assert not runner.prepared.approval_guard
            runner.set_permission_mode("auto")
            assert runner.invoke("run c").status == "completed"
            first = runner.thread_id
            runner.set_permission_mode("ask")
            assert runner.apply_pending_config_if_idle()
            assert runner.permission_mode() == "ask" and runner.prepared.approval_guard
            runner.new_session()
            assert runner.permission_mode() == "ask" and not runner.prepared.approval_guard
            second = runner.thread_id
            runner.switch_session(first)
            assert runner.permission_mode() == "ask" and runner.prepared.approval_guard
            runner.switch_session(second)
            assert runner.permission_mode() == "ask" and not runner.prepared.approval_guard
            runner.switch_session(first)
            assert runner.permission_mode() == "ask" and runner.prepared.approval_guard
        finally:
            runner.close()
            store.close()


def test_switch_guard_rebuild_failure_keeps_old_session(tmp_path, monkeypatch):
    from agent.sessions.store import SessionStore

    async def review(key, trust_env, evidence, cancelled):
        return Decision("allow", "authorized")

    monkeypatch.setattr("agent.runtime.auto_approval.classify", review)
    backend = CountingBackend()
    backend.calls = []
    with patch("agent.bootstrap.factory.select_backend", return_value=BackendSelection(backend, ExecutionMode.SANDBOXED)):
        store = SessionStore(tmp_path / "guard-rollback.sqlite")
        runner = AgentRunner(session_store=store, settings=Settings(resolved_deepseek_api_key="key"), model=scripted_model([
            AIMessage(content="", tool_calls=[{"name": "execute", "id": "c", "args": {"command": "c", "network": True}}]),
            AIMessage(content="done"),
            AIMessage(content="still here"),
        ]))
        try:
            runner.set_permission_mode("auto")
            assert runner.invoke("run c").status == "completed"
            first = runner.thread_id
            runner.set_permission_mode("ask")
            assert runner.apply_pending_config_if_idle()
            runner.new_session()
            assert not runner.prepared.approval_guard
            old = runner.thread_id
            real = runner._rebuild_prepared

            def fail_when_mounting_guard(*, thread_id=..., **kwargs):
                if thread_id == first:
                    raise RuntimeError("guard rebuild failed")
                return real(thread_id=thread_id, **kwargs)

            monkeypatch.setattr(runner, "_rebuild_prepared", fail_when_mounting_guard)
            with pytest.raises(RuntimeError, match="guard rebuild failed"):
                runner.switch_session(first)
            assert runner.thread_id == old
            assert runner._runtime.lease is not None
            assert runner._runtime.lease.thread_id == old
            assert not runner.prepared.approval_guard
            freed = store.try_acquire_session(first)
            assert freed is not None
            freed.release()
            assert runner.invoke("still on the new session").status == "completed"
        finally:
            runner.close()
            store.close()


def test_thread_prefix_does_not_match_a_longer_thread_id():
    journal = ApprovalJournal(object())
    saver = type("Saver", (), {"_auto_approval_journal": journal})()
    journal.record(f"a:b:{'0' * 64}", Decision("allow", "authorized"), "policy")
    assert ApprovalJournal.has_thread(saver, "a:b")
    assert not ApprovalJournal.has_thread(saver, "a")


def test_create_agent_mounts_ask_guard_when_thread_id_has_auto_history(tmp_path):
    from agent.bootstrap.factory import create_agent

    path = str(tmp_path / "create-agent-guard.sqlite")
    thread = "thread-with-auto"
    with SqliteSaver.from_conn_string(path) as saver:
        journal = ApprovalJournal(saver)
        journal.record(f"{thread}:{'a' * 64}", Decision("allow", "authorized"), "policy")
        with patch("agent.bootstrap.factory.select_backend", return_value=BackendSelection(CountingBackend(), ExecutionMode.SANDBOXED)):
            guarded = create_agent(
                model=scripted_model([AIMessage(content="done")]),
                checkpointer=saver,
                permission_mode="ask",
                thread_id=thread,
                settings=Settings(resolved_deepseek_api_key="key"),
            )
            bare = create_agent(
                model=scripted_model([AIMessage(content="done")]),
                checkpointer=saver,
                permission_mode="ask",
                settings=Settings(resolved_deepseek_api_key="key"),
            )
        assert guarded.approval_guard and not bare.approval_guard


@pytest.mark.parametrize("change", ["sandbox", "mount"])
def test_restart_blocks_stale_grants_and_reviews_new_calls(tmp_path, monkeypatch, change):
    from agent.sessions.store import SessionStore

    mount = BindMount(source=tmp_path, destination="/shared")
    policy = SandboxConfig(workspace=tmp_path, extra_read_only_mounts=(mount,))
    changed = (replace(policy, mode="full-access") if change == "sandbox" else
               replace(policy, extra_read_only_mounts=(), extra_read_write_mounts=(mount,)))
    settings = Settings(resolved_deepseek_api_key="key", sandbox=policy)
    backend = CountingBackend()
    backend.calls = []
    reviews = []

    async def review(key, trust_env, evidence, cancelled):
        data = json.loads(evidence)
        command = data["action"]["args"]["command"]
        reviews.append((command, data["environment"]["sandbox"], data["environment"]["read_write_mounts"]))
        return Decision("ask" if command == "confirm" else "allow", "reviewed")

    def batch(*commands):
        return AIMessage(content="", tool_calls=[
            {"id": cmd, "name": "execute", "args": {"command": cmd, "network": True}} for cmd in commands
        ])

    monkeypatch.setattr("agent.runtime.auto_approval.classify", review)
    selection = BackendSelection(backend, ExecutionMode.SANDBOXED)
    with patch("agent.bootstrap.factory.select_backend", return_value=selection):
        db = tmp_path / "session.sqlite"
        store = SessionStore(db)
        runner = AgentRunner(session_store=store, settings=settings, model=scripted_model([batch("old", "confirm")]))
        try:
            thread = runner.thread_id
            runner.set_permission_mode("auto")
            pending = runner.invoke("review operations")
            assert [item["toolCallId"] for item in pending.pending_tool_calls] == ["confirm"]
        finally:
            runner.close()
            store.close()

        store = SessionStore(db)
        restored = AgentRunner(session_store=store, thread_id=thread, settings=replace(settings, sandbox=changed),
                               model=scripted_model([batch("new"), AIMessage(content="done")]))
        try:
            assert restored.approve_tool("confirm").status == "completed"
            messages = restored.prepared.graph.get_state(restored._thread_config()).values["messages"]
            blocked = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)
                       and m.status == "error" and "审批环境已变化" in m.content}
            assert blocked == {"old", "confirm"}
            assert backend.calls == ["new"]
            assert reviews == [("old", "restricted", []), ("confirm", "restricted", []),
                               ("new", changed.mode, ["/shared"] if change == "mount" else [])]
        finally:
            restored.close()
            store.close()


def test_batch_deadline_preserves_results_and_cleans_up(monkeypatch):
    from agent.runtime import auto_approval

    async def scenario():
        active = set()
        started = []
        full = asyncio.Event()

        async def review(key, trust_env, evidence, cancelled):
            active.add(evidence)
            started.append(evidence)
            assert len(active) <= 4
            if len(active) == 4:
                full.set()
            try:
                await full.wait()
                if evidence == "2":
                    return Decision("deny", "reviewed")
                await asyncio.Future()
            finally:
                active.remove(evidence)

        monkeypatch.setattr(auto_approval, "classify", review)
        monkeypatch.setattr(auto_approval, "_BATCH_TIMEOUT", 0.1)
        evidence = {str(i): str(i) for i in range(8)}
        decisions = await auto_approval._classify_batch(evidence, "key", False, lambda: False)
        assert [d.decision for d in decisions.values()] == ["ask", "ask", "deny"] + ["ask"] * 5
        assert started == list(evidence)[:5]
        assert not active

    asyncio.run(scenario())


def test_batch_cancellation_closes_siblings(monkeypatch):
    from agent.models.stream_lifecycle import ModelStreamCancelled
    from agent.runtime import auto_approval

    async def scenario():
        active = set()
        cancelled = asyncio.Event()

        async def review(key, trust_env, evidence, is_cancelled):
            active.add(evidence)
            try:
                if len(active) == 4:
                    cancelled.set()
                    raise ModelStreamCancelled("cancelled")
                await asyncio.Future()
            finally:
                active.remove(evidence)

        monkeypatch.setattr(auto_approval, "classify", review)
        with pytest.raises(ModelStreamCancelled):
            await auto_approval._classify_batch({i: i for i in range(8)}, "key", False, cancelled.is_set)
        assert not active

    asyncio.run(scenario())


@pytest.mark.parametrize(("response", "outcome"), [
    ({"decision": "allow", "reason": "authorized"}, "allow"),
    ({"decision": "block", "reason": "exfiltration"}, "deny"),
    ({"decision": "allow", "reason": "uncertain"}, "ask"),
    ({"decision": "ask", "reason": "uncertain"}, "ask"),
    ({}, "ask"),
    ("not json", "ask"),
    (None, "ask"),
])
def test_independent_classifier(monkeypatch, response, outcome):
    import httpx
    from agent.runtime.auto_approval import classify

    sent = []
    original = httpx.AsyncClient

    def respond(request):
        assert str(request.url) == "https://api.deepseek.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer unified"
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash"
        assert body["reasoning_effort"] == "none"
        assert body["messages"][1] == {"role": "user", "content": "example"}
        sent.append(body)
        if response is None:
            return httpx.Response(503)
        content = response if isinstance(response, str) else json.dumps(response)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    assert asyncio.run(classify("unified", False, "example", lambda: False)).decision == outcome
    # One request per call: an uncertain verdict is not re-asked.
    assert len(sent) == 1


def test_unexpected_classifier_error_goes_to_human(monkeypatch):
    import httpx
    from agent.runtime.auto_approval import classify

    def respond(request):
        raise RuntimeError("unexpected")

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    assert asyncio.run(classify("unified", False, "example", lambda: False)).decision == "ask"


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


def test_web_search_with_known_secret_is_statically_allowed(monkeypatch):
    reviews = []

    async def review(*args):
        reviews.append(True)
        return Decision("allow", "reviewed")

    monkeypatch.setattr("agent.runtime.auto_approval.classify", review)
    secret = "unit-key-value-for-web-allow"
    settings = Settings(resolved_deepseek_api_key=secret)
    model = scripted_model([
        AIMessage(content="", tool_calls=[{"name": "web_search", "id": "ok", "args": {
            "query": f"debug token={secret}"}}]),
        AIMessage(content="done"),
    ])
    with patch("agent.bootstrap.factory.select_backend", return_value=BackendSelection(StateBackend(), ExecutionMode.SANDBOXED)):
        runner = AgentRunner(model=model, settings=settings)
        runner.set_permission_mode("auto")
        assert runner.invoke("search JWT Bearer docs").status == "completed"
        assert reviews == []


def test_flash_evidence_redacts_config_keys_and_replacements(monkeypatch, tmp_path):
    from langchain_core.messages import HumanMessage

    from agent.bootstrap.factory import AgentSpec
    from agent.execution.layout import sandbox_environment
    from agent.models.profile import ModelProfile
    from agent.runtime.auto_approval import AutoApprovalMiddleware

    monkeypatch.setenv("APP_TOKEN", "allowlisted-token-value")
    profile_key = "profile-api-key-value-xyz"
    deepseek_key = "deepseek-api-key-value-xyz"
    env_set_secret = "env-set-secret-value"
    custom_literal = "sk-custom-literal-xyz"
    sandbox = SandboxConfig(
        workspace=tmp_path,
        env_set={"APP_SECRET": env_set_secret},
        env_allowlist=("APP_TOKEN", "PATH"),
    )
    settings = Settings(
        resolved_deepseek_api_key=deepseek_key,
        llm_profiles=(ModelProfile("local/mock", "mock", api_key=profile_key),),
        llm_default="local/mock",
        sandbox=sandbox,
        redaction_replacements=((custom_literal, "密钥"),),
    )
    with SqliteSaver.from_conn_string(str(tmp_path / "journal.sqlite")) as saver:
        middleware = AutoApprovalMiddleware(
            spec=AgentSpec(settings=settings, deepseek_api_key=deepseek_key, sandbox=sandbox),
            backend=object(),
            controller=object(),
            journal=ApprovalJournal(saver),
            gated=ask_interrupt_on(),
        )
        state = {"messages": [HumanMessage(content="run unit tests")]}
        call = {"name": "execute", "id": "1", "args": {
            "command": (
                f"echo {profile_key} {deepseek_key} {env_set_secret} {custom_literal} "
                f"allowlisted-token-value eyJ.test.jwt {sandbox_environment(sandbox)['PATH']}"
            ),
        }}
        environment, _ = middleware._policy()
        text = middleware.evidence(state, call, environment)
        assert text is not None
        assert profile_key not in text
        assert deepseek_key not in text
        assert custom_literal not in text
        # Sandbox / allowlisted env values are no longer auto-redacted.
        assert env_set_secret in text
        assert "allowlisted-token-value" in text
        assert "eyJ.test.jwt" in text
        assert "$llm.api_key" in text or "$llm.models" in text
        assert "密钥" in text
        assert "$APP_SECRET" not in text
        assert "$APP_TOKEN" not in text
        assert sandbox_environment(sandbox)["PATH"] in text


def test_env_set_change_blocks_stale_auto_grant(tmp_path, monkeypatch):
    from agent.sessions.store import SessionStore

    async def review(key, trust_env, evidence, cancelled):
        command = json.loads(evidence)["action"]["args"]["command"]
        return Decision("ask" if command == "confirm" else "allow", "reviewed")

    monkeypatch.setattr("agent.runtime.auto_approval.classify", review)
    policy = SandboxConfig(workspace=tmp_path, env_set={"TOKEN": "one"})
    changed = replace(policy, env_set={"TOKEN": "two"})
    settings = Settings(resolved_deepseek_api_key="key", sandbox=policy)
    backend = CountingBackend()
    backend.calls = []
    selection = BackendSelection(backend, ExecutionMode.SANDBOXED)
    with patch("agent.bootstrap.factory.select_backend", return_value=selection):
        db = tmp_path / "session.sqlite"
        store = SessionStore(db)
        runner = AgentRunner(session_store=store, settings=settings, model=scripted_model([
            AIMessage(content="", tool_calls=[
                {"id": "old", "name": "execute", "args": {"command": "old", "network": True}},
                {"id": "confirm", "name": "execute", "args": {"command": "confirm", "network": True}},
            ]),
        ]))
        try:
            thread = runner.thread_id
            runner.set_permission_mode("auto")
            pending = runner.invoke("review operations")
            assert [item["toolCallId"] for item in pending.pending_tool_calls] == ["confirm"]
        finally:
            runner.close()
            store.close()
        store = SessionStore(db)
        restored = AgentRunner(
            session_store=store, thread_id=thread, settings=replace(settings, sandbox=changed),
            model=scripted_model([
                AIMessage(content="", tool_calls=[{"id": "new", "name": "execute",
                                                  "args": {"command": "new", "network": True}}]),
                AIMessage(content="done"),
            ]),
        )
        try:
            assert restored.approve_tool("confirm").status == "completed"
            messages = restored.prepared.graph.get_state(restored._thread_config()).values["messages"]
            blocked = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)
                       and m.status == "error" and "审批环境已变化" in m.content}
            assert blocked == {"old", "confirm"}
            assert backend.calls == ["new"]
        finally:
            restored.close()
            store.close()


def test_async_graph_reviews_and_resumes_without_blocking_the_loop(monkeypatch):
    from agent.bootstrap.factory import create_agent
    from langgraph.types import Command

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        backend = CountingBackend()
        backend.calls = []

        async def review(*args):
            started.set()
            await release.wait()
            return Decision("ask", "manual")

        monkeypatch.setattr("agent.runtime.auto_approval.classify", review)
        selection = BackendSelection(backend, ExecutionMode.SANDBOXED)
        with patch("agent.bootstrap.factory.select_backend", return_value=selection):
            graph = create_agent(settings=Settings(resolved_deepseek_api_key="test"), permission_mode="auto",
                                 model=scripted_model([
                                     AIMessage(content="", tool_calls=[{"name": "execute", "id": "call",
                                                                        "args": {"command": "example", "network": True}}]),
                                     AIMessage(content="done"),
                                 ])).graph
        config = {"configurable": {"thread_id": "async-example"}}
        pending = asyncio.create_task(graph.ainvoke({"messages": [("user", "test")]}, config))
        await asyncio.wait_for(started.wait(), timeout=5)
        assert not pending.done()
        release.set()
        assert (await pending)["__interrupt__"]
        assert not backend.calls
        await graph.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
        assert backend.calls == ["example"]

    asyncio.run(scenario())
