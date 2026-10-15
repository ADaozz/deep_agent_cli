from concurrent.futures import ThreadPoolExecutor
import time

import pytest
from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage, ToolMessage

from agent.mcp.config import MCPConfig, MCPProfile
from agent.runner import AgentRunner
from tests.conftest import scripted_model
from tests.test_mcp_client import mcp_server


def call(name="remote_add", id="remote", args=None):
    return AIMessage(content="", tool_calls=[{"name": name, "id": id, "args": args or {"a": 2, "b": 3}}])


def test_runner_hitl_switch_lifecycle_and_rollback(mcp_server, tmp_path, monkeypatch, request):
    model = scripted_model([call(), AIMessage(content="done"), call(id="denied"), AIMessage(content="rejected")])
    runner = AgentRunner(model=model, backend=StateBackend())
    path = tmp_path / "mcp.yaml"
    runner.load_mcp(path)
    try:
        runner.add_mcp(mcp_server, activate=True)
        tools = runner._mcp_tools
        prepared = runner.prepared
        runner.switch_mcp("test")
        assert runner.prepared is prepared
        assert "remote_add" in runner.prepared.interrupt_on
        assert runner.invoke("add").status == "waiting_confirmation"
        state = runner.prepared.graph.get_state(runner._thread_config())
        assert not any(isinstance(m, ToolMessage) and m.name == "remote_add" for m in state.values["messages"])
        with pytest.raises(RuntimeError, match="pending"):
            runner.switch_mcp("none")
        assert runner.approve_tool("remote").status == "completed"
        result = runner.prepared.graph.get_state(runner._thread_config()).values["messages"]
        assert next(m for m in result if isinstance(m, ToolMessage) and m.name == "remote_add").artifact == {
            "structured_content": {"sum": 5}}
        runner._rebuild_prepared()
        assert runner._mcp_tools is tools
        assert "remote_add" in runner.prepared.exposed_tool_names
        assert runner.invoke("deny").status == "waiting_confirmation"
        assert runner.reject_tool("denied").status == "completed"
        assert request.node.mcp_server_log.read_text().count("REMOTE_CALL remote_add") == 1
        old = runner.prepared
        old_file = path.read_bytes()
        def fail(*args, **kwargs):
            raise OSError("save failed")
        monkeypatch.setattr(MCPConfig, "save", fail)
        with pytest.raises(OSError):
            runner.switch_mcp("none")
        assert runner.prepared is old and runner._mcp_tools is tools
        assert path.read_bytes() == old_file
        monkeypatch.undo()
        runner.switch_mcp("none")
        assert "remote_add" not in runner.prepared.exposed_tool_names
        assert "test" in runner.mcp_config.profiles
        assert not tools.bridge._thread.is_alive()
    finally:
        runner.close()


def test_sqlite_restore_new_and_resume_keep_tools(mcp_server, tmp_path):
    from agent.session import SessionStore
    store = SessionStore(tmp_path / "sessions.sqlite3")
    runner = AgentRunner(model=scripted_model([call()]), backend=StateBackend(), session_store=store)
    runner.load_mcp(tmp_path / "mcp.yaml")
    identifier = runner.thread_id
    try:
        runner.add_mcp(mcp_server, activate=True)
        snapshot = runner.mcp_config
        assert runner.invoke("checkpoint").status == "waiting_confirmation"
    finally:
        runner.close()
    resumed = AgentRunner(model=scripted_model([AIMessage(content="restored")]), backend=StateBackend(),
                          session_store=store, thread_id=identifier)
    try:
        resumed.load_mcp_snapshot(snapshot)
        assert resumed.approve_tool("remote").status == "completed"
        tools = resumed._mcp_tools
        resumed.new_session()
        assert resumed._mcp_tools is tools and resumed.mcp_config.current == "test"
        resumed.switch_session(identifier)
        assert resumed._mcp_tools is tools and resumed.mcp_config.current == "test"
        assert "remote_add" in resumed.prepared.exposed_tool_names
    finally:
        resumed.close()
        store.close()
    restarted = AgentRunner(model=scripted_model([AIMessage(content="done")]), backend=StateBackend())
    try:
        restarted.load_mcp(tmp_path / "mcp.yaml")
        assert restarted.mcp_config.current == "test"
        assert "remote_add" in restarted.prepared.exposed_tool_names
    finally:
        restarted.close()


def test_name_conflict_rolls_back(mcp_server, tmp_path):
    from langchain_core.tools import StructuredTool
    from agent.factory import create_agent
    custom = StructuredTool.from_function(lambda a: a, name="remote_add", description="Local tool")
    prepared = create_agent(model=scripted_model([AIMessage(content="done")]),
                            backend=StateBackend(), extra_tools=[custom])
    runner = AgentRunner(prepared=prepared)
    runner.load_mcp(tmp_path / "mcp.yaml")
    try:
        before = runner.prepared
        with pytest.raises(ValueError, match="conflict"):
            runner.add_mcp(mcp_server, activate=True)
        assert runner.prepared is before and runner.mcp_config.current == "none"
        assert not runner._mcp_tools
    finally:
        runner.close()


def test_http_service_cancel_uses_same_runner(mcp_server, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from agent.server.app import create_app
    from agent.server.config import ServerConfig
    from tests.test_server import AUTH, session, submit, wait
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = tmp_path / "mcp.yaml"
    MCPConfig("test", {"test": mcp_server}).save(path)
    config = ServerConfig({"one": workspace}, tmp_path / "model.yaml", tmp_path / "service", "service-secret",
                          mcp_config=path)
    config.model_config.write_text("llm:\n  default: local/test\n  models:\n    local:\n      models:\n        test: {}\n")
    # A file edit after startup must not change the service snapshot.
    MCPConfig().save(path)
    assert config.mcp_snapshot.current == "test"
    runners = []
    import agent.server.runtime as runtime
    original = runtime._runner_from_settings
    monkeypatch.setattr("agent.runner.build_chat_model", lambda *args, **kwargs: scripted_model([
        call("slow_tool", args={"delay": 60}), AIMessage(content="done")]))
    def record_runner(*args, **kwargs):
        runner = original(*args, **kwargs)
        runners.append(runner)
        return runner
    monkeypatch.setattr(runtime, "_runner_from_settings", record_runner)
    with TestClient(create_app(config)) as client:
        rid = submit(client, session(client), "slow")
        pending = wait(client, rid, "waiting_confirmation")
        assert pending["result"]["pending_tool_calls"][0]["mcp"]["profile"] == "test"
        response = client.post(f"/v1/runs/{rid}/approvals", headers=AUTH, json={
            "interrupt_id": pending["interrupt_id"],
            "decisions": [{"tool_call_id": "remote", "decision": "approve"}],
        })
        assert response.status_code == 202
        deadline = time.monotonic() + 5
        while not runners[0]._mcp_tools.bridge._pending and time.monotonic() < deadline:
            time.sleep(.01)
        assert runners[0]._mcp_tools.bridge._pending
        assert client.post(f"/v1/runs/{rid}/cancel", headers=AUTH).status_code == 202
        wait(client, rid, "cancelled")
    assert not runners[0]._mcp_tools.bridge._thread.is_alive()


def test_model_and_permission_switch_reuse_tools(mcp_server, tmp_path, monkeypatch):
    from agent.config import Settings, SandboxConfig
    from agent.permission import PermissionMode
    settings = Settings.from_mapping({"llm": {"default": "local/a", "models": {"local": {
        "models": {"a": {}, "b": {}}}}}})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), settings=settings,
                         sandbox_config=SandboxConfig(workspace=workspace))
    runner.load_mcp(tmp_path / "mcp.yaml")
    try:
        runner.add_mcp(mcp_server, activate=True)
        tools = runner._mcp_tools
        monkeypatch.setattr("agent.runner.build_chat_model", lambda *args, **kwargs: scripted_model([call(), AIMessage(content="done")]))
        runner.switch_model("local/b")
        runner._apply_pending_runtime_config()
        assert runner.current_model().id == "local/b"
        assert runner._mcp_tools is tools
        runner.set_permission_mode(PermissionMode.ALLOW)
        runner._apply_pending_runtime_config()
        assert "remote_add" not in runner.prepared.interrupt_on
        assert runner._mcp_tools is tools
        assert runner.invoke("auto approve").status == "completed"
        runner.set_permission_mode(PermissionMode.ASK)
        runner._apply_pending_runtime_config()
        assert "remote_add" in runner.prepared.interrupt_on
        assert runner._mcp_tools is tools
    finally:
        runner.close()


def test_connection_failure_preserves_agent(mcp_server, tmp_path):
    runner = AgentRunner(model=scripted_model([AIMessage(content="works")]), backend=StateBackend())
    runner.load_mcp(tmp_path / "mcp.yaml")
    try:
        runner.add_mcp(mcp_server, activate=True)
        before = runner.prepared
        with pytest.raises(Exception):
            runner.add_mcp(MCPProfile("offline", "http://127.0.0.1:1/mcp"), activate=True)
        assert runner.prepared is before
        assert runner.mcp_config.current == "test"
        assert "offline" not in runner.mcp_config.profiles
        assert runner.invoke("hello").output == "works"
    finally:
        runner.close()


def test_real_http_cancel_through_runner(mcp_server, tmp_path):
    runner = AgentRunner(model=scripted_model([call("slow_tool", args={"delay": 60}), AIMessage(content="done")]),
                         backend=StateBackend())
    runner.load_mcp(tmp_path / "mcp.yaml")
    try:
        runner.add_mcp(mcp_server, activate=True)
        assert runner.invoke("slow").status == "waiting_confirmation"
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(runner.approve_tool, "remote")
            deadline = time.monotonic() + 5
            while not runner._mcp_tools.bridge._pending and time.monotonic() < deadline:
                time.sleep(.01)
            assert runner._mcp_tools.bridge._pending
            runner.request_cancel()
            assert future.result(timeout=5).status == "cancelled"
        assert not runner._mcp_tools.bridge._pending
    finally:
        runner.close()
