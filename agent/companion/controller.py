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
import re
import sqlite3
import sys
from time import monotonic, perf_counter, time
from uuid import uuid4

from agent.companion import STATES
from agent.companion.config import CompanionConfig
from agent.companion.diagnostic_io import QueryBudget
from agent.companion.timeline import (
    SESSION_RELIABILITY,
    Timeline,
    ToolObservation,
    category,
    restore_session,
    timeline_window,
    transact,
)
from agent.companion.repository import Repository
from agent.companion.redaction import redact_diagnostic_text
from agent.companion.session_inspector import (
    CALL_CACHE_LIMIT, CallDetails, call_arguments, call_result, diagnostic_error, format_inspect_response,
    observation_views, result_status,
)
from agent.companion.diagnosis import InvalidObservation, interpret_turn, reply_text, validate_observation
from agent.companion.monitor import Resources, services


REPLY_TICK_SECONDS = 0.25
QUESTION_LIMIT = 4000
CONVERSATION_LIMIT = 4000
_REPLY_ERRORS = {
    "request_timeout": "这次问答已超时，可以重新提问。",
    "model_timeout": "模型响应超时，可以重新提问。",
    "model_connection_error": "无法连接玲音的模型，可以重新提问。",
    "model_error": "玲音的模型请求出错，可以重新提问。",
    "recursion_limit": "玲音达到本轮工具调用上限，可以缩小问题范围再问。",
    "ipc_error": "玲音的进程通信出错，可以重新提问。",
}


def _frames(*key, path=None):
    # Import NumPy and decode the first GIF on the background worker.
    from agent.companion.animation import frames

    yield from frames(*key, path=path)


def clean(value):
    return "".join(c for c in str(value)[:512] if c.isprintable())[:240]


async def start_worker(config_dir, config: CompanionConfig, api_key=None):
    # Isolated Python ignores user site packages and PYTHONPATH. Inject only
    # the install root so the same entry point also works in a source checkout.
    root = str(Path(__file__).resolve().parents[2])
    bootstrap = (
        "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
        "runpy.run_module('agent.companion.worker',run_name='__main__')"
    )
    env = {
        k: os.environ[k]
        for k in ("HOME", "PATH", "LANG")
        if k in os.environ
    }
    if api_key:
        env["DEEPSEEK_API_KEY"] = api_key
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-B",
        "-c",
        bootstrap,
        root,
        str(config_dir),
        str(config.persona_prompt),
        str(config.observation_prompt),
        str(config.chat_prompt),
        str(config.rpc_timeout_seconds),
        str(config.question_timeout_seconds),
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
        api_key=None,
        profile=None,
        window=None,
        execute_environment=None,
        config: CompanionConfig | None = None,
        inspect_session=None,
    ):
        self.workspace = workspace.resolve()
        self.protected = (config_dir, *protected)
        self.api_key = api_key
        from agent.redaction import SecretRef, secret_values

        refs: list[SecretRef] = []
        for item in secrets:
            if isinstance(item, SecretRef):
                if item.value:
                    refs.append(item)
            elif item:
                refs.append(SecretRef(name="secret", value=str(item)))
        if api_key:
            refs.append(SecretRef(name="llm.models.deepseek.api_key", value=str(api_key)))
        self.secrets = tuple(refs)
        self._secret_values = secret_values(self.secrets)
        self.repository = Repository(self.workspace, self.protected, self.secrets)
        self.inspect_session = inspect_session
        self.worker_ready = False
        self.question = None
        self.question_deadline = 0.0
        self.active_request: asyncio.Task | None = None
        self.reply_stamp = None
        self.reply_preview = ""
        self.reply_wait_started = 0.0
        self.reply_refresh = None
        self.errors = deque(maxlen=10)
        self.session_generation = 0
        self.session = session
        self.geometry = geometry
        self.invalidate = invalidate
        self.timezone = timezone
        self.execute_environment = execute_environment
        workspace_id = sha256(os.fsencode(workspace.resolve())).hexdigest()[:24]
        self.config_dir = config_dir
        self.config = config or CompanionConfig()
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
        self.call_cache = OrderedDict()
        self.pending = []
        self.storage_lock = asyncio.Lock()
        self.batches = {}
        self.behavior = "idle"
        self.until = 0
        self.behavior_queue: deque[tuple[str, bool]] = deque()
        self.connection = "STARTING"
        self.metrics = (None, None, None, None)
        self.services = ()
        self.logs = deque(maxlen=240)
        self.log_sequence = 0
        self.anchor = None
        self.frame = []
        self.changed = asyncio.Event()
        if "COMPANION_DEEPSEEK_API_KEY" in os.environ or (config_dir / "companion.key").exists():
            import warnings
            warnings.warn("旧 Companion 凭据已停用，请配置 llm.models.deepseek.api_key", UserWarning, stacklevel=2)

    def ask(self, text):
        self.sync_session()
        if self.question:
            return "上一问还在看。"
        if self.connection == "OFFLINE" and not self.worker_ready:
            return "玲音离线。请检查 Companion 密钥和连接。"
        text = text.strip()
        if not text or len(text) > QUESTION_LIMIT:
            return f"请输入问题，最多 {QUESTION_LIMIT} 字。"
        self.question = text
        self.question_deadline = perf_counter() + self.config.question_timeout_seconds
        self.reply_wait_started = monotonic()
        self._schedule_reply_refresh()
        self.anchor = None
        self.log(text, speaker="You")
        self.changed.set()
        return "已发给玲音。"

    def _schedule_reply_refresh(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.reply_refresh = loop.call_later(REPLY_TICK_SECONDS, self._refresh_waiting_reply)

    def _refresh_waiting_reply(self):
        self.reply_refresh = None
        if self.question and self.reply_stamp is None:
            self.invalidate()
            self._schedule_reply_refresh()

    def _stop_reply_refresh(self):
        if self.reply_refresh is not None:
            self.reply_refresh.cancel()
            self.reply_refresh = None

    def chat_preview(self):
        if not self.question:
            return None
        if self.reply_stamp is None:
            ticks = int((monotonic() - self.reply_wait_started) / REPLY_TICK_SECONDS)
            return "·" * (1 + ticks % 3), "Lain", None
        return self.reply_stamp, "Lain", self.reply_preview or None

    def _update_chat_preview(self, text):
        text = redact_diagnostic_text(text, self.secrets)
        text = reply_text("".join(c for c in text if c.isprintable() or c == "\n"), partial=True)
        if self.reply_stamp is None:
            if not text:
                return
            self.reply_stamp = datetime.now(self.timezone).strftime("%H:%M:%S")
            self._stop_reply_refresh()
        if text != self.reply_preview:
            self.reply_preview = text
            self.invalidate()

    def _clear_chat_reply(self):
        stamp = self.reply_stamp
        self.question = self.reply_stamp = None
        self.reply_preview = ""
        self._stop_reply_refresh()
        return stamp

    def log(self, message, speaker="Lain", *, stamp=None):
        self.log_sequence += 1
        limit = QUESTION_LIMIT if speaker == "You" else 1600
        if stamp is None:
            now = datetime.now(self.timezone)
            # Status uses full wall time for app-log style rows in the panel.
            stamp = now.strftime("%Y-%m-%d %H:%M:%S" if speaker == "Status" else "%H:%M:%S")
        self.logs.append(
            (
                stamp,
                speaker,
                "".join(c for c in str(message) if c.isprintable() or c in "\n\t")[:limit],
            )
        )
        self.invalidate()

    def chat_history(self, question):
        remaining = CONVERSATION_LIMIT
        history = []
        current = "".join(c for c in question if c.isprintable() or c in "\n\t")
        for _, role, message in reversed(self.logs):
            if role not in ("You", "Lain"):
                continue
            if role == "You" and message == current:
                current = None
                continue
            safe = redact_diagnostic_text(message, self.secrets)
            remaining -= len(role) + 1
            if remaining <= 0:
                break
            if len(safe) > remaining:
                history.append((role, safe[:remaining - 1].rstrip() + "…"))
                break
            history.append((role, safe))
            remaining -= len(safe)
        return list(reversed(history))

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
            if self.active_request is not None:
                self.active_request.cancel()
            self.call_cache.clear()
            self.starts.clear()
            self.errors.clear()
            self._clear_chat_reply()
            self.changed.clear()
            self.logs.clear()
            self.log_sequence += 1
            self.anchor = None
            self.behavior = "idle"
            self.until = 0
            self.behavior_queue.clear()
            self.batches.clear()
            self.run = uuid4().hex
            self.turn = self.run
            self.services = ()

    def close_call(self, obs, outcome, wall, tick, error="", exit_code=None):
        if obs.finished_at is not None:
            return
        obs.outcome = outcome
        obs.finished_at = wall
        start = self.starts.pop((obs.run_id, obs.tool_call_id), None)
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
            if obs.outcome == "waiting" and outcome != "cancelled":
                continue
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
        summary["findings"] = interpret_turn(
            summary, self.timeline.failures, self.timeline.samples
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
        run = event.run_id or self.run
        identity = run, event.tool_call_id
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
            self.queue_behavior("focused")
        elif kind in ("tool_started", "tool_resumed") and event.tool_call_id:
            if event.checkpoint_replay or identity in self.timeline.active or identity in self.timeline.finished:
                return
            if kind == "tool_resumed":
                resumed = self.resumable_call(event)
                if resumed is not None:
                    identity = resumed.run_id, resumed.tool_call_id
                    resumed.outcome = "running"
                    self.timeline.started(resumed)
                    self.calls[identity] = resumed
                    self.starts[identity] = tick
                    self.cache_call(identity, call_arguments(event.arguments, self.secrets))
                    self.invalidate()
                    return
            name = clean(event.name)
            obs = ToolObservation(
                self.thread,
                self.turn,
                run,
                agent,
                model,
                effort,
                name,
                event.tool_call_id,
                wall,
                message_id=event.message_id,
                resumed_from_checkpoint=kind == "tool_resumed",
            )
            series = self.timeline.series.get((model, effort, name))
            previous = series.last_failed if series else None
            details = call_arguments(event.arguments, self.secrets)
            if (
                previous
                and previous.run_id == run
                and previous.turn_id == obs.turn_id
                and previous.agent_id == agent
                and 0 <= wall - previous.finished_at <= 60
                and details.matches_arguments(self.call_cache.get((run, previous.tool_call_id), CallDetails()))
            ):
                obs.retry_of = previous.tool_call_id
                obs.retry_basis = "matching_visible_arguments"
            self.calls[identity] = obs
            self.starts[identity] = tick
            self.cache_call(identity, details)
            self.timeline.started(obs)
            self.timeline.record(
                "tool",
                asdict(obs),
                key=run + "/" + obs.tool_call_id,
                stamp=wall,
                turn=self.turn,
            )
            if event.message_id:
                self.batches.setdefault(event.message_id, []).append(name)
            self.queue_behavior("observing")
        elif kind == "tool_completed" and event.tool_call_id:
            if identity in self.timeline.finished:
                return
            obs = self.timeline.active.get(identity)
            if obs is None:
                obs = self.resumable_call(event)
            if obs is None:
                obs = ToolObservation(self.thread, self.turn, run, agent, model, effort,
                                      clean(event.name) or "tool", event.tool_call_id, wall,
                                      message_id=event.message_id, resumed_from_checkpoint=bool(event.message_id))
            if obs.finished_at is not None:
                return
            artifact = event.artifact if isinstance(event.artifact, dict) else {}
            code = artifact.get("exit_code")
            code = code if type(code) is int else None
            content = event.content or (event.result if isinstance(event.result, str) else "")
            outcome = result_status(
                "completed", name=obs.tool_name, content=content, artifact=artifact,
                is_error=event.is_error or code not in (None, 0),
            )
            error = category(content, code) if outcome == "failed" else ""
            identity = obs.run_id, obs.tool_call_id
            self.calls[identity] = obs
            details = call_result(self.call_cache.get(identity, CallDetails()), content, artifact, self.secrets,
                                  status=outcome)
            self.cache_call(identity, details)
            self.close_call(obs, outcome, wall, tick, error, code)
            if outcome == "failed":
                self.errors.append(
                    (
                        obs.tool_name,
                        clean(details.output_excerpt),
                    )
                )
        elif kind == "turn_completed":
            self.close_turn("completed", wall, tick)
        elif kind == "interaction_requested":
            pending = {call.get("toolCallId") for call in getattr(event.result, "pending_tool_calls", ())}
            for obs in self.timeline.active.values():
                if obs.run_id == run and (obs.tool_call_id in pending or (
                    obs.tool_name == "request_human_input" and getattr(event.result, "status", None) == "waiting_human"
                )):
                    obs.outcome = "waiting"
            self.queue_behavior("curious")
        elif kind in ("run_failed", "run_completed", "run_cancelled"):
            behavior = {
                "run_failed": "concerned",
                "run_completed": "relieved",
                "run_cancelled": "idle",
            }[kind]
            self.queue_behavior(behavior, temporary=behavior != "idle")
            self.close_turn(kind.removeprefix("run_"), wall, tick)
        self.invalidate()

    def resumable_call(self, event):
        pending = self.timeline.interrupted | {
            key: obs for key, obs in self.timeline.active.items()
            if obs.outcome == "waiting" or event.message_id and obs.message_id == event.message_id
        }
        matches = [obs for obs in pending.values() if obs.tool_call_id == event.tool_call_id]
        if event.message_id:
            exact = [obs for obs in matches if obs.message_id == event.message_id]
            matches = exact or [obs for obs in matches if not obs.message_id]
        return matches[0] if len(matches) == 1 else None

    def cache_call(self, identity, details):
        self.call_cache[identity] = details
        self.call_cache.move_to_end(identity)
        if len(self.call_cache) > CALL_CACHE_LIMIT:
            self.call_cache.popitem(last=False)

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
        if event["phase"] == "tool_start":
            self.timeline.started(obs)
        else:
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
                self.calls[run, call] = obs
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
                for obs in state.active.values():
                    combined.started(obs)
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
                old = {(row.host_pid, row.started): row for row in self.services}
                for row in current:
                    if old.pop((row.host_pid, row.started), None) is None:
                        self.log(f"服务已启动：{row.name}（PID {row.pid}）", "Status")
                for row in old.values():
                    self.log(f"服务已停止：{row.name}（PID {row.pid}）", "Status")
                self.services = current
                self.timeline.sample(
                    "services",
                    [(r.name, r.pid) for r in current],
                    self.turn if self.turn_start is not None else "",
                )
            self.invalidate()
            await asyncio.sleep(2)

    def queue_behavior(self, behavior: str, *, temporary: bool = False) -> None:
        # Repeated tool events often ask for the same expression.
        if self.behavior_queue and self.behavior_queue[-1][0] == behavior:
            self.behavior_queue[-1] = (behavior, temporary)
        elif not self.behavior_queue and self.behavior == behavior:
            self.until = monotonic() + 4.8 if temporary else 0
        else:
            self.behavior_queue.append((behavior, temporary))

    def _advance_behavior(self) -> bool:
        if self.behavior_queue:
            self.behavior, temporary = self.behavior_queue.popleft()
            self.until = monotonic() + 4.8 if temporary else 0
            return True
        if self.until and monotonic() >= self.until:
            self.behavior, self.until = "idle", 0
            return True
        return False

    async def animate(self):
        clips = OrderedDict()
        key = iterator = None
        cache = []
        index = 0
        while True:
            width, height = self.geometry()
            if key is None or not width:
                self._advance_behavior()
            current = self.behavior, width, height
            generation = self.session_generation
            if current != key:
                if key and iterator is not None:
                    clips[key] = (iterator, cache)
                    if len(clips) > 3:
                        old, _ = clips.popitem(last=False)[1]
                        old.close()
                # Keep the last frame on screen while the next clip decodes.
                iterator, cache = clips.pop(current, (None, []))
                key, index = current, 0
            delay = 0.2
            if width:
                try:
                    if iterator is None:
                        iterator = _frames(*current, path=self.config.gifs.get(self.behavior))
                    if index == len(cache):
                        item = await asyncio.to_thread(next, iterator, None)
                        if item is not None:
                            cache.append(item)
                        if (
                            generation != self.session_generation
                            or (self.behavior, *self.geometry()) != current
                        ):
                            continue
                        if item is None:
                            # The last frame has already had its full display delay.
                            if not cache:
                                raise ValueError("Empty companion animation")
                            if self._advance_behavior():
                                continue
                            index = 0
                    self.frame, delay = cache[index]
                    self.invalidate()
                    index += 1
                except Exception:
                    if (
                        generation != self.session_generation
                        or (self.behavior, *self.geometry()) != current
                    ):
                        continue
                    if self._advance_behavior():
                        continue
                    self.frame = [("#777777", "Animation unavailable")]
                    self.invalidate()
                    delay = 1
            await asyncio.sleep(max(0.04, delay))

    def session_evidence(self, kind, *, tool_name=None, turn_id=None):
        state = self.timeline

        def selected(name, turn):
            return (not tool_name or name == tool_name) and (not turn_id or turn == turn_id)

        if kind == "statistics" and not turn_id:
            rows = [
                dict(model=m, effort=e, tool=t, **series.statistics())
                for (m, e, t), series in state.series.items()
                if (m, e) == self.profile() and selected(t, None)
            ]
            return dict(
                overall=state.overall.statistics() if not tool_name else None,
                current_profile=self.profile(),
                records=sorted(rows, key=lambda r: r["counts"].get("failed", 0), reverse=True),
            )
        if kind == "turns":
            return [
                dict(
                    turn_id=summary["turn"],
                    outcome=summary["outcome"],
                    duration_ms=summary["duration_ms"],
                    previous_turn_ms=summary["previous_turn_ms"][-3:],
                    findings=(summary.get("findings") or interpret_turn(summary, state.failures, state.samples))[:8],
                )
                for summary in state.summaries
                if (not turn_id or turn_id == summary["turn"])
                and (not tool_name or any(row["tool"] == tool_name for row in summary["tools"]))
            ]

        observations = observation_views(state, cache=self.call_cache)
        if kind == "retries":
            records = []
            for row in state.retries:
                if selected(row["tool"], row["turn_id"]):
                    prior = observations.get((row["run_id"], row["previous"]))
                    records.append(dict(
                        **row, previous_status=prior.status if prior else None,
                        previous_category=prior.error_category if prior else None,
                    ))
            return records

        ordered = sorted(observations.values(), key=lambda row: row.started_at or 0)
        calls = [row for row in ordered if selected(row.name, row.turn_id)]
        if kind == "statistics":
            counts = {}
            for row in calls:
                counts.setdefault(row.name, Counter())[row.status] += 1
            return dict(records=[dict(tool=name, counts=dict(values)) for name, values in counts.items()],
                        evidence_window="retained_calls")

        prior_categories = {}
        records = []
        for call in calls:
            if kind == "failures" and call.status != "failed":
                continue
            row = call.row()
            if call.status == "failed":
                key = call.turn_id, call.name
                previous = prior_categories.get(key)
                if previous is not None and previous != call.error_category:
                    row["previous_error_category"] = previous
                prior_categories[key] = call.error_category
            records.append(row)
        return records

    def chat_tool_evidence(self, question):
        mentioned = set(re.findall(r"[a-z0-9_]+(?:[.-][a-z0-9_]+)*", question.casefold()))
        failures = self.session_evidence("failures")
        relevant = [row for row in failures if row["name"].casefold() in mentioned]
        return [
            {key: row[key] for key in ("name", "run_id", "turn_id", "tool_call_id", "error_category")}
            for row in relevant[-4:]
        ]

    async def host_query(self, kind, args):
        self.sync_session()
        thread, generation = self.thread, self.session_generation
        budget = QueryBudget(self.config.rpc_timeout_seconds)
        try:
            async with asyncio.timeout(self.config.rpc_timeout_seconds):
                result = await self._host_query(kind, args, thread, budget)
            if self.session() != thread or generation != self.session_generation:
                result = diagnostic_error("request_expired", "The active session changed.")
        except TimeoutError:
            result = diagnostic_error("tool_timeout", "Host read-only query timed out.")
        except (OSError, ValueError, TypeError, sqlite3.Error):
            result = diagnostic_error("query_failed", "Host read-only query failed.")
        finally:
            budget.cancelled.set()
        return result if isinstance(result, str) else format_inspect_response(result)

    async def _host_query(self, kind, args, thread, budget):
        if not isinstance(kind, str) or not isinstance(args, dict):
            return diagnostic_error("invalid_query", "Query arguments must be an object.")
        if kind == "inspect_current_session":
            if self.inspect_session is None:
                return diagnostic_error("inspector_unavailable", "Session inspector unavailable.")
            inspect = self.inspect_session(
                view=str(args.get("view") or "recent_tools"),
                tool_call_id=args.get("tool_call_id"), run_id=args.get("run_id"),
                limit=args.get("limit", 10), status=args.get("status"), order=args.get("order", "asc"),
            )
            return await asyncio.to_thread(inspect, budget)
        if kind in ("timeline", "git"):
            end = min(float(args.get("end") or time()), time())
            start = max(float(args.get("start") or end - 14 * 86400), end - 14 * 86400)
            return await asyncio.to_thread(
                timeline_window, self.database, thread, kind, start, end, budget,
                tool_name=args.get("tool_name"), turn_id=args.get("turn_id"),
                event_type=args.get("event_type"),
                limit=max(1, min(20, int(args.get("limit") or 16))),
            )
        if kind in ("search", "files"):
            return await asyncio.to_thread(
                self.repository.search, str(args.get("term", ""))[:160],
                directory=str(args.get("directory") or ""), paths_only=kind == "files", budget=budget,
            )
        if kind == "read":
            return await asyncio.to_thread(
                self.repository.read, str(args.get("path", ""))[:512],
                int(args.get("start", 1)), min(self.config.source_read_lines,
                                               int(args.get("limit") or self.config.source_read_lines)),
                budget=budget,
            )
        if kind not in {"statistics", "failures", "retries", "sequence", "turns"}:
            return diagnostic_error("unknown_tool", "Unknown host query.")
        limit = max(1, min(20, int(args.get("limit") or 10)))
        evidence = self.session_evidence(kind, tool_name=args.get("tool_name"), turn_id=args.get("turn_id"))
        if isinstance(evidence, list):
            response = {"records": evidence[-limit:], "order": "asc", "truncated": len(evidence) > limit}
            if kind in {"failures", "retries", "sequence"}:
                response["detail_view"] = "inspect_current_session(view=tool_detail, tool_call_id, run_id)"
            return response
        rows = evidence["records"]
        return {**evidence, "records": rows[:limit], "truncated": len(rows) > limit}

    async def observe(self):
        failures = 0
        while failures < 3:
            if failures:
                await asyncio.sleep(2 ** (failures - 1))
            try:
                status = await self.worker_session()
            except (OSError, ValueError, TimeoutError, sqlite3.Error):
                self.log("玲音的连接中断了。", "Status")
                status = None
            if status in ("no key", "prompt error"):
                return
            if status in ("request_expired", "request_timeout"):
                failures = 0
            else:
                failures += 1
        self.log("玲音离线，本地监测继续。", "Status")

    async def _exchange(self, process, identity, snapshot, current):
        async def send(payload):
            process.stdin.write(
                json.dumps(dict(**identity, **payload), ensure_ascii=False).encode() + b"\n"
            )
            await process.stdin.drain()

        if self.execute_environment is not None:
            environment = await asyncio.to_thread(self.execute_environment)
            if environment:
                snapshot["execute_environment"] = environment
        await send({"snapshot": snapshot})
        question = "question" in snapshot
        while True:
            reply = json.loads(await process.stdout.readline())
            if any(reply.get(k) != v for k, v in identity.items()):
                raise ValueError("mismatched companion response")
            if "partial" in reply:
                if question and current() and isinstance(reply["partial"], str):
                    self._update_chat_preview(reply["partial"])
                continue
            if "query" not in reply:
                return reply
            data = diagnostic_error("request_expired", "The active session changed.")
            if current():
                if question:
                    data = await self.host_query(reply["query"], reply.get("args") or {})
                else:
                    data = diagnostic_error("forbidden_tool", "Observation cannot query the host.")
            if not current():
                data = diagnostic_error("request_expired", "The active session changed.")
            await send(dict(
                query_id=reply.get("query_id"),
                data=data if isinstance(data, str) else format_inspect_response(data),
            ))

    async def worker_session(self):
        process = None
        restart = False
        spawn = asyncio.create_task(start_worker(self.config_dir, self.config, self.api_key))
        try:
            process = await asyncio.shield(spawn)
            hello = json.loads(await asyncio.wait_for(process.stdout.readline(), 30))
            if hello.get("status") == "no key":
                self.log("未配置 Companion 密钥，本地监测继续。", "Status")
                self._clear_chat_reply()
                return "no key"
            if hello.get("status") == "prompt error":
                self.log(f"无法读取玲音提示词：{clean(hello.get('path', ''))}。", "Status")
                self._clear_chat_reply()
                return "prompt error"
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
                question = self.question
                state = self.timeline
                generation = self.session_generation
                incident = None
                if question:
                    snapshot = dict(
                        question=redact_diagnostic_text(question, self.secrets),
                        snapshot_version=state.version,
                        profile=self.profile(),
                        tool_evidence=self.chat_tool_evidence(question),
                        recent_errors=list(self.errors),
                        latest_review=self.session_evidence("turns")[-1:],
                        conversation=self.chat_history(question),
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
                            if (
                                key == SESSION_RELIABILITY
                                or (f["model_id"], f["reasoning_effort"]) == key[:2]
                            )
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
                        and (not incident or self.timeline is state and state.incidents.get(key) is incident)
                    )

                timeout = self.config.question_timeout_seconds
                if question:
                    timeout = max(0, self.question_deadline - perf_counter())
                request = asyncio.create_task(self._exchange(process, identity, snapshot, current))
                self.active_request = request
                try:
                    async with asyncio.timeout(timeout):
                        reply = await request
                except TimeoutError:
                    if current() and question:
                        self._clear_chat_reply()
                        self.log(_REPLY_ERRORS["request_timeout"], "Status")
                    restart = True
                    return "request_timeout"
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    restart = True
                    return "request_expired"
                finally:
                    self.active_request = None
                self.sync_session()
                if not current():
                    continue
                if question:
                    stamp = self._clear_chat_reply()
                    if reply.get("status") == "ok":
                        self.log(reply_text(redact_diagnostic_text(str(reply.get("message") or ""), self.secrets)), stamp=stamp)
                    else:
                        self.log(_REPLY_ERRORS.get(reply.get("status"), _REPLY_ERRORS["model_error"]), "Status")
                else:
                    status = reply.get("status")
                    decision = None
                    if status == "ok":
                        try:
                            decision = validate_observation({field: reply.get(field) for field in ("behavior", "message")})
                        except InvalidObservation:
                            status = "invalid_observation"
                    message = reply_text(redact_diagnostic_text(decision["message"], self.secrets),
                                         proactive=True) if decision else ""
                    analysis = dict(
                        incident=incident_id,
                        version=version,
                        turn=incident["turn"],
                        status=status,
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
                    if decision:
                        self.queue_behavior(decision["behavior"], temporary=True)
                    if message:
                        self.log(message)
        finally:
            if not restart:
                if self.question:
                    self.log("玲音的 IPC 连接中断了。", "Status")
                self._clear_chat_reply()
            self.worker_ready = False
            self.connection = "STARTING" if restart else "OFFLINE"
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
