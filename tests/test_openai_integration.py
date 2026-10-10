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

from agent.config import ModelProfile, Settings
from agent.runner import AgentRunner
from agent.session import SessionStore

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
    answers = [{"tool": "write_file", "id": "write-local", "args": {"file_path": "/note.txt", "content": "approved"}},
               {"text": "saved after approval"}]
    with compatible_endpoint(answers) as (endpoint, requests):
        settings = settings_for(endpoint)
        path = tmp_path / "state.sqlite3"
        store = SessionStore(path)
        runner = AgentRunner(settings=settings, backend=StateBackend(), session_store=store)
        waiting = runner.invoke("write a note")
        assert waiting.status == "waiting_confirmation"
        assert waiting.pending_tool_calls[0]["name"] == "write_file"
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
    answers = [{"tool": "write_file", "id": "pause-write", "args": {"file_path": "/paused.txt", "content": "once"}},
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
