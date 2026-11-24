"""Local HTTP integration: real SDK/streaming/graph/checkpoints, no external API."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import pytest
from deepagents.backends import StateBackend

from agent.models.profile import ModelProfile
from agent.bootstrap.config import Settings
from agent.runtime.runner import AgentRunner
from agent.sessions.store import SessionStore

pytestmark = pytest.mark.integration


@contextmanager
def compatible_endpoint(responses):
    pending = deque(responses)
    requests = []
    errors = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            if self.path != "/v1/chat/completions" or not pending:
                errors.append((self.path, body))
                self.send_error(500, "Unexpected model request")
                return
            answer = pending.popleft()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            delta = {"role": "assistant"}
            if "tool" in answer:
                delta["tool_calls"] = [{"index": 0, "id": answer["id"], "type": "function",
                                        "function": {"name": answer["tool"], "arguments": json.dumps(answer["args"])}}]
            else:
                delta["content"] = answer["text"]
            for payload in [
                {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if "tool" in answer else "stop"}],
                 "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}},
            ]:
                payload.update(id="chatcmpl-local", object="chat.completion.chunk", created=0, model=body["model"])
                self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
        assert not errors, errors
        assert not pending, "Scripted responses were not consumed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def settings_for(endpoint):
    return Settings(llm_profiles=(ModelProfile("local/mock", "mock-coder", api_key="local-test-key",
                    base_url=endpoint, api="chat_completions", reasoning_efforts=("low",)),), llm_default="local/mock")


def test_local_model_stream_and_effort_mapping():
    with compatible_endpoint([{"text": "local response"}, {"text": "low response"}]) as (endpoint, requests):
        runner = AgentRunner(settings=settings_for(endpoint), backend=StateBackend())
        try:
            deltas = []
            result = runner.invoke("hello", on_delta=lambda *args: deltas.append(args))
            assert result.status == "completed" and result.output == "local response"
            assert deltas
            assert "reasoning_effort" not in requests[0]
            runner.request_model_change("local/mock", reasoning_effort="low")
            assert runner.invoke("again").output == "low response"
            assert requests[1]["reasoning_effort"] == "low"
        finally:
            runner.close()


def test_http_tool_approval_survives_process_restart(tmp_path: Path):
    answers = [{"tool": "write", "id": "write-local", "args": {"file_path": "/note.txt", "content": "approved"}},
               {"text": "saved after approval"}]
    with compatible_endpoint(answers) as (endpoint, requests):
        settings = settings_for(endpoint)
        path = tmp_path / "state.sqlite3"
        store = SessionStore(path)
        runner = AgentRunner(settings=settings, backend=StateBackend(), session_store=store)
        waiting = runner.invoke("write a note")
        assert waiting.status == "waiting_confirmation"
        assert waiting.pending_tool_calls[0]["name"] == "write"
        assert len(requests) == 1
        thread_id = runner.thread_id
        runner.close(); store.close()
        store = SessionStore(path)
        runner = AgentRunner(settings=settings, backend=StateBackend(), session_store=store, thread_id=thread_id)
        try:
            result = runner.approve_tool("write-local")
            assert result.status == "completed" and result.output == "saved after approval"
            messages = requests[1]["messages"]
            assert any(message["role"] == "tool" and message["tool_call_id"] == "write-local" for message in messages)
            files = runner.prepared.graph.get_state({"configurable": {"thread_id": thread_id}}).values["files"]
            assert files["/note.txt"]["content"] == "approved"
        finally:
            runner.close(); store.close()


def test_http_pause_then_resume_without_repeating_tool():
    answers = [{"tool": "write", "id": "pause-write", "args": {"file_path": "/paused.txt", "content": "once"}},
               {"text": "continued"}]
    with compatible_endpoint(answers) as (endpoint, requests):
        runner = AgentRunner(settings=settings_for(endpoint), backend=StateBackend())
        try:
            assert runner.invoke("write").status == "waiting_confirmation"
            runner.request_pause()
            paused = runner.approve_tool("pause-write")
            assert paused.status == "paused"
            assert len(requests) == 1
            result = runner.continue_run()
            assert result.status == "completed" and result.output == "continued"
            assert sum(message["role"] == "tool" for message in requests[1]["messages"]) == 1
        finally:
            runner.close()


def test_lain_diagnoses_actual_execution_failures_through_worker_ipc(tmp_path, monkeypatch):
    import asyncio
    import os
    import shlex
    import subprocess
    import sys

    from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol
    from langchain_core.messages import AIMessage
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from agent.cli.app import CliApplication
    from tests.conftest import scripted_model

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    blocked = workspace / "not-executable"
    blocked.write_text("fixture")
    blocked.chmod(0o600)
    (workspace / "test_lain_fixture.py").write_text(
        "def test_fixture_comparison():\n    assert False, 'fixture comparison failed'\n",
    )
    canary = "opaque-companion-integration-secret"
    from agent.bootstrap.config import Settings

    cases = {
        "dependency": ["-c", "import _lain_missing_fixture_"],
        "assertion": ["-m", "pytest", "-q", "test_lain_fixture.py"],
        "permission": ["-c", f"import subprocess; subprocess.run([{str(blocked)!r}], check=True)"],
        "success": ["-c", f"print('fixture success {canary}')"],
    }
    commands = {key: shlex.join([sys.executable, *args]) for key, args in cases.items()}
    release = threading.Event()
    running = threading.Event()

    class FixtureBackend(StateBackend, SandboxBackendProtocol):
        id = "lain-integration-fixture"

        def execute(self, command, *, timeout=None):
            assert command in commands.values()
            if command == commands["dependency"]:
                running.set()
                assert release.wait(5)
            result = subprocess.run(
                shlex.split(command), cwd=workspace, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, timeout=timeout or 5, check=False,
            )
            return ExecuteResponse(result.stdout, result.returncode, False)

    explanation = "先后出现缺少模块、测试断言和权限错误，最后一次命令成功。"
    answers = [
        {"tool": "session_evidence", "id": "evidence", "args": {"kind": "sequence", "tool_name": "execute"}},
        *({"tool": "inspect_current_session", "id": "detail-" + case,
           "args": {"view": "tool_detail", "tool_call_id": case}} for case in cases),
        {"text": explanation},
    ]

    async def scenario(endpoint, requests):
        launch = """
import asyncio, sys
from dataclasses import replace
from agent.companion import worker
endpoint = sys.argv.pop(1)
build = worker.build_chat_model
worker.build_chat_model = lambda profile, **kwargs: build(replace(profile, base_url=endpoint), **kwargs)
asyncio.run(worker.main())
"""

        async def start_worker(directory, config, api_key):
            return await asyncio.create_subprocess_exec(
                sys.executable, "-c", launch, endpoint, str(directory),
                str(config.persona_prompt), str(config.chat_prompt),
                str(config.rpc_timeout_seconds), str(config.question_timeout_seconds),
                env={"PATH": os.environ["PATH"], "DEEPSEEK_API_KEY": api_key},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )

        monkeypatch.setattr("agent.companion.controller.start_worker", start_worker)
        store = SessionStore(tmp_path / "sessions.sqlite3")
        model = scripted_model([
            AIMessage(content="", tool_calls=[
                {"name": "execute", "id": case, "args": {"command": command}}
                for case, command in commands.items()
            ]),
            AIMessage(content="Main agent completed the fixture"),
        ])
        runner = AgentRunner(model=model, backend=FixtureBackend(), session_store=store, settings=Settings(resolved_deepseek_api_key=canary))
        try:
            with create_pipe_input() as pipe:
                app = CliApplication(runner, config_dir=tmp_path / "config", input=pipe, output=DummyOutput())
                app.run_view.loop = asyncio.get_running_loop()
                companion = app.companion

                async def inspect(**args):
                    return json.loads(await companion.host_query("inspect_current_session", args))

                waiting = await asyncio.to_thread(runner.invoke, "Run the fixture commands.")
                assert waiting.status == "waiting_confirmation"
                pending_rows = (await inspect())["tools"]
                assert len(pending_rows) == len(cases)
                assert {row["status"] for row in pending_rows} == {"waiting"}

                decisions = [{"tool_call_id": case, "decision": "approve"} for case in cases]
                approved = asyncio.create_task(asyncio.to_thread(runner.decide_tools, decisions))
                try:
                    assert await asyncio.to_thread(running.wait, 4)
                    live = await inspect(view="tool_detail", tool_call_id="dependency")
                    assert live.get("status") == "running", live
                    assert live["run_id"] == pending_rows[0]["run_id"]
                finally:
                    release.set()
                    completed = await approved
                assert completed.status == "completed"
                rows = (await inspect())["tools"]
                assert len(rows) == len(cases)
                assert [row["tool_call_id"] for row in rows] == list(cases)
                assert [row["error_category"] for row in rows] == ["dependency", "assertion", "permission", ""]
                await companion.save_statistics()
                companion.ask("解释 execute 的失败变化。")
                worker = asyncio.create_task(companion.worker_session())
                try:
                    async with asyncio.timeout(15):
                        while companion.question:
                            await asyncio.sleep(.02)
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                assert [message for _, role, message in companion.logs if role == "Lain"] == [explanation]
                runner.new_session()
                missing = await inspect(view="tool_detail", tool_call_id="success")
                assert missing["error"]["code"] == "tool_call_not_found"
                assert not companion.call_cache
                await companion.finish()
        finally:
            runner.close()
            store.close()

        final_request = requests[-1]
        assert all(item.get("tools") for item in requests)
        assert {item["function"]["name"] for item in final_request["tools"]} == {
            "session_evidence", "inspect_current_session", "search_workspace_text",
            "list_workspace_files", "read_workspace_file",
        }
        evidence, *details = [
            json.loads(message["content"])
            for message in final_request["messages"] if message["role"] == "tool"
        ]
        assert [row["tool_call_id"] for row in evidence["records"]] == list(cases)
        assert {detail["tool_call_id"]: detail["result"]["error_category"] for detail in details} == {
            "dependency": "dependency", "assertion": "assertion", "permission": "permission", "success": "",
        }
        for detail in details:
            assert detail["arguments"]["command"]
            assert detail["result"]["output_excerpt"]
        assert "fixture success" in details[-1]["result"]["output_excerpt"]
        assert canary not in json.dumps(requests, ensure_ascii=False)

    with compatible_endpoint(answers) as (endpoint, requests):
        asyncio.run(scenario(endpoint, requests))
