"""Presentation state and lifecycle-owned work for the right-hand panel."""
import asyncio
from collections import deque
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
from time import monotonic
from uuid import uuid4

from agent.companion import STATES
from agent.companion.evidence import update_windows
from agent.companion.monitor import Resources, services

def _frames(*key):
    # Import NumPy and decode the first GIF on the background worker.
    from agent.companion.animation import frames
    yield from frames(*key)



def clean(value):
    return "".join(c for c in str(value) if c.isprintable())[:240]


async def start_worker(config_dir):
    # Isolated Python ignores user site packages and PYTHONPATH. Inject only
    # the install root so the same entry point also works in a source checkout.
    root = str(Path(__file__).resolve().parents[2])
    bootstrap = ("import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
                 "runpy.run_module('agent.companion.worker',run_name='__main__')")
    env = {k: os.environ[k] for k in ("HOME", "PATH", "LANG", "COMPANION_DEEPSEEK_API_KEY") if k in os.environ}
    return await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", bootstrap, root, str(config_dir), env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)


class Companion:
    def __init__(self, *, workspace, config_dir, session, geometry, invalidate, timezone):
        self.session = session
        self.geometry = geometry
        self.invalidate = invalidate
        self.timezone = timezone
        workspace_id = sha256(os.fsencode(workspace.resolve())).hexdigest()[:24]
        self.config_dir = config_dir
        self.database = config_dir / "companion" / (workspace_id + ".sqlite3")
        self.thread = session()
        self.run = uuid4().hex
        self.seq = 0
        self.calls = {}
        self.pending = []
        self.storage_lock = asyncio.Lock()
        self.windows = []
        self.batches = {}
        self.behavior = "idle"
        self.until = 0
        self.connection = "STARTING"
        self.metrics = (None, None, None)
        self.services = ()
        self.logs = deque(maxlen=240)
        self.log_sequence = 0
        self.anchor = None
        self.frame = []
        self.changed = asyncio.Event()
        self.last_reason = ""
        self.last_remark = ""
        self.last_evidence = None

    def log(self, message):
        self.log_sequence += 1
        self.logs.append((datetime.now(self.timezone).strftime("%H:%M:%S"), clean(message)))
        self.invalidate()

    def sync_session(self):
        current = self.session()
        if current != self.thread:
            self.flush_calls()
            self.thread = current
            self.seq += 1
            self.changed.clear()
            self.logs.clear()
            self.log_sequence += 1
            self.anchor = None
            self.behavior = "idle"
            self.until = 0
            self.last_evidence = self.last_remark = None
            self.batches.clear()
            self.run = uuid4().hex

    def flush_calls(self):
        self.pending.extend((self.thread, self.run, key, *row) for key, row in self.calls.items())
        self.calls.clear()

    def notify(self, reason):
        self.seq += 1
        self.last_reason = reason
        self.changed.set()

    def event(self, event, model):
        self.sync_session()
        kind = event.type
        if kind == "run_started":
            self.flush_calls()
            self.run = uuid4().hex
            self.batches.clear()
            self.behavior = "focused"
            self.until = 0
            self.seq += 1
        elif kind == "tool_started" and event.tool_call_id:
            if event.tool_call_id not in self.calls:
                self.calls[event.tool_call_id] = [model, clean(event.name), "unknown"]
                if event.message_id:
                    self.batches.setdefault(event.message_id, []).append(clean(event.name))
            self.behavior = "observing"
        elif kind == "tool_completed" and event.tool_call_id in self.calls:
            if self.calls[event.tool_call_id][2] == "unknown":
                self.calls[event.tool_call_id][2] = "failed" if event.is_error else "success"
        elif kind == "interaction_requested":
            self.behavior = "curious"
            self.seq += 1
        elif kind in ("run_failed", "run_completed", "run_cancelled"):
            self.behavior = {"run_failed": "concerned", "run_completed": "relieved", "run_cancelled": "idle"}[kind]
            self.until = monotonic() + 4.8
            self.flush_calls()
            self.notify(kind)

    async def save_statistics(self, *, read=False):
        async with self.storage_lock:
            if not self.pending and not read:
                return
            # Keep the batch until its transaction commits, including cancellation.
            count = len(self.pending)
            self.windows = await asyncio.to_thread(update_windows, self.database, self.pending[:count])
            del self.pending[:count]

    async def finish(self):
        self.flush_calls()
        try:
            await self.save_statistics()
        except (OSError, sqlite3.Error):
            pass

    async def monitor(self):
        sampler = Resources()
        while True:
            self.sync_session()
            try:
                await self.save_statistics()
            except (OSError, sqlite3.Error):
                pass
            if self.geometry()[0]:
                self.metrics = await asyncio.to_thread(sampler.sample)
                try:
                    current = await asyncio.to_thread(services)
                except OSError:
                    current = self.services
                if current != self.services:
                    old = set(self.services)
                    for row in current:
                        if row not in old:
                            self.log(f"Service started: {row.name} (PID {row.pid})")
                    for row in old.difference(current):
                        self.log(f"Service stopped: {row.name} (PID {row.pid})")
                    self.services = current
                    self.notify("services changed")
                self.invalidate()
            await asyncio.sleep(2)

    async def animate(self):
        key = iterator = None
        cache = []
        index = 0
        while True:
            width, height = self.geometry()
            if self.until and monotonic() >= self.until:
                self.behavior, self.until = "idle", 0
            current = self.behavior, width, height
            if current != key:
                key, iterator, cache, index = current, None, [], 0
                self.frame = []
            delay = 0.2
            if width:
                try:
                    if iterator is None:
                        iterator = _frames(*current)
                    if index == len(cache):
                        item = await asyncio.to_thread(next, iterator, None)
                        if item is None:
                            index = 0
                        else:
                            cache.append(item)
                    if (self.behavior, *self.geometry()) == current:
                        self.frame, delay = cache[index]
                        self.invalidate()
                    index += 1
                except Exception:
                    self.frame = [("#777777", "Animation unavailable")]
                    delay = 1
            await asyncio.sleep(max(0.04, delay))

    async def observe(self):
        while not self.geometry()[0]:
            await asyncio.sleep(0.25)
        process = None
        spawn = asyncio.create_task(start_worker(self.config_dir))
        try:
            process = await asyncio.shield(spawn)
            hello = json.loads(await asyncio.wait_for(process.stdout.readline(), 30))
            if hello.get("status") != "ready":
                self.log("Companion API key unavailable.")
                return
            self.connection = "CONNECTED"
            self.log("Connected. Watching the environment.")
            while True:
                try:
                    await asyncio.wait_for(self.changed.wait(), 1)
                except TimeoutError:
                    if process.returncode is not None:
                        break
                    continue
                if not self.geometry()[0]:
                    await asyncio.sleep(0.5)
                    continue
                self.sync_session()
                if not self.changed.is_set():
                    continue
                self.changed.clear()
                seq, session = self.seq, self.thread
                # Commit this run before giving its evidence to the model.
                await self.save_statistics(read=True)
                if (session, seq) != (self.session(), self.seq):
                    continue
                evidence = self.windows
                signature = json.dumps(evidence, sort_keys=True)
                if not any(row["calls"] >= 3 for row in evidence) or signature == self.last_evidence:
                    continue
                snapshot = dict(reason=self.last_reason, windows=evidence, behavior=self.behavior,
                                parallel_tool_batches=[batch[:16] for batch in list(self.batches.values())[-8:]],
                                services=[clean(row.name) for row in self.services[:12]],
                                previous_remark=self.last_remark)
                request = json.dumps(dict(seq=seq, session=session, snapshot=snapshot)).encode() + b"\n"
                if len(request) >= 65536:
                    continue
                process.stdin.write(request)
                await process.stdin.drain()
                reply = json.loads(await asyncio.wait_for(process.stdout.readline(), 32))
                self.sync_session()
                if (session, seq) != (self.thread, self.seq) or (reply.get("session"), reply.get("seq")) != (session, seq):
                    continue
                if reply.get("status") != "ok":
                    if self.connection != "OFFLINE":
                        self.log("DeepSeek Flash unavailable.")
                    self.connection = "OFFLINE"
                    continue
                self.connection = "CONNECTED"
                self.last_evidence = signature
                if reply.get("behavior") in STATES:
                    self.behavior = reply["behavior"]
                    self.until = monotonic() + 4.8
                message = clean(reply.get("message", ""))
                if message and message != self.last_remark:
                    self.last_remark = message
                    self.log(message)
        except (OSError, ValueError, TimeoutError, sqlite3.Error):
            self.log("Companion observer unavailable.")
        finally:
            self.connection = "OFFLINE"
            if process is None and not spawn.cancelled():
                try:
                    process = await spawn
                except OSError:
                    pass
            if process is not None and process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 1)
                except TimeoutError:
                    process.kill()
                    await process.wait()
