"""Session-local evidence, rolling statistics and bounded SQLite persistence."""

from bisect import insort_right
from collections import Counter, deque
from dataclasses import asdict, dataclass
import json
import re
import sqlite3
from time import time
from uuid import uuid4

from agent.companion.diagnostic_io import readonly_database


@dataclass
class ToolObservation:
    session_id: str
    turn_id: str
    run_id: str
    agent_id: str
    model_id: str
    reasoning_effort: str
    tool_name: str
    tool_call_id: str
    started_at: float
    finished_at: float | None = None
    duration_ms: int | None = None
    outcome: str = "unknown"
    error_category: str = ""
    exit_code: int | None = None
    retry_of: str | None = None
    message_id: str = ""
    retry_basis: str = ""
    resumed_from_checkpoint: bool = False


def category(text, exit_code):
    text = (text[:4096] + ("\n" + text[-2048:] if len(text) > 4096 else "")).lower()
    for label, terms in (
        (
            "dependency",
            (
                "modulenotfounderror",
                "no module named",
                "command not found",
                "cannot find module",
            ),
        ),
        ("permission", ("permission denied", "operation not permitted")),
        ("assertion", ("assertionerror", "assertion failed", "assertion failure")),
        ("timeout", ("timeout", "timed out")),
        ("not_found", ("no such file", "not found")),
        ("network", ("connection refused", "connection reset")),
    ):
        if any(term in text for term in terms):
            return label
    if re.search(r"^e\s+assert\b", text, re.M):
        return "assertion"
    return "exit_code" if exit_code is not None else "tool_error"


TOOL_ACTIVITY_LIMIT = 60


def _completion_key(obs):
    return obs.finished_at, obs.run_id, obs.tool_call_id


class Series:
    def __init__(self, size):
        self.recent = deque(maxlen=size)
        self.counts = Counter()
        self.window_failed = 0
        self.points = deque(maxlen=180)
        self.last_failed = None

    @property
    def rate(self):
        return 100 * self.window_failed / len(self.recent) if self.recent else 0

    def add(self, obs):
        self.counts[obs.outcome] += 1
        if obs.outcome not in ("success", "failed"):
            return
        failed = obs.outcome == "failed"
        if len(self.recent) == self.recent.maxlen:
            self.window_failed -= self.recent[0]
        self.recent.append(int(failed))
        self.window_failed += int(failed)
        self.points.append((obs.finished_at, self.rate))
        self.last_failed = obs if failed else None

    def statistics(self):
        known = self.counts["success"] + self.counts["failed"]
        return dict(
            counts=dict(self.counts),
            recent_pct=self.rate,
            n=len(self.recent),
            cumulative_pct=100 * self.counts["failed"] / known if known else None,
        )


class Timeline:
    def __init__(self, session, window):
        self.session = session
        self.window = window
        self.finished = set()
        self.interrupted = {}
        self.active = {}
        self.retries = deque(maxlen=32)
        self.series = {}
        self.models = {}
        self.overall = Series(200)
        self.summaries = deque(maxlen=8)
        self.failures = deque(maxlen=32)
        self.samples = deque(maxlen=60)
        self.version = 0
        self.recent_tool_calls: list[ToolObservation] = []
        self.diagnostic_calls = deque(maxlen=128)
        self.activity_version = 0
        self.pending = []
        self.queue = deque()

    def record(self, kind, data, *, key=None, stamp=None, turn=""):
        self.version += 1
        self.pending.append(
            (
                self.session,
                kind,
                key or uuid4().hex,
                time() if stamp is None else stamp,
                turn,
                data,
            )
        )

    def started(self, obs):
        identity = (obs.run_id, obs.tool_call_id)
        if identity not in self.finished and identity not in self.active:
            self.active[identity] = obs

    def chart_tools(self, group):
        active = Counter(
            obs.tool_name for identity, obs in self.active.items()
            if (obs.model_id, obs.reasoning_effort) == group and identity not in self.interrupted
        )
        for (model, effort, name), series in self.series.items():
            if (model, effort) == group:
                count = series.counts.total() + active.pop(name, 0)
                if count:
                    yield name, series.rate if series.recent else None, count
        for name, count in active.items():
            yield name, None, count

    def completed(self, obs, *, replay=False):
        identity = obs.run_id, obs.tool_call_id
        if identity in self.finished:
            return
        self.finished.add(identity)
        self.active.pop(identity, None)
        self.diagnostic_calls.append(obs)
        group = (obs.model_id, obs.reasoning_effort)
        key = (*group, obs.tool_name)
        if key not in self.series:
            self.series[key] = Series(self.window)
        if group not in self.models:
            self.models[group] = Series(self.window)
        series, aggregate = self.series[key], self.models[group]
        if identity in self.interrupted:
            del self.interrupted[identity]
            series.counts["unknown"] -= 1
            aggregate.counts["unknown"] -= 1
            self.overall.counts["unknown"] -= 1
        if obs.retry_of and obs.retry_basis:
            self.retries.append(
                dict(
                    tool=obs.tool_name,
                    call=obs.tool_call_id,
                    previous=obs.retry_of,
                    category=obs.error_category,
                    outcome=obs.outcome,
                    run_id=obs.run_id,
                    turn_id=obs.turn_id,
                    basis=obs.retry_basis,
                )
            )
        if obs.finished_at is not None and obs.outcome in ("success", "failed"):
            calls = self.recent_tool_calls
            if len(calls) < TOOL_ACTIVITY_LIMIT or _completion_key(obs) > _completion_key(calls[0]):
                insort_right(calls, obs, key=_completion_key)
                if len(calls) > TOOL_ACTIVITY_LIMIT:
                    del calls[0]
                self.activity_version += 1
        series.add(obs)
        aggregate.add(obs)
        self.overall.add(obs)
        if obs.outcome == "failed":
            self.failures.append(asdict(obs))
        if replay:
            return
        self.record(
            "window",
            dict(
                model=group[0],
                effort=group[1],
                tool=obs.tool_name,
                **series.statistics(),
            ),
            key=obs.run_id + "/" + obs.tool_call_id,
            stamp=obs.finished_at,
            turn=obs.turn_id,
        )

    def restore(self, rows):
        for kind, stamp, data in rows:
            self.version += 1
            if kind == "tool":
                obs = ToolObservation(**data)
                if obs.finished_at is None:
                    # Interrupted after a committed start. Unknown is excluded from rates.
                    key = (obs.run_id, obs.tool_call_id)
                    if key not in self.finished and key not in self.interrupted:
                        self.interrupted[key] = obs
                        self.overall.counts["unknown"] += 1
                        group = (obs.model_id, obs.reasoning_effort)
                        self.series.setdefault(
                            (*group, obs.tool_name), Series(self.window)
                        ).counts["unknown"] += 1
                        self.models.setdefault(group, Series(self.window)).counts[
                            "unknown"
                        ] += 1
                else:
                    self.completed(obs, replay=True)
            elif kind == "turn":
                self.summaries.append(data)
            elif kind in ("resources", "git", "services", "profile"):
                self.samples.append((stamp, kind, data))

    def merge_live_events(self, events):
        """Replay the small arrival batch against restored history before its first commit."""
        for row in events:
            _, kind, _, stamp, _, data = row
            if kind in ("window", "incident"):
                # Window rows are statistics already stored on the series.
                continue
            if kind == "tool" and data["finished_at"] is not None:
                self.completed(ToolObservation(**data))
            elif kind == "turn":
                self.summaries.append(data)
            elif kind != "tool":
                self.restore([(kind, stamp, data)])
            self.pending.append(row)
            self.version += 1

    def sample(self, kind, data, turn):
        stamp = time()
        self.record(kind, data, stamp=stamp, turn=turn)
        self.samples.append((stamp, kind, data))


def transact(path, batch, session=None, prune=False):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=2)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS timeline(
                session TEXT, kind TEXT, id TEXT, stamp REAL, turn TEXT, payload TEXT,
                PRIMARY KEY(session,kind,id))""")
            db.execute("CREATE INDEX IF NOT EXISTS timeline_time ON timeline(stamp)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS timeline_session_time ON timeline(session,stamp)"
            )
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                legacy = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='calls'"
                ).fetchone()
                if legacy:
                    # The old schema has a commit timestamp, outcome and run, but no effort or duration.
                    db.execute(
                        """INSERT OR IGNORE INTO timeline SELECT session,'tool',run||'/'||call,stamp,run,
                        json_object('session_id',session,'turn_id',run,'run_id',run,'agent_id','main',
                        'model_id',model,'reasoning_effort','unknown','tool_name',tool,'tool_call_id',call,
                        'started_at',stamp,'finished_at',stamp,'outcome',outcome)
                        FROM calls WHERE stamp >= ?""",
                        (time() - 14 * 86400,),
                    )
                db.execute("PRAGMA user_version=1")
            db.executemany(
                """INSERT INTO timeline VALUES(?,?,?,?,?,?)
                ON CONFLICT(session,kind,id) DO UPDATE SET stamp=excluded.stamp,payload=excluded.payload
                WHERE excluded.stamp >= timeline.stamp AND
                (timeline.kind != 'tool' OR json_extract(timeline.payload,'$.finished_at') IS NULL OR
                 json_extract(excluded.payload,'$.finished_at') IS NOT NULL)""",
                [
                    (s, k, i, t, u, json.dumps(d, ensure_ascii=False))
                    for s, k, i, t, u, d in batch
                ],
            )
            if prune:
                db.execute(
                    "DELETE FROM timeline WHERE stamp < ?", (time() - 14 * 86400,)
                )
                db.execute("""DELETE FROM timeline WHERE rowid IN
                    (SELECT rowid FROM timeline ORDER BY stamp DESC LIMIT -1 OFFSET 100000)""")
        if session is not None:
            rows = db.execute(
                """SELECT kind,stamp,payload FROM timeline WHERE session=?
                    AND kind IN ('tool','incident','resources','services','git','turn','profile') ORDER BY stamp,rowid""",
                (session,),
            )
            return [(k, t, json.loads(d)) for k, t, d in rows]
    finally:
        db.close()


def restore_session(path, session, window):
    state = Timeline(session, window)
    state.restore(transact(path, [], session))
    return state


def timeline_window(path, session, kind, start, end, budget=None, *,
                    tool_name=None, turn_id=None, event_type=None, limit=16):
    """Read a bounded diagnostic window for the host-selected session."""
    with readonly_database(path, budget) as db:
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 256 * 1024)
        query = "SELECT stamp,kind,payload FROM timeline WHERE session=? AND stamp BETWEEN ? AND ?"
        args = [session, start, end]
        if kind == "git":
            query += " AND kind='git'"
        elif event_type is not None:
            if event_type not in {"tool", "turn", "run", "analysis", "git"}:
                raise ValueError("Unsupported diagnostic event type")
            query += " AND kind=?"
            args.append(event_type)
        else:
            query += " AND kind IN ('tool','turn')"
        if tool_name:
            query += " AND kind='tool' AND json_extract(payload,'$.tool_name')=?"
            args.append(tool_name)
        if turn_id:
            query += " AND turn=?"
            args.append(turn_id)
        rows = list(db.execute(query + " ORDER BY stamp DESC LIMIT ?", [*args, limit + 1]))
        records = []
        for stamp, kind, data in rows[:limit]:
            data = json.loads(data)
            if kind == "turn":
                data = {key: data[key] for key in ("turn", "run", "query", "outcome", "duration_ms") if key in data}
            elif kind == "tool":
                data.pop("session_id", None)
                data.pop("message_id", None)
                if not data.pop("retry_basis", ""):
                    data.pop("retry_of", None)
            records.append((stamp, kind, data))
        return {"records": records, "truncated": len(rows) > limit}
