#!/usr/bin/env python3
"""Silent model stream fault injection against the real ``deep-agent`` TUI or HTTP service.

The scripted model endpoint (``tests.model_stream_server``) runs in this process;
the agent runs as a separate ``deep-agent`` process built from ``--repo``, so
the same driver can exercise a baseline checkout and the fixed one. Each cycle:

1. submits a task whose model stream goes silent (before the first chunk, mid
   stream, or keeps streaming),
2. cancels it (Esc in the PTY, or ``POST /runs/{id}/cancel``),
3. samples process threads, model TCP connections, the scripted server's open
   requests, session lease and run state at the requested offsets,
4. submits a simple task in the same session and times it.

It also runs a few normal tasks first, so request counts, request bytes,
reported token usage and elapsed time can be compared between checkouts.
Results are diagnostic only and are written as JSON.

Needs ``pyte`` (renders the PTY screen); ``py-spy`` is optional and adds a
Python thread dump before cancel and at the last sample. Neither is a project
dependency.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pyte  # noqa: E402  (diagnostic-only dependency: pip install pyte)

from tests.model_stream_server import ScriptedSSEServer, Turn  # noqa: E402

SCENARIOS = ("before_first_chunk", "mid_stream", "continuous")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-B]|\x1b[=>78]")
TOKEN = "fault-injection-token"


def stalled_turn(scenario: str) -> Turn:
    if scenario == "before_first_chunk":
        return Turn(text="never", mode="hang")
    if scenario == "mid_stream":
        return Turn(text="partial answer that never finishes", mode="hang", chunks_before_hang=2)
    return Turn(text="x", mode="forever", interval=0.05)


def answer_turn(marker: str) -> Turn:
    return Turn(text=f"{marker} done")


# -- process observation ---------------------------------------------------------------
def thread_names(pid: int) -> list[str]:
    names = []
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            names.append((task / "comm").read_text().strip())
        except OSError:
            pass
    return sorted(names)


def python_threads(pid: int) -> dict[str, Any] | None:
    """Python thread names and how many stacks sit in httpx/httpcore (needs py-spy)."""
    spy = shutil.which("py-spy") or str(Path(sys.executable).with_name("py-spy"))
    if not Path(spy).exists():
        return None
    try:
        text = subprocess.run([spy, "dump", "--pid", str(pid)], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    threads: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        match = re.match(r'Thread \S+ \((\w+)\): "(.*)"', line)
        if match:
            threads.append((match.group(2), []))
        elif threads and line.startswith("    "):
            threads[-1][1].append(line.strip())
    names = sorted(name for name, _ in threads)
    return {
        "names": {name: names.count(name) for name in sorted(set(names))},
        "in_http_client": sorted(name for name, frames in threads
                                 if any("httpcore" in f or "httpx" in f for f in frames)),
    }


def descendants(pid: int) -> list[int]:
    result, frontier = [], [pid]
    while frontier:
        parent = frontier.pop()
        for child in Path(f"/proc/{parent}/task/{parent}/children").read_text().split() \
                if Path(f"/proc/{parent}/task/{parent}/children").exists() else []:
            result.append(int(child))
            frontier.append(int(child))
    return result


def socket_inodes(pid: int) -> set[str]:
    inodes = set()
    for item in Path(f"/proc/{pid}/fd").iterdir():
        try:
            target = os.readlink(item)
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[8:-1])
    return inodes


def model_connections(pid: int, port: int) -> int:
    """Established TCP sockets owned by ``pid`` whose remote port is the model port."""
    inodes = socket_inodes(pid)
    count = 0
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            remote, state, inode = fields[2], fields[3], fields[9]
            if inode in inodes and state == "01" and int(remote.rsplit(":", 1)[1], 16) == port:
                count += 1
    return count


def held_leases(lock_dir: Path) -> int:
    """Number of session lock files some process currently holds."""
    held = 0
    for path in sorted(lock_dir.glob("*.lock")) if lock_dir.is_dir() else []:
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            held += 1
        finally:
            os.close(fd)
    return held


def catalog_status(database: Path) -> dict[str, str]:
    if not database.exists():
        return {}
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
    try:
        return dict(connection.execute("SELECT id, last_run_status FROM session_catalog"))
    except sqlite3.Error:
        return {}
    finally:
        connection.close()


def wait_for(predicate: Callable[[], Any], timeout: float, step: float = 0.02) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(step)


# -- agent processes ---------------------------------------------------------------------
def child_env(repo: Path, home: Path, extra: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI", "LANGCHAIN"))}
    env.update(HOME=str(home), PYTHONPATH=str(repo), PYTHONUNBUFFERED="1",
               TERM="xterm-256color", NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    env.update(extra)
    return env


def model_yaml(base_url: str, api: str, workspace: Path, state: Path | None, config_dir: Path) -> str:
    paths = f"paths:\n  config_dir: {config_dir}\n" + (f"  state_path: {state}\n" if state else "")
    return f"""llm:
  default: local/stream
  models:
    local:
      api_key: local-key
      base_url: {base_url}
      api: {api}
      models:
        stream: {{}}
sandbox:
  workspace: {workspace}
  allow_unsandboxed: false
{paths}"""


class PtyAgent:
    def __init__(self, repo: Path, env: dict[str, str], cwd: Path) -> None:
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
        self.process = subprocess.Popen(
            [sys.executable, "-c", "from agent.cli.main import main; main()"],
            cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, close_fds=True,
        )
        os.close(slave)
        self.output = ""
        self.screen = pyte.Screen(140, 40)
        self._stream = pyte.ByteStream(self.screen)
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._pump, name="pty-reader", daemon=True)
        self._reader.start()

    @property
    def pid(self) -> int:
        return self.process.pid

    def _pump(self) -> None:
        # Drain continuously so the TUI never blocks writing to a full PTY.
        while True:
            try:
                ready = select.select([self.master], [], [], 0.05)[0]
                data = os.read(self.master, 65536) if ready else None
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):
                    return
                raise
            if data == b"":
                return
            if data:
                with self._lock:
                    self._stream.feed(data)
                    self.output += ANSI.sub("", data.decode("utf-8", "replace"))

    def send(self, text: str) -> None:
        os.write(self.master, text.encode())

    def mark(self) -> int:
        with self._lock:
            return len(self.output)

    def display(self) -> str:
        with self._lock:
            return "\n".join(self.screen.display)

    def seen(self, needle: str, since: int) -> bool:
        """Whether ``needle`` is on the rendered screen or was written after ``since``."""
        with self._lock:
            rendered = "\n".join(self.screen.display)
            raw = self.output[since:]
        return needle in rendered or re.sub(r"\s", "", needle) in re.sub(r"\s", "", raw)

    def close(self) -> int | None:
        if self.process.poll() is None:
            self.send("\x03")
            time.sleep(0.2)
            self.send("\x03")
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(5)
        self._reader.join(2)
        os.close(self.master)
        return self.process.returncode


def http(method: str, url: str, body: dict | None = None, timeout: float = 10) -> tuple[int, Any]:
    request = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# -- driver --------------------------------------------------------------------------------
class Driver:
    def __init__(self, args: argparse.Namespace, server: ScriptedSSEServer, root: Path) -> None:
        self.args = args
        self.server = server
        self.root = root
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.home = root / "home"
        self.home.mkdir()
        self.config_dir = root / "config"
        self.config_dir.mkdir()
        self.port = int(server.base_url.rsplit(":", 1)[1].split("/")[0])

    def request_stats(self, start: int, end: int | None = None) -> dict[str, Any]:
        connections = self.server.connections[start:end]
        return {"requests": len(connections),
                "request_bytes": sum(len(json.dumps(c.body)) for c in connections),
                "reported_tokens": sum(28 for c in connections if c.finished_at is not None)}

    def sample(self, pid: int, extra: Callable[[], dict[str, Any]], dump: bool = False) -> dict[str, Any]:
        processes = [pid] + [p for p in descendants(pid) if Path(f"/proc/{p}").exists()]
        names = thread_names(pid)
        return {
            "threads": len(names),
            **({"python_threads": python_threads(pid)} if dump else {}),
            "agent_model_tcp_established": sum(model_connections(p, self.port) for p in processes),
            "server_open_requests": len(self.server.open_connections()),
            "server_errors": len(self.server.errors),
            **extra(),
        }

    def observe(self, started: float, pid: int, extra: Callable[[], dict[str, Any]],
                pending: Any) -> tuple[dict[str, Any], float | None]:
        samples: dict[str, Any] = {}
        closed_after = None
        for offset in self.args.samples:
            while time.monotonic() - started < offset:
                if closed_after is None and pending.closed.is_set():
                    closed_after = pending.closed_by_client_at - started
                time.sleep(0.01)
            if closed_after is None and pending.closed.is_set():
                closed_after = pending.closed_by_client_at - started
            samples[f"{offset:g}s"] = self.sample(pid, extra, dump=offset == self.args.samples[-1])
        return samples, closed_after


class TuiDriver(Driver):
    def run(self) -> dict[str, Any]:
        state = self.root / "state" / "sessions.sqlite3"
        config = self.root / "config.yaml"
        config.write_text(model_yaml(self.server.base_url, self.args.api, self.workspace, state, self.config_dir))
        agent = PtyAgent(self.args.repo, child_env(self.args.repo, self.home, {"DEEP_AGENT_CONFIG": str(config)}),
                         self.workspace)
        report: dict[str, Any] = {"pid": agent.pid, "normal_tasks": [], "cycles": []}
        lock_dir = state.parent / f"{state.name}.locks"

        def extra() -> dict[str, Any]:
            lines = [line.strip() for line in agent.display().splitlines() if line.strip()]
            return {"held_session_leases": held_leases(lock_dir),
                    "session_status": sorted(catalog_status(state).values()),
                    "screen_tail": lines[-5:]}

        def task(marker: str, timeout: float) -> dict[str, Any]:
            first = len(self.server.connections)
            self.server.add(answer_turn(marker))
            mark = agent.mark()
            started = time.monotonic()
            agent.send(f"reply with {marker}\r")
            ok = wait_for(lambda: agent.seen(f"{marker} done", mark) and self.server.connections[first:]
                          and self.server.connections[first].finished_at, timeout)
            elapsed = time.monotonic() - started
            ok = ok and wait_for(lambda: catalog_status(state) and set(catalog_status(state).values()) == {"stop"},
                                 timeout)
            time.sleep(0.3)
            return {"marker": marker, "completed": bool(ok),
                    "elapsed_s": round(elapsed, 3), **self.request_stats(first)}

        try:
            if not wait_for(lambda: agent.seen("workspace=", 0) or agent.seen("perm:", 0), 30):
                raise RuntimeError(f"TUI did not start: {agent.output[-2000:]}")
            time.sleep(1.0)
            report["baseline_threads"] = len(thread_names(agent.pid))
            for index in range(self.args.normal):
                report["normal_tasks"].append(task(f"NORMAL-{index}", 30))
            for index in range(self.args.cycles):
                scenario = SCENARIOS[index % len(SCENARIOS)] if self.args.scenario == "mixed" else self.args.scenario
                first = len(self.server.connections)
                self.server.add(stalled_turn(scenario))
                mark = agent.mark()
                agent.send(f"cancel probe {index}\r")
                connection = self.server.wait_for_connection(first, 30)
                if scenario == "continuous":
                    connection.first_chunk_sent.wait(10)
                    time.sleep(0.3)
                else:
                    connection.hanging.wait(10)
                    time.sleep(0.5)
                before = self.sample(agent.pid, extra, dump=True)
                started = time.monotonic()
                agent.send("\x1b")
                samples, closed_after = self.observe(started, agent.pid, extra, connection)
                cancelled_seen = agent.seen("Cancelled", mark)
                follow = task(f"FOLLOW-{index}", self.args.follow_timeout)
                follow_body = json.dumps(self.server.connections[first + 1].body) \
                    if len(self.server.connections) > first + 1 else ""
                cycle = {
                    "index": index, "scenario": scenario, "before_cancel": before, "samples": samples,
                    "server_saw_close_after_s": None if closed_after is None else round(closed_after, 4),
                    "ui_showed_cancelled": cancelled_seen, "follow_up": follow,
                    "follow_up_has_cancelled_prompt": f"cancel probe {index}" in follow_body,
                    "follow_up_has_abort_note": "aborted by the user" in follow_body,
                }
                report["cycles"].append(cycle)
                print(json.dumps({"mode": "tui", "api": self.args.api, **cycle}, ensure_ascii=False), flush=True)
                if not follow["completed"]:
                    report["stopped_early"] = f"follow-up task after cycle {index} did not complete"
                    break
        finally:
            report["exit_code"] = agent.close()
            report["leases_after_exit"] = held_leases(lock_dir)
            report["final_screen"] = agent.display()
        return report


class HttpDriver(Driver):
    def run(self) -> dict[str, Any]:
        state_dir = self.root / "service-state"
        model_config = self.root / "model.yaml"
        model_config.write_text(model_yaml(self.server.base_url, self.args.api, self.workspace, None,
                                           self.config_dir))
        service = self.root / "service.yaml"
        service.write_text(f"projects:\n  demo: {self.workspace}\nmodel_config: {model_config}\n"
                           f"state_dir: {state_dir}\ntoken_env: FAULT_INJECTION_TOKEN\nmax_runs: 2\n")
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        log = (self.root / "serve.log").open("w")
        process = subprocess.Popen(
            [sys.executable, "-c", "from agent.cli.main import main; main()", "serve", "--config", str(service),
             "--port", str(port)], cwd=self.root,
            env=child_env(self.args.repo, self.home, {"FAULT_INJECTION_TOKEN": TOKEN}),
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        report: dict[str, Any] = {"pid": process.pid, "normal_tasks": [], "cycles": []}
        lock_dir = state_dir / "projects" / "demo" / "sessions.sqlite3.locks"
        current: dict[str, str] = {}

        def extra() -> dict[str, Any]:
            status = http("GET", f"{base}/v1/runs/{current['run']}")[1]["status"] if current.get("run") else None
            return {"run_status": status, "held_session_leases": held_leases(lock_dir)}

        def task(session: str, marker: str, timeout: float) -> dict[str, Any]:
            first = len(self.server.connections)
            self.server.add(answer_turn(marker))
            started = time.monotonic()
            code, run = http("POST", f"{base}/v1/sessions/{session}/runs", {"text": f"reply with {marker}"})
            if code != 202:
                self.server._turns.clear()
                return {"marker": marker, "completed": False, "submit_status": code, "detail": run}
            final = wait_for(lambda: (r := http("GET", f"{base}/v1/runs/{run['id']}")[1])["status"]
                             in {"completed", "failed", "cancelled"} and r, timeout)
            return {"marker": marker, "completed": bool(final) and final["status"] == "completed",
                    "final_status": final["status"] if final else None,
                    "elapsed_s": round(time.monotonic() - started, 3), **self.request_stats(first)}

        try:
            if not wait_for(lambda: _health(base), 30, 0.1):
                raise RuntimeError("service did not start")
            report["baseline_threads"] = len(thread_names(process.pid))
            code, session = http("POST", f"{base}/v1/sessions", {"project_id": "demo"})
            assert code == 201, session
            for index in range(self.args.normal):
                report["normal_tasks"].append(task(session["id"], f"NORMAL-{index}", 30))
            for index in range(self.args.cycles):
                scenario = SCENARIOS[index % len(SCENARIOS)] if self.args.scenario == "mixed" else self.args.scenario
                first = len(self.server.connections)
                self.server.add(stalled_turn(scenario))
                code, run = http("POST", f"{base}/v1/sessions/{session['id']}/runs", {"text": f"cancel probe {index}"})
                if code != 202:
                    self.server._turns.clear()
                    report["stopped_early"] = f"cycle {index} submit returned {code}: {run}"
                    break
                current["run"] = run["id"]
                connection = self.server.wait_for_connection(first, 30)
                if scenario == "continuous":
                    connection.first_chunk_sent.wait(10)
                    time.sleep(0.3)
                else:
                    connection.hanging.wait(10)
                    time.sleep(0.5)
                before = self.sample(process.pid, extra, dump=True)
                started = time.monotonic()
                cancel_code, _ = http("POST", f"{base}/v1/runs/{run['id']}/cancel")
                cancel_response_s = time.monotonic() - started
                terminal = {"at": None}

                def watch_terminal() -> dict[str, Any]:
                    data = extra()
                    if terminal["at"] is None and data["run_status"] in {"cancelled", "completed", "failed"}:
                        terminal["at"] = round(time.monotonic() - started, 4)
                    return data

                samples, closed_after = self.observe(started, process.pid, watch_terminal, connection)
                events = _events(base, run["id"])
                follow = task(session["id"], f"FOLLOW-{index}", self.args.follow_timeout)
                follow_body = json.dumps(self.server.connections[first + 1].body) \
                    if len(self.server.connections) > first + 1 else ""
                cycle = {
                    "index": index, "scenario": scenario, "before_cancel": before,
                    "cancel_http_status": cancel_code, "cancel_response_s": round(cancel_response_s, 4),
                    "samples": samples,
                    "server_saw_close_after_s": None if closed_after is None else round(closed_after, 4),
                    "events": events, "follow_up": follow,
                    "follow_up_has_cancelled_prompt": f"cancel probe {index}" in follow_body,
                }
                report["cycles"].append(cycle)
                print(json.dumps({"mode": "http", "api": self.args.api, **cycle}, ensure_ascii=False), flush=True)
                if not follow["completed"]:
                    report["stopped_early"] = f"follow-up task after cycle {index} did not complete"
                    break
        finally:
            process.send_signal(signal.SIGINT)
            try:
                report["exit_code"] = process.wait(45)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                report["exit_code"] = f"killed after 45s ({process.wait(5)})"
            report["leases_after_exit"] = held_leases(lock_dir)
            log.close()
            report["log_tail"] = (self.root / "serve.log").read_text()[-1500:]
        return report


def _health(base: str) -> bool:
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"{base}/health", timeout=1):
            return True
    except OSError:
        return False


def _events(base: str, run_id: str) -> list[str]:
    request = urllib.request.Request(f"{base}/v1/runs/{run_id}/events",
                                     headers={"Authorization": f"Bearer {TOKEN}"})
    names: list[str] = []
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
            event = ""
            for line in response:
                if line.startswith(b"event: "):
                    event = line[7:].decode().strip()
                elif line.startswith(b"data: ") and event != "delta":
                    data = json.loads(line[6:])
                    inner = data.get("type") if event == "agent_event" and isinstance(data, dict) else None
                    names.append(f"agent_event:{inner}" if inner else event)
    except (OSError, TimeoutError):
        names.append("<stream still open>")
    return names


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    cycles = report["cycles"]
    final_key = list(cycles[0]["samples"])[-1] if cycles else None
    closes = [c["server_saw_close_after_s"] for c in cycles]
    normal = report["normal_tasks"]
    follows = [c["follow_up"] for c in cycles]
    return {
        "cycles_run": len(cycles),
        "server_saw_close": sum(x is not None for x in closes),
        "max_close_s": max((x for x in closes if x is not None), default=None),
        "open_model_tcp_at_last_sample": sum(c["samples"][final_key]["agent_model_tcp_established"] for c in cycles)
        if final_key else None,
        "follow_ups_completed": sum(f["completed"] for f in follows),
        "normal_mean_s": round(sum(t["elapsed_s"] for t in normal) / len(normal), 3) if normal else None,
        "normal_requests": sum(t["requests"] for t in normal),
        "normal_request_bytes": sum(t["request_bytes"] for t in normal),
        "normal_reported_tokens": sum(t["reported_tokens"] for t in normal),
        "follow_mean_s": round(sum(f["elapsed_s"] for f in follows if "elapsed_s" in f) / max(1, len(follows)), 3),
        "threads": [report.get("baseline_threads")] + [c["samples"][final_key]["threads"] for c in cycles]
        if final_key else [],
        "stopped_early": report.get("stopped_early"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("tui", "http"), required=True)
    parser.add_argument("--api", choices=("chat_completions", "responses"), default="chat_completions")
    parser.add_argument("--scenario", choices=(*SCENARIOS, "mixed"), default="mixed")
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--normal", type=int, default=3, help="normal tasks before the injections")
    parser.add_argument("--samples", default="5,15,30", help="seconds after cancel to sample")
    parser.add_argument("--follow-timeout", type=float, default=20)
    parser.add_argument("--repo", type=Path, default=ROOT, help="checkout whose agent package is launched")
    parser.add_argument("--label", default="")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    args.samples = [float(x) for x in args.samples.split(",")]
    args.repo = args.repo.resolve()
    commit = subprocess.run(["git", "-C", str(args.repo), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="cancel-fi-") as tmp, ScriptedSSEServer(args.api) as server:
        driver = (TuiDriver if args.mode == "tui" else HttpDriver)(args, server, Path(tmp))
        started = time.time()
        report = driver.run()
    result = {"label": args.label, "mode": args.mode, "api": args.api, "repo_commit": commit,
              "scenario": args.scenario, "samples_s": args.samples, "started_at": started,
              "summary": summarize(report), **report}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
    print(json.dumps({"label": args.label, "mode": args.mode, "api": args.api, "commit": commit,
                      **result["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
