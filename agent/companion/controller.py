"""Presentation state and lifecycle-owned work for the right-hand panel."""

import asyncio
from collections import OrderedDict, deque
from datetime import datetime
from dataclasses import asdict
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
from time import monotonic, time
from uuid import uuid4

from agent.companion import STATES
from agent.companion.timeline import (
    Timeline,
    ToolObservation,
    category,
    restore_session,
    timeline_window,
    transact,
)
from agent.sessions.summary import redact_summary_input
from agent.companion.repository import Repository
from agent.companion.monitor import Resources, services


def _frames(*key):
    # Import NumPy and decode the first GIF on the background worker.
    from agent.companion.animation import frames

    yield from frames(*key)


def clean(value):
    return "".join(c for c in str(value)[:512] if c.isprintable())[:240]


async def start_worker(config_dir):
    # Isolated Python ignores user site packages and PYTHONPATH. Inject only
    # the install root so the same entry point also works in a source checkout.
    root = str(Path(__file__).resolve().parents[2])
    bootstrap = (
        "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
        "runpy.run_module('agent.companion.worker',run_name='__main__')"
    )
    env = {
        k: os.environ[k]
        for k in ("HOME", "PATH", "LANG", "COMPANION_DEEPSEEK_API_KEY")
        if k in os.environ
    }
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-B",
        "-c",
        bootstrap,
        root,
        str(config_dir),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


class Companion:
    def __init__(
        self,
        *,
        workspace,
        config_dir,
        session,
        geometry,
        invalidate,
        timezone,
        protected=(),
        secrets=(),
        profile=None,
        window=None,
    ):
        self.workspace = workspace.resolve()
        self.protected = (config_dir, *protected)
        self.repository = Repository(self.workspace, self.protected, secrets)
        self.secrets = secrets
        self.worker_ready = False
        self.question = None
        self.errors = deque(maxlen=10)
        self.session_generation = 0
        self.session = session
        self.geometry = geometry
        self.invalidate = invalidate
        self.timezone = timezone
        workspace_id = sha256(os.fsencode(workspace.resolve())).hexdigest()[:24]
        self.config_dir = config_dir
        self.database = config_dir / "companion" / (workspace_id + ".sqlite3")
        self.thread = session()
        self.run = uuid4().hex
        self.profile = profile or (lambda: ("fixed model", "default"))
        try:
            self.window = max(
                1, min(500, int(window or os.getenv("LAIN_FAILURE_WINDOW", "20")))
            )
        except ValueError:
            self.window = 20
        self.timeline = Timeline(self.thread, self.window)
        self.loaded = False
        self.next_prune = 0
        self.starts = {}
        self.turn = self.run
        self.turn_start = None
        self.turn_profile = self.profile()
        self.last_profile = None
        self.turn_rates = {}
        self.turn_metrics = []
        self.turn_git = []
        self.last_git = None
        self.request_version = 0
        self.calls = {}
        self.pending = []
        self.storage_lock = asyncio.Lock()
        self.batches = {}
        self.behavior = "idle"
        self.until = 0
        self.connection = "STARTING"
        self.metrics = (None, None, None, None)
        self.services = ()
        self.logs = deque(maxlen=240)
        self.log_sequence = 0
        self.anchor = None
        self.frame = []
        self.changed = asyncio.Event()

    def ask(self, text):
        self.sync_session()
        if self.question:
            return "Lain is answering your previous question."
        if self.connection == "OFFLINE" and not self.worker_ready:
            return "Lain is offline. Check the Companion API key and connection."
        text = " ".join(text.split())
        if not text or len(text) > 800:
            return "Use /lain <question> (up to 800 characters)."
        self.question = text
        self.anchor = None
        self.log(text, speaker="You")
        self.changed.set()
        return "Question sent to Lain."

    def log(self, message, speaker="Lain"):
        self.log_sequence += 1
        self.logs.append(
            (
                datetime.now(self.timezone).strftime("%H:%M:%S"),
                speaker,
                "".join(c for c in str(message) if c.isprintable() or c == "\n")[:1600],
            )
        )
        self.invalidate()

    def sync_session(self):
        current = self.session()
        if current != self.thread:
            self.close_turn("unknown")
            self.pending.extend(self.timeline.pending)
            self.timeline = Timeline(current, self.window)
            self.loaded = False
            self.last_git = None
            self.last_profile = None
            self.thread = current
            self.session_generation += 1
            self.errors.clear()
            self.question = None
            self.changed.clear()
            self.logs.clear()
            self.log_sequence += 1
            self.anchor = None
            self.behavior = "idle"
            self.until = 0
            self.batches.clear()
            self.run = uuid4().hex
            self.turn = self.run
            self.services = ()

    def close_call(self, obs, outcome, wall, tick, error="", exit_code=None):
        if obs.finished_at is not None:
            return
        obs.outcome = outcome
        obs.finished_at = wall
        start = self.starts.pop(obs.tool_call_id, None)
        obs.duration_ms = (
            max(0, round((tick - start) * 1000)) if start is not None else None
        )
        obs.error_category = error
        obs.exit_code = exit_code
        self.timeline.completed(obs)
        self.timeline.record(
            "tool",
            asdict(obs),
            key=obs.run_id + "/" + obs.tool_call_id,
            stamp=wall,
            turn=obs.turn_id,
        )
        if self.timeline.queue:
            self.changed.set()

    def close_turn(self, outcome, wall=None, tick=None):
        wall = time() if wall is None else wall
        tick = monotonic() if tick is None else tick
        if self.turn_start is None and not self.calls:
            return
        tools = {}
        for obs in self.calls.values():
            self.close_call(
                obs, "cancelled" if outcome == "cancelled" else "unknown", wall, tick
            )
            key = (obs.model_id, obs.reasoning_effort, obs.tool_name)
            row = tools.setdefault(key, dict(outcomes=Counter(), duration_ms=0))
            row["outcomes"][obs.outcome] += 1
            row["duration_ms"] += obs.duration_ms or 0
            known = row["outcomes"]["success"] + row["outcomes"]["failed"]
            row["success_pct"] = (
                round(100 * row["outcomes"]["success"] / known, 2) if known else None
            )
        elapsed = (
            max(0, round((tick - self.turn_start) * 1000))
            if self.turn_start is not None
            else None
        )
        previous = [
            r["duration_ms"]
            for r in self.timeline.summaries
            if r["duration_ms"] is not None
        ]
        summary = dict(
            turn=self.turn,
            run=self.run,
            profile=self.turn_profile,
            outcome=outcome,
            duration_ms=elapsed,
            previous_turn_ms=previous[-3:],
            git=self.turn_git,
            resources=self.turn_metrics,
            tools=[
                dict(
                    model=m,
                    effort=e,
                    tool=t,
                    **d,
                    recent_pct=self.timeline.series[m, e, t].rate,
                    start_pct=self.turn_rates.get((m, e, t)),
                )
                for (m, e, t), d in tools.items()
            ],
            retry_chains=[
                (o.tool_call_id, o.retry_of) for o in self.calls.values() if o.retry_of
            ],
            incidents=[
                i.copy()
                for i in self.timeline.incidents.values()
                if self.turn in (i["turn"], i.get("resolved_turn"))
            ],
        )
        self.timeline.record("turn", summary, key=self.turn, stamp=wall, turn=self.turn)
        self.timeline.summaries.append(summary)
        self.calls.clear()
        self.turn_start = None
        self.turn_metrics = []
        self.turn_git = []

    def git_changed(self, summary):
        self.sync_session()
        value = asdict(summary)
        if value != self.last_git:
            self.last_git = value
            self.timeline.sample(
                "git", value, self.turn if self.turn_start is not None else ""
            )
            if self.turn_start is not None:
                self.turn_git[:] = (self.turn_git[:1] + [value])[-2:]

    def event(
        self,
        event,
        model,
        effort="default",
        agent="main",
        wall=None,
        tick=None,
        turn_id=None,
    ):
        self.sync_session()
        wall = time() if wall is None else wall
        tick = monotonic() if tick is None else tick
        kind = event.type
        if kind in ("run_started", "turn_started"):
            if event.turn_id and (
                self.turn == event.turn_id
                or any(r["turn"] == event.turn_id for r in self.timeline.summaries)
            ):
                return
            self.close_turn("unknown", wall, tick)
            self.run = event.run_id or uuid4().hex
            self.turn = event.turn_id or turn_id or self.run
            self.turn_start = tick
            self.turn_profile = (model, effort)
            self.last_profile = (model, effort)
            self.turn_rates = {k: s.rate for k, s in self.timeline.series.items()}
            self.turn_metrics = [self.metrics]
            self.turn_git = [self.last_git] if self.last_git else []
            self.timeline.record(
                "run",
                dict(model=model, effort=effort, agent=agent, run=self.run),
                key=self.turn,
                stamp=wall,
                turn=self.turn,
            )
            self.batches.clear()
            self.behavior, self.until = "focused", 0
        elif (
            kind == "tool_started"
            and event.tool_call_id
            and event.tool_call_id not in self.calls
        ):
            if (self.run, event.tool_call_id) in self.timeline.finished:
                return
            name = clean(event.name)
            obs = ToolObservation(
                self.thread,
                self.turn,
                self.run,
                agent,
                model,
                effort,
                name,
                event.tool_call_id,
                wall,
            )
            series = self.timeline.series.get((model, effort, name))
            previous = series.last_failed if series else None
            if (
                previous
                and previous.run_id == self.run
                and previous.agent_id == agent
                and 0 <= wall - previous.finished_at <= 60
            ):
                obs.retry_of = previous.tool_call_id
            self.calls[event.tool_call_id] = obs
            self.starts[event.tool_call_id] = tick
            self.timeline.record(
                "tool",
                asdict(obs),
                key=self.run + "/" + obs.tool_call_id,
                stamp=wall,
                turn=self.turn,
            )
            if event.message_id:
                self.batches.setdefault(event.message_id, []).append(name)
            self.behavior = "observing"
        elif kind == "tool_completed":
            obs = self.calls.get(event.tool_call_id)
            if obs is None:
                # Resuming a checkpoint can deliver the end of a pre-crash call.
                matches = [
                    o
                    for (run, call), o in self.timeline.interrupted.items()
                    if call == event.tool_call_id
                    and (not event.run_id or event.run_id == run)
                ]
                obs = matches[0] if len(matches) == 1 else None
            if obs is None or obs.finished_at is not None:
                return
            artifact = event.artifact if isinstance(event.artifact, dict) else {}
            code = artifact.get("exit_code")
            code = code if type(code) is int else None
            cancelled = (
                artifact.get("cancelled")
                or artifact.get("status") == "cancelled"
                or artifact.get("termination_reason") == "cancelled"
            )
            outcome = (
                "cancelled"
                if cancelled
                else "failed"
                if event.is_error or code not in (None, 0)
                else "success"
            )
            error = category(event.content, code) if outcome == "failed" else ""
            self.close_call(obs, outcome, wall, tick, error, code)
            if outcome == "failed":
                self.errors.append(
                    (
                        obs.tool_name,
                        redact_summary_input(clean(event.content), self.secrets),
                    )
                )
        elif kind == "turn_completed":
            self.close_turn("completed", wall, tick)
        elif kind == "interaction_requested":
            self.behavior = "curious"
        elif kind in ("run_failed", "run_completed", "run_cancelled"):
            self.behavior = {
                "run_failed": "concerned",
                "run_completed": "relieved",
                "run_cancelled": "idle",
            }[kind]
            self.until = monotonic() + 4.8
            self.close_turn(kind.removeprefix("run_"), wall, tick)
        self.invalidate()

    def child_event(self, event, wall):
        session, run, turn = event["scope"]
        self.sync_session()
        if session != self.thread:
            return
        call = event["tool_run_id"]
        duration = event.get("elapsed_seconds", 0)
        obs = ToolObservation(
            session,
            turn,
            run,
            event["agent"],
            event["model"],
            event["effort"],
            event["tool_name"],
            call,
            wall - duration,
        )
        if (run, call) in self.timeline.finished:
            return
        if event["phase"] != "tool_start":
            obs.finished_at, obs.duration_ms = wall, round(duration * 1000)
            obs.exit_code = event.get("exit_code")
            obs.outcome = {
                "cancelled": "cancelled",
                "failed": "failed",
                "completed": "success",
            }.get(event["status"], "unknown")
            if obs.outcome == "failed":
                obs.error_category = category("", obs.exit_code)
            self.timeline.completed(obs)
            if turn == self.turn and self.turn_start is not None:
                self.calls[call] = obs
            else:
                for summary in self.timeline.summaries:
                    if summary["turn"] == turn:
                        summary.setdefault("background_completions", []).append(
                            asdict(obs)
                        )
                        self.timeline.record(
                            "turn", summary.copy(), key=turn, turn=turn
                        )
                        break
            if self.timeline.queue:
                self.changed.set()
        self.timeline.record(
            "tool", asdict(obs), key=run + "/" + call, stamp=wall, turn=turn
        )
        self.invalidate()

    async def save_statistics(self, *, read=False):
        async with self.storage_lock:
            state = self.timeline
            if not self.loaded:
                if self.pending:
                    count = len(self.pending)
                    await asyncio.to_thread(
                        transact, self.database, self.pending[:count]
                    )
                    del self.pending[:count]
                combined = await asyncio.to_thread(
                    restore_session, self.database, state.session, self.window
                )
                if state is not self.timeline:
                    return
                combined.merge_live_events(state.pending)
                self.timeline = state = combined
                self.loaded = True
                self.invalidate()
            self.pending.extend(state.pending)
            state.pending.clear()
            if not self.pending:
                return
            count = len(self.pending)
            prune = monotonic() >= self.next_prune
            await asyncio.to_thread(
                transact, self.database, self.pending[:count], None, prune
            )
            if prune:
                self.next_prune = monotonic() + 60
            del self.pending[:count]

    async def finish(self):
        self.close_turn("unknown")
        try:
            await self.save_statistics()
        except (OSError, sqlite3.Error):
            pass

    async def monitor(self):
        sampler = Resources(self.workspace)
        while True:
            self.sync_session()
            try:
                await self.save_statistics()
            except (OSError, sqlite3.Error):
                pass
            profile = self.profile()
            if self.last_profile != profile:
                self.timeline.sample(
                    "profile", profile, self.turn if self.turn_start is not None else ""
                )
                self.last_profile = profile
            self.metrics = await asyncio.to_thread(sampler.sample)
            self.timeline.sample(
                "resources",
                dict(percent=self.metrics, disk_bytes=sampler.disk_bytes),
                self.turn if self.turn_start is not None else "",
            )
            if self.turn_start is not None:
                self.turn_metrics[:] = self.turn_metrics[:1] + [self.metrics]
            try:
                current = await asyncio.to_thread(services)
            except OSError:
                current = self.services
            if current != self.services:
                old = set(self.services)
                for row in current:
                    if row not in old:
                        self.log(
                            f"Service started: {row.name} (PID {row.pid})", "Status"
                        )
                for row in old.difference(current):
                    self.log(f"Service stopped: {row.name} (PID {row.pid})", "Status")
                self.services = current
                self.timeline.sample(
                    "services",
                    [(r.name, r.pid) for r in current],
                    self.turn if self.turn_start is not None else "",
                )
            self.invalidate()
            await asyncio.sleep(2)

    async def animate(self):
        clips = OrderedDict()
        key = iterator = None
        cache = []
        index = 0
        while True:
            width, height = self.geometry()
            if self.until and monotonic() >= self.until:
                self.behavior, self.until = "idle", 0
            current = self.behavior, width, height
            if current != key:
                if key and iterator is not None:
                    clips[key] = (iterator, cache)
                    if len(clips) > 3:
                        old, _ = clips.popitem(last=False)[1]
                        old.close()
                iterator, cache = clips.pop(current, (None, []))
                key, index = current, 0
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

    def session_evidence(self, kind):
        state = self.timeline
        if kind == "statistics":
            rows = [
                dict(model=m, effort=e, tool=t, **series.statistics())
                for (m, e, t), series in state.series.items()
                if (m, e) == self.profile()
            ]
            return sorted(
                rows, key=lambda r: r["counts"].get("failed", 0), reverse=True
            )[:48]
        if kind == "failures":
            return list(state.failures)[-8:]
        if kind == "retries":
            return list(state.retries)[-8:]
        if kind == "turns":
            return list(state.summaries)[-3:]
        return []

    async def host_query(self, kind, args):
        if kind in ("timeline", "git"):
            end = min(float(args.get("end") or time()), time())
            start = max(float(args.get("start") or end - 14 * 86400), end - 14 * 86400)
            rows = await asyncio.to_thread(
                timeline_window, self.database, self.thread, kind, start, end
            )
            return json.dumps(rows, ensure_ascii=False)[:6000]
        if kind in ("search", "files"):
            return await asyncio.to_thread(
                self.repository.search,
                str(args.get("term", ""))[:160],
                paths_only=kind == "files",
            )
        if kind == "read":
            return await asyncio.to_thread(
                self.repository.read,
                str(args.get("path", ""))[:512],
                int(args.get("start", 1)),
            )
        return json.dumps(self.session_evidence(kind), ensure_ascii=False)[:6000]

    async def observe(self):
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(2 ** (attempt - 1))
            try:
                if await self.worker_session() == "no key":
                    return
            except (OSError, ValueError, TimeoutError, sqlite3.Error):
                self.log("Companion connection interrupted.", "Status")
        self.log("Companion offline; local monitoring continues.", "Status")

    async def worker_session(self):
        process = None
        spawn = asyncio.create_task(start_worker(self.config_dir))
        try:
            process = await asyncio.shield(spawn)
            hello = json.loads(await asyncio.wait_for(process.stdout.readline(), 30))
            if hello.get("status") == "no key":
                self.log("Chat offline · Companion API key unavailable.", "Status")
                self.question = None
                return "no key"
            if hello.get("status") != "ready":
                raise ValueError("worker unavailable")
            self.worker_ready = True
            self.connection = "CONNECTED"
            while True:
                self.sync_session()
                if not self.question and not self.timeline.queue:
                    self.changed.clear()
                    try:
                        await asyncio.wait_for(self.changed.wait(), 1)
                    except TimeoutError:
                        if process.returncode is not None:
                            return
                    continue
                await self.save_statistics()
                self.sync_session()
                question = self.question
                state = self.timeline
                generation = self.session_generation
                incident = None
                if question:
                    snapshot = dict(
                        question=question,
                        snapshot_version=state.version,
                        profile=self.profile(),
                        recent_errors=list(self.errors),
                        conversation=[(r, m[:400]) for _, r, m in list(self.logs)[-5:]],
                    )
                elif state.queue:
                    key, incident_id, version = state.queue.popleft()
                    incident = state.incidents.get(key)
                    if not incident or (
                        incident["id"],
                        incident["version"],
                        incident["state"],
                    ) != (incident_id, version, "open"):
                        continue
                    incident["requested"] = True
                    state.record(
                        "incident",
                        incident.copy(),
                        key=incident_id,
                        turn=incident["turn"],
                    )
                    await self.save_statistics()
                    if (
                        self.session() != self.thread
                        or state is not self.timeline
                        or state.incidents.get(key) is not incident
                    ):
                        continue
                    snapshot = dict(
                        incident=incident,
                        snapshot_version=state.version,
                        recent_failures=[
                            f
                            for f in state.failures
                            if (f["model_id"], f["reasoning_effort"]) == key[:2]
                            and key[2] in ("*", f["tool_name"])
                        ][-5:],
                    )
                else:
                    continue
                self.request_version += 1
                identity = dict(
                    session=self.thread, seq=self.request_version, token=uuid4().hex
                )

                def current():
                    return (
                        self.session() == identity["session"]
                        and generation == self.session_generation
                        and (not incident or state.incidents.get(key) is incident)
                    )

                async def send(payload):
                    process.stdin.write(
                        json.dumps(
                            dict(**identity, **payload), ensure_ascii=False
                        ).encode()
                        + b"\n"
                    )
                    await process.stdin.drain()

                await send(dict(snapshot=snapshot))
                async with asyncio.timeout(35):
                    while True:
                        reply = json.loads(await process.stdout.readline())
                        if any(reply.get(k) != v for k, v in identity.items()):
                            raise ValueError("mismatched companion response")
                        if "query" not in reply:
                            break
                        data = "Expired request."
                        if question and current():
                            try:
                                data = await asyncio.wait_for(
                                    self.host_query(
                                        reply["query"], reply.get("args", {})
                                    ),
                                    2,
                                )
                            except (
                                OSError,
                                ValueError,
                                TypeError,
                                TimeoutError,
                                sqlite3.Error,
                            ):
                                data = "Unavailable."
                        await send(
                            dict(data=data[:6000] if current() else "Expired request.")
                        )
                self.sync_session()
                if not current():
                    continue
                if question:
                    self.question = None
                    self.log(reply.get("message") or "暂时无法回答，请稍后再试。")
                else:
                    message = clean(reply.get("message", ""))
                    analysis = dict(
                        incident=incident_id,
                        version=version,
                        turn=incident["turn"],
                        status=reply.get("status"),
                        message=message,
                    )
                    state.record(
                        "analysis",
                        analysis,
                        key=f"{incident_id}/{version}",
                        turn=incident["turn"],
                    )
                    incident["analysis"] = analysis
                    for summary in state.summaries:
                        if summary["turn"] == incident["turn"]:
                            summary.setdefault("analyses", []).append(analysis)
                            state.record(
                                "turn",
                                summary.copy(),
                                key=summary["turn"],
                                turn=summary["turn"],
                            )
                    state.record(
                        "incident",
                        incident.copy(),
                        key=incident_id,
                        turn=incident["turn"],
                    )
                    if reply.get("behavior") in STATES:
                        self.behavior, self.until = reply["behavior"], monotonic() + 4.8
                    if message:
                        self.log(message)
        finally:
            if self.question:
                self.log("Chat connection unavailable.", "Status")
            self.worker_ready = False
            self.question = None
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
