import asyncio
import json
import sqlite3

from agent.runtime.contracts import RunEvent
from tests.test_companion import companion


def test_timeline_identity_concurrent_calls_and_recovery(tmp_path):
    async def run():
        session = ["first"]
        c = companion(tmp_path, lambda: session[0])
        c.event(RunEvent("run_started"), "m", "low")
        for i, effort in [("a", "low"), ("b", "high")]:
            event = RunEvent(
                "tool_started",
                tool_call_id=i,
                name="exec",
                arguments={"secret": "secret_payload"},
            )
            c.event(event, "m", effort, tick=10)
            c.event(event, "wrong", tick=12)
        for i in ("b", "a", "b"):
            event = RunEvent(
                "tool_completed",
                tool_call_id=i,
                is_error=i == "b",
                artifact={"exit_code": int(i == "b")},
                content="secret_payload",
            )
            c.event(event, "wrong", tick=10.5)
        c.event(RunEvent("tool_started", tool_call_id="c", name="read"), "m")
        c.event(RunEvent("run_cancelled"), "m")
        await c.save_statistics()
        db = sqlite3.connect(c.database)
        assert db.execute("pragma journal_mode").fetchone()[0] == "wal"
        rows = [
            json.loads(r[0])
            for r in db.execute("select payload from timeline where kind='tool'")
        ]
        assert len(rows) == 3
        assert "secret_payload" not in json.dumps(rows)
        a, b, _ = sorted(rows, key=lambda r: r["tool_call_id"])
        assert a["duration_ms"] == b["duration_ms"] == 500
        assert (b["exit_code"], b["reasoning_effort"]) == (1, "high")
        assert len(c.timeline.summaries) == 1
        session[0] = "other"
        c.sync_session()
        await c.save_statistics()
        assert not c.timeline.series
        session[0] = "first"
        c.sync_session()
        await c.save_statistics()
        assert c.timeline.series["m", "high", "exec"].rate == 100
        assert not c.timeline.series["m", "default", "read"].recent

    asyncio.run(run())


def test_chart_filter_window_and_incident_cooldown(tmp_path):
    from agent.companion.chart import FailureChart
    from agent.companion.timeline import ToolObservation, Timeline

    c = companion(tmp_path, lambda: "s")
    c.timeline = Timeline("s", 20)
    chart = FailureChart(c)

    def add(i, failed, tool="read"):
        obs = ToolObservation(
            "s",
            "t",
            "r",
            "main",
            "fixed model",
            "default",
            tool,
            tool + str(i),
            i,
            i,
            1,
            "failed" if failed else "success",
            "dependency" if failed else "",
        )
        c.timeline.completed(obs)

    for i in range(20):
        add(i, i < 3)
    assert c.timeline.series["fixed model", "default", "read"].rate == 15
    assert "15% n=20" in str(chart.draw(38, 6))
    add(20, False)
    assert "No elevated" in str(chart.draw(38, 5))
    assert len(c.timeline.queue) == 1
    for i in range(3):
        add(21 + i, True, "edit")
    assert "100% n=3" in str(chart.draw(38, 6))
    add(24, True, "edit")
    assert len(c.timeline.queue) == 2
    for i in range(4):
        add(i, True, "other" + str(i))
    chart.draw(38, 5)
    assert chart.pages == 2
    chart.scroll(1)
    assert "2/2" in str(chart.draw(38, 5))


def test_committed_start_survives_crash_and_replay(tmp_path):
    async def run():
        c = companion(tmp_path, lambda: "session")
        c.event(RunEvent("run_started", run_id="r", turn_id="t"), "m")
        c.event(RunEvent("tool_started", name="read", tool_call_id="a"), "m")
        await c.save_statistics()  # no orderly finish
        restored = companion(tmp_path, lambda: "session")
        await restored.save_statistics()
        series = restored.timeline.series["m", "default", "read"]
        assert series.counts["unknown"] == 1 and not series.recent
        for _ in range(2):
            restored.event(
                RunEvent("tool_completed", run_id="r", tool_call_id="a"), "other"
            )
        await restored.save_statistics()
        assert series.counts["success"] == 1 and series.counts["unknown"] == 0
        db = sqlite3.connect(c.database)
        obs = json.loads(
            db.execute("select payload from timeline where kind='tool'").fetchone()[0]
        )
        assert obs["duration_ms"] is None and obs["outcome"] == "success"

    asyncio.run(run())


def test_history_retention_is_bounded(tmp_path):
    from time import time
    from agent.companion.timeline import transact

    path = tmp_path / "e.db"
    stamp = time()
    transact(
        path,
        [
            ("s", "resources", "old", 0, "", {}),
            ("s", "resources", "recent", stamp, "", {}),
        ],
        prune=True,
    )
    assert len(transact(path, [], "s")) == 1


def test_worker_restart_limit_and_local_operation_without_key(tmp_path, monkeypatch):
    async def run():
        c = companion(tmp_path, lambda: "s")
        attempts = []

        async def broken():
            attempts.append(1)
            raise OSError("unavailable")

        waits = []

        async def delay(seconds):
            waits.append(seconds)

        monkeypatch.setattr(c, "worker_session", broken)
        monkeypatch.setattr("agent.companion.controller.asyncio.sleep", delay)
        await c.observe()
        assert len(attempts) == 3 and waits == [1, 2]
        c.event(RunEvent("tool_started", tool_call_id="a", name="read"), "m")
        c.event(RunEvent("tool_completed", tool_call_id="a", is_error=True), "m")
        await c.save_statistics()
        assert c.timeline.series["m", "default", "read"].rate == 100

    asyncio.run(run())


def test_focus_returns_on_resize_and_preserves_both_drafts():
    from deepagents.backends import StateBackend
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from agent.cli.app import CliApplication
    from agent.runtime.runner import AgentRunner
    from tests.conftest import scripted_model

    async def run():
        runner = AgentRunner(model=scripted_model([]), backend=StateBackend())
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app.application.output.get_size = lambda: Size(48, 180)
            with set_app(app.application):
                app.buffer.text = "main draft"
                app.application.layout.focus(app.lain_editor)
                app.lain_buffer.text = "chat draft"
                app.application.output.get_size = lambda: Size(24, 80)
                app.application.before_render.fire()
                assert app.application.layout.current_control is app.editor_control
                assert (app.buffer.text, app.lain_buffer.text) == (
                    "main draft",
                    "chat draft",
                )
        runner.close()

    asyncio.run(run())


def test_first_live_window_on_resume_uses_persisted_baseline(tmp_path):
    async def run():
        c = companion(tmp_path, lambda: "s")
        c.event(RunEvent("run_started"), "m")
        for i in range(20):
            c.event(RunEvent("tool_started", name="read", tool_call_id=str(i)), "m")
            c.event(RunEvent("tool_completed", tool_call_id=str(i)), "m")
        await c.finish()
        resumed = companion(tmp_path, lambda: "s")
        resumed.event(RunEvent("run_started"), "m")
        resumed.event(
            RunEvent("tool_started", name="read", tool_call_id="failure"), "m"
        )
        resumed.event(
            RunEvent("tool_completed", tool_call_id="failure", is_error=True), "m"
        )
        await resumed.finish()
        db = sqlite3.connect(c.database)
        row = db.execute(
            "select payload from timeline where kind='window' and id like '%/failure'"
        ).fetchone()
        assert json.loads(row[0])["recent_pct"] == 5
        assert resumed.timeline.summaries[-1]["tools"][0]["start_pct"] == 0

    asyncio.run(run())


def test_distributed_decline_with_no_visible_bars_survives_resume(tmp_path):
    from agent.companion.chart import FailureChart
    from agent.companion.timeline import (
        SESSION_RELIABILITY,
        ToolObservation,
        restore_session,
        transact,
    )
    from time import time

    c = companion(tmp_path, lambda: "s")
    stamp = time() - 1000
    call = 0
    # Interleave tools, spacing failures across the population. Each tool's
    # latest 20 calls contain two failures; every call contributes to overall.
    for step in range(40):
        for tool in range(20):
            failed = step >= 20 and (step + tool) % 10 == 0
            call += 1
            obs = ToolObservation(
                "s",
                "turn",
                "run",
                "main",
                "fixed model",
                "default",
                f"tool{tool}",
                str(call),
                stamp + call,
                stamp + call,
                1,
                "failed" if failed else "success",
                "tool_error" if failed else "",
            )
            c.timeline.completed(obs)
            c.timeline.record("tool", vars(obs), key=str(call), stamp=obs.finished_at)
    assert all(v.rate == 10 for v in c.timeline.series.values())
    assert "No elevated failure rates" in str(FailureChart(c).draw(45, 6))
    assert c.timeline.overall.rate == 10
    incident = c.timeline.incidents[SESSION_RELIABILITY]
    assert incident["state"] == "open" and incident["scope"] == "session"
    assert sum(key == SESSION_RELIABILITY for key, _, _ in c.timeline.queue) == 1
    transact(c.database, c.timeline.pending)
    restored = restore_session(c.database, "s", 20)
    assert restored.overall.statistics() == c.timeline.overall.statistics()
    assert restored.incidents[SESSION_RELIABILITY]["id"] == incident["id"]


def test_bars_sorted_across_pages_and_recover_only_after_clean_results(tmp_path):
    from agent.companion.chart import FailureChart
    from agent.companion.timeline import ToolObservation

    c = companion(tmp_path, lambda: "s")

    def add(tool, i, failed):
        obs = ToolObservation(
            "s",
            "t",
            "r",
            "main",
            "fixed model",
            "default",
            tool,
            tool + str(i),
            i,
            i,
            1,
            "failed" if failed else "success",
            "timeout" if failed else "",
        )
        c.timeline.completed(obs)

    for tool, count in (("edit", 3), ("execute", 7), ("read", 2), ("grep", 1)):
        for i in range(10):
            add(tool, i, i < count)
    chart = FailureChart(c)
    rows = chart.draw(25, 6)
    assert [s.strip() for _, s in rows[-2]] == ["execute", "edit"]
    assert [s.strip() for _, s in rows[-1]] == ["70% n=10", "30% n=10"]
    chart.scroll(1)
    assert [s.strip() for _, s in chart.draw(25, 6)[-2]] == ["read"]
    for i in range(3):
        add("probe", i, True)
    key = ("fixed model", "default", "probe")
    add("probe", 3, False)
    assert c.timeline.incidents[key]["state"] == "open"
    for i in range(4, 8):
        add("probe", i, False)
    assert c.timeline.incidents[key]["state"] == "resolved"


def test_turn_review_filters_evidence_and_voice():
    from agent.companion.diagnosis import interpret_turn, reply_text

    summary = dict(
        turn="t",
        tools=[],
        git=[],
        incidents=[],
        previous_turn_ms=[1000, 1100, 1200],
        duration_ms=6000,
    )
    summary["tools"] = [
        dict(
            tool="execute",
            duration_ms=4200,
            outcomes={"success": 2, "failed": 2},
            recent_pct=40,
            start_pct=10,
        )
    ]
    failures = [
        dict(
            turn_id="t",
            tool_name="execute",
            tool_call_id="a",
            retry_of=None,
            error_category="timeout",
            finished_at=100,
        ),
        dict(
            turn_id="t",
            tool_name="execute",
            tool_call_id="b",
            retry_of="a",
            error_category="timeout",
            finished_at=103,
        ),
    ]
    findings = " ".join(
        interpret_turn(
            summary, failures, [(101, "resources", {"percent": [10, None, 96, 20]})]
        )
    )
    assert all(term in findings for term in ("上升", "疑似重试", "RAM", "更慢"))
    assert "4200" not in findings and "Git" not in findings
    assert reply_text("Uncertain.", proactive=True) == ""
    assert "可靠" in reply_text("No evidence.")
    assert reply_text("先看依赖声明。") == "先看依赖声明。"
