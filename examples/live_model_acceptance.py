#!/usr/bin/env python3
"""Real-model acceptance for the cancellable model stream (TokenPlan or any compatible endpoint).

Per protocol (``api: chat_completions`` / ``responses``) it checks:

1. parity: the same requests through the bridged stream and through LangChain's
   original synchronous ``ChatOpenAI._stream`` — text, reasoning presence,
   streamed tool-call arguments and API-reported usage;
2. coding: a real Runner in the Bubblewrap sandbox writes and runs a script
   (tools approved programmatically in ask mode, sandbox network stays off);
   tokens come from the usage the API reported on each checkpointed answer;
3. cancel: a long answer is cancelled after its first delta and after 0.5s;
   the Runner must return cancelled with no live stream, and a simple task in
   the same session must then complete;
4. race: short answers with cancels swept across natural completion; every
   reported outcome must match the checkpoint and the session status.

The API key is read from the environment variable named by ``--key-env``
(default ``TOKEN_PLAN_API_KEY``) and never printed or written. ``--self-test``
runs the same flow against the local scripted endpoint to check the plumbing;
its numbers say nothing about a real model.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

from agent.config import ModelProfile, SandboxConfig, Settings  # noqa: E402
from agent.llm import build_chat_model  # noqa: E402
from agent.llm_stream import live_stream_count  # noqa: E402
from agent.permission import PermissionMode  # noqa: E402
from agent.runner import AgentRunner, RunEvent  # noqa: E402
from agent.session import SessionStore  # noqa: E402
from agent.stream import message_text, reasoning_text  # noqa: E402

TOKEN_PLAN = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
FIB = "0 1 1 2 3 5 8 13 21 34"
CODING_TASK = ("In the workspace, create fib.py that prints the first 10 Fibonacci numbers starting "
               "with 0, separated by single spaces on one line. Run it with python3 and reply with "
               "exactly the line it printed.")
ESSAY_TASK = "Write a detailed essay of about 1500 words on the history of compilers. Do not use tools."


@tool
def save_note(title: str, body: str) -> str:
    """Save a note with a title and a body."""
    return "saved"


def usage_of(message: Any) -> dict[str, int]:
    usage = getattr(message, "usage_metadata", None) or {}
    return {key: int(usage.get(key) or 0) for key in ("input_tokens", "output_tokens", "total_tokens")}


# -- 1. parity ---------------------------------------------------------------------------
def collect(model: Any, messages: list[Any]) -> dict[str, Any]:
    started = time.monotonic()
    chunks = 0
    merged: AIMessageChunk | None = None
    for chunk in model.stream(messages):
        chunks += 1
        merged = chunk if merged is None else merged + chunk
    assert merged is not None
    calls = []
    for call in merged.tool_calls:
        calls.append({"name": call["name"], "args_keys": sorted(call["args"]), "args_valid": isinstance(call["args"], dict)})
    return {"chunks": chunks, "seconds": round(time.monotonic() - started, 3), "text": message_text(merged)[:200],
            "reasoning_chars": len(reasoning_text(merged)), "tool_calls": calls,
            "invalid_tool_calls": len(merged.invalid_tool_calls), "usage": usage_of(merged)}


def parity(profile: ModelProfile) -> dict[str, Any]:
    model = build_chat_model(profile)
    cases = {
        "text": ([HumanMessage("Reply with exactly: pong")], model),
        # qwen3.8-flash thinking mode rejects tool_choice=required/object.
        "tool": ([HumanMessage(
            "Use save_note to save a note titled groceries whose body is milk eggs. Do not reply with text.")],
                 model.bind_tools([save_note])),
    }
    report: dict[str, Any] = {}
    for name, (messages, runnable) in cases.items():
        bridged = collect(runnable, messages)
        with mock.patch.object(type(model), "_stream", ChatOpenAI._stream):
            original = collect(runnable, messages)
        same = {
            "usage_reported": all(r["usage"]["input_tokens"] > 0 and r["usage"]["output_tokens"] > 0
                                  for r in (bridged, original)),
            "reasoning_presence": (bridged["reasoning_chars"] > 0) == (original["reasoning_chars"] > 0),
            "tool_calls": [(c["name"], c["args_keys"]) for c in bridged["tool_calls"]]
            == [(c["name"], c["args_keys"]) for c in original["tool_calls"]],
            "no_invalid_tool_calls": bridged["invalid_tool_calls"] == original["invalid_tool_calls"] == 0,
        }
        if name == "tool":
            same["tool_args_complete"] = bridged["tool_calls"][:1] and bridged["tool_calls"][0]["args_keys"] == ["body", "title"]
        report[name] = {"bridged": bridged, "original_sync": original, "checks": same,
                        "pass": all(bool(v) for v in same.values())}
    report["pass"] = all(case["pass"] for case in report.values())
    return report


# -- Runner helpers ------------------------------------------------------------------------
class Session:
    def __init__(self, profile: ModelProfile, root: Path, name: str) -> None:
        self.workspace = root / f"{name}-workspace"
        self.workspace.mkdir()
        self.store = SessionStore(root / f"{name}.sqlite3")
        settings = Settings(llm_profiles=(profile,), llm_default=profile.id)
        self.runner = AgentRunner(settings=settings, sandbox_config=SandboxConfig(workspace=self.workspace),
                                  session_store=self.store, thread_id=name, workspace=self.workspace)
        self.runner.set_permission_mode(PermissionMode.ASK)

    def messages(self) -> list[Any]:
        return self.runner.prepared.graph.get_state(self.runner._thread_config()).values.get("messages") or []

    def status(self) -> str:
        return self.store.get(self.runner.thread_id).last_run_status.value

    def run(self, text: str, events: list[RunEvent] | None = None, approve: bool = True) -> Any:
        sink = events if events is not None else []
        result = self.runner.invoke(text, on_event=sink.append)
        while approve and result.status == "waiting_confirmation":
            result = self.runner.decide_tools(
                [{"tool_call_id": call["toolCallId"], "decision": "approve"} for call in result.pending_tool_calls],
                on_event=sink.append)
        return result

    def close(self) -> None:
        try:
            self.runner.close()
        finally:
            self.store.close()


def answers_since(messages: list[Any], prompt: str) -> list[AIMessage]:
    start = max((i for i, m in enumerate(messages) if message_text(m) == prompt), default=len(messages))
    return [m for m in messages[start:] if isinstance(m, AIMessage)]


# -- 2. coding -----------------------------------------------------------------------------
def coding(profile: ModelProfile, root: Path) -> dict[str, Any]:
    session = Session(profile, root, "coding")
    try:
        events: list[RunEvent] = []
        started = time.monotonic()
        result = session.run(CODING_TASK, events)
        elapsed = time.monotonic() - started
        answers = answers_since(session.messages(), CODING_TASK)
        script = session.workspace / "fib.py"
        printed = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30).stdout.strip() \
            if script.exists() else None
        tools = list({e.tool_call_id: e.name for e in events if e.type == "tool_started"}.values())
        totals = {k: sum(usage_of(m)[k] for m in answers) for k in ("input_tokens", "output_tokens", "total_tokens")}
        return {"status": result.status, "elapsed_s": round(elapsed, 2), "model_requests": len(answers),
                "tokens": totals, "tools": tools, "script_output": printed,
                "reply_has_output": FIB in (result.output or ""),
                "pass": result.status == "completed" and printed == FIB and FIB in (result.output or "")}
    finally:
        session.close()


# -- 3. cancel -----------------------------------------------------------------------------
def cancel(profile: ModelProfile, root: Path) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for variant, delay in (("first_delta", 0.0), ("after_0.5s", 0.5)):
        session = Session(profile, root, f"cancel-{variant}")
        try:
            events: list[RunEvent] = []
            first_delta = threading.Event()
            holder: dict[str, Any] = {}

            def sink(event: RunEvent) -> None:
                events.append(event)
                if event.type in {"assistant_delta", "thinking_delta"}:
                    first_delta.set()

            worker = threading.Thread(target=lambda: holder.setdefault("r", session.runner.invoke(ESSAY_TASK, on_event=sink)))
            worker.start()
            got_delta = first_delta.wait(120)
            time.sleep(delay)
            requested = time.monotonic()
            session.runner.request_cancel()
            worker.join(30)
            stopped = time.monotonic() - requested
            result = holder.get("r")
            kinds = [e.type for e in events]
            essay_answered = bool(answers_since(session.messages(), ESSAY_TASK))
            status_after_cancel = session.status()
            follow_events: list[RunEvent] = []
            follow = session.run("Reply with exactly: ok", follow_events) if result is not None else None
            report[variant] = {
                "saw_delta": got_delta, "cancel_to_return_s": round(stopped, 3),
                "status": getattr(result, "status", None), "error": getattr(result, "error", None),
                "live_streams_after": live_stream_count(), "run_cancelled_events": kinds.count("run_cancelled"),
                "session_status_after_cancel": status_after_cancel, "partial_answer_saved": essay_answered,
                "follow_up_status": getattr(follow, "status", None),
                "follow_up_output": (getattr(follow, "output", "") or "")[:40],
                "prompt_kept": any(message_text(m) == ESSAY_TASK for m in session.messages()),
            }
            item = report[variant]
            item["pass"] = (got_delta and item["status"] == "cancelled" and not item["error"]
                            and stopped < 5 and item["live_streams_after"] == 0 and item["run_cancelled_events"] == 1
                            and status_after_cancel == "aborted" and not essay_answered
                            and item["follow_up_status"] == "completed" and item["prompt_kept"])
        finally:
            session.close()
    report["pass"] = all(item["pass"] for item in report.values())
    return report


# -- 4. race -------------------------------------------------------------------------------
def race(profile: ModelProfile, root: Path, rounds: int, step: float) -> dict[str, Any]:
    # A fresh session per round so earlier cancelled prompts do not inflate later latency.
    outcomes = []
    for index in range(rounds):
        session = Session(profile, root, f"race-{index}")
        try:
            prompt = f"Reply with exactly: r{index}"
            holder: dict[str, Any] = {}
            started = threading.Event()

            def sink(event: RunEvent) -> None:
                if event.type == "run_started":
                    started.set()

            worker = threading.Thread(target=lambda: holder.setdefault("r", session.runner.invoke(prompt, on_event=sink)))
            worker.start()
            # A cancel before the run began is not carried into it (library API).
            started.wait(30)
            time.sleep(step * index)
            session.runner.request_cancel()
            worker.join(60)
            result = holder["r"]
            answered = bool(answers_since(session.messages(), prompt))
            status = session.status()
            consistent = ((result.status == "completed" and answered and status == "stop")
                          or (result.status == "cancelled" and not answered
                              and status in {"aborted", "pending"}))
            outcomes.append({"delay_s": round(step * index, 2), "status": result.status, "answered": answered,
                             "session_status": status, "consistent": consistent})
        finally:
            session.close()
    return {"rounds": outcomes, "pass": all(o["consistent"] for o in outcomes),
            "both_outcomes_seen": {"completed", "cancelled"} <= {o["status"] for o in outcomes}}


def http_race(profile: ModelProfile, root: Path, rounds: int, step: float) -> dict[str, Any]:
    """Cancel vs completion through the HTTP service, using a real Runner."""
    pytest = __import__("pytest")
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from agent.server.app import create_app
    from agent.server.config import ServerConfig
    from agent.server.store import TERMINAL
    from agent.stream import message_text as text_of

    project = root / "http-project"
    project.mkdir()
    dummy = root / "unused-model.yaml"
    dummy.write_text("llm: {default: x, models: {x: {models: {x: {}}}}}\n")
    config = ServerConfig({"one": project}, dummy, root / "http-state", "service-secret")
    settings = Settings(llm_profiles=(profile,), llm_default=profile.id)
    database = config.state_dir / "one.sqlite3"

    def factory(_config, project_id, session_id):
        return AgentRunner(settings=settings, session_store=SessionStore(database), thread_id=session_id,
                           workspace=project)

    auth = {"Authorization": "Bearer service-secret"}
    outcomes = []
    with TestClient(create_app(config, factory=factory)) as client:
        for index in range(rounds):
            session = client.post("/v1/sessions", json={"project_id": "one"}, headers=auth)
            assert session.status_code == 201, session.text
            sid = session.json()["id"]
            prompt = f"Reply with exactly: h{index}"
            created = client.post(f"/v1/sessions/{sid}/runs", json={"text": prompt}, headers=auth)
            assert created.status_code == 202, created.text
            rid = created.json()["id"]
            time.sleep(step * index)
            cancel = client.post(f"/v1/runs/{rid}/cancel", headers=auth)
            deadline = time.monotonic() + 90
            run = None
            while time.monotonic() < deadline:
                run = client.get(f"/v1/runs/{rid}", headers=auth).json()
                if run["status"] in TERMINAL:
                    break
                time.sleep(0.05)
            assert run is not None and run["status"] in TERMINAL, run
            store = SessionStore(database)
            reader = AgentRunner(settings=settings, session_store=store, thread_id=sid, workspace=project)
            try:
                messages = (reader.prepared.graph.get_state(reader._thread_config()).values or {}).get("messages") or []
                catalog = store.get(sid).last_run_status.value
            finally:
                reader.close()
                store.close()
            start = max((i for i, m in enumerate(messages) if text_of(m) == prompt), default=len(messages))
            answered = any(isinstance(m, AIMessage) and text_of(m).strip() for m in messages[start:])
            consistent = ((run["status"] == "completed" and answered and catalog == "stop")
                          or (run["status"] == "cancelled" and not answered
                              and catalog in {"aborted", "pending"})
                          or (run["status"] == "failed" and bool(run.get("result", {}).get("error"))))
            outcomes.append({
                "delay_s": round(step * index, 2), "http_status": run["status"],
                "cancel_http": cancel.status_code, "answered": answered, "session_status": catalog,
                "consistent": consistent, "error": (run.get("result") or {}).get("error") or "",
            })
    return {"rounds": outcomes, "pass": all(o["consistent"] for o in outcomes),
            "both_outcomes_seen": {"completed", "cancelled"} <= {o["http_status"] for o in outcomes}}


# -- self-test endpoint ----------------------------------------------------------------------
def self_test_server(api: str, selected: set[str]):
    from tests.model_stream_server import ScriptedSSEServer, Turn

    write = {"id": "w1", "name": "write_file",
             "args": {"file_path": "/workspace/fib.py", "content": "a, b = 0, 1\nout = []\nfor _ in range(10):\n"
                      "    out.append(str(a))\n    a, b = b, a + b\nprint(' '.join(out))\n"}}
    run = {"id": "e1", "name": "execute", "args": {"command": "python3 fib.py"}}
    note = {"id": "n1", "name": "save_note", "args": {"title": "groceries", "body": "milk, eggs"}}
    turns = []
    if "parity" in selected:
        turns += [Turn(text="pong"), Turn(text="pong"), Turn(tool_calls=[note]), Turn(tool_calls=[note])]
    if "coding" in selected:
        turns += [Turn(tool_calls=[write]), Turn(tool_calls=[run]), Turn(text=FIB)]
    if "cancel" in selected:
        for _ in range(2):
            turns += [Turn(text="essay " * 400, interval=0.01), Turn(text="ok")]
    if "race" in selected:
        turns += [Turn(text=f"r{i} done", interval=0.02) for i in range(6)]
    return ScriptedSSEServer(api, turns)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", choices=("chat_completions", "responses", "both"), default="both")
    parser.add_argument("--base-url", default=TOKEN_PLAN)
    parser.add_argument("--model", default="qwen3.8-flash")
    parser.add_argument("--key-env", default="TOKEN_PLAN_API_KEY")
    parser.add_argument("--race-rounds", type=int, default=6)
    parser.add_argument("--race-step", type=float, default=0.4, help="seconds added to each round's cancel delay")
    parser.add_argument("--only", default="parity,coding,cancel,race,http")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    apis = ("chat_completions", "responses") if args.api == "both" else (args.api,)
    selected = set(args.only.split(","))
    if args.self_test:
        selected.discard("http")
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    result: dict[str, Any] = {"commit": commit, "model": args.model, "self_test": args.self_test,
                              "endpoint": "local scripted" if args.self_test else args.base_url, "apis": {}}
    for api in apis:
        server = self_test_server(api, selected).__enter__() if args.self_test else None
        try:
            if server is not None:
                key, base_url = "local-key", server.base_url
            else:
                key = os.environ.get(args.key_env)
                if not key:
                    raise SystemExit(f"Set {args.key_env} (it is read from the environment only).")
                base_url = args.base_url
            profile = ModelProfile(f"live/{args.model}", args.model, api_key=key, base_url=base_url, api=api)
            section: dict[str, Any] = {}
            with tempfile.TemporaryDirectory(prefix="live-acceptance-") as tmp:
                root = Path(tmp)
                for name, run in (("parity", lambda: parity(profile)), ("coding", lambda: coding(profile, root)),
                                  ("cancel", lambda: cancel(profile, root)),
                                  ("race", lambda: race(profile, root, args.race_rounds, args.race_step)),
                                  ("http", lambda: http_race(profile, root, args.race_rounds, args.race_step))):
                    if name not in selected:
                        continue
                    started = time.monotonic()
                    try:
                        section[name] = run()
                    except Exception as exc:  # noqa: BLE001
                        section[name] = {"pass": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
                    section[name]["seconds"] = round(time.monotonic() - started, 1)
                    print(json.dumps({"api": api, name: section[name]}, ensure_ascii=False), flush=True)
            result["apis"][api] = section
        finally:
            if server is not None:
                server.close()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if key_value := os.environ.get(args.key_env):
        assert key_value not in text
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
    summary = {api: {name: section.get("pass") for name, section in sections.items()}
               for api, sections in result["apis"].items()}
    print(json.dumps({"summary": summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
