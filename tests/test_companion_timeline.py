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


def test_heatmap_uses_tool_results_and_preserves_cumulative_statistics(tmp_path):
    from agent.companion.chart import ACTIVITY_CELLS, FAILED_CELL, ActivityHeatmap
    from agent.companion.timeline import Timeline, restore_session

    async def run():
        c = companion(tmp_path, lambda: "s")
        c.window = 2
        c.timeline = Timeline("s", c.window)
        chart = ActivityHeatmap(c)

        def event(kind, call, **kwargs):
            c.event(RunEvent(kind, name="read", tool_call_id=call, **kwargs), "fixed model")

        empty = chart.draw(30, 8)
        assert [row[0][1] for row in empty] == ["Tool Health", "No tool calls yet"]
        event("tool_started", "a")
        assert chart.draw(30, 8) is empty
        await c.save_statistics()
        assert chart.draw(30, 8) == empty
        event("tool_completed", "a", content="timeout was handled")
        assert chart.draw(30, 8)[2][-1] == ACTIVITY_CELLS[1]
        event("tool_started", "b")
        event("tool_completed", "b", artifact={"exit_code": 127})
        event("tool_started", "c")
        event("tool_completed", "c", artifact={"status": "cancelled"})
        event("tool_started", "d")
        event("tool_started", "d")
        populated = chart.draw(30, 8)
        assert populated[0][-1][1] == "50% success"
        assert populated[1][0][1] == "Last 2 calls"
        assert populated[2][-1] == FAILED_CELL
        assert populated[-1][0][1] == "2 calls · 1 success · 1 failed"
        assert len(c.timeline.series["fixed model", "default", "read"].recent) == 2
        await c.save_statistics()
        restored = restore_session(c.database, "s", c.window)
        assert list(restored.chart_tools(("fixed model", "default"))) == [("read", 50, 4)]
        assert [obs.tool_call_id for obs in restored.recent_tool_calls] == ["a", "b"]
        c.timeline = restored
        assert chart.draw(30, 8) == populated

    asyncio.run(run())


def _observe_tool(timeline, i, name, *, failed=False, model="fixed model"):
    from agent.companion.timeline import ToolObservation

    obs = ToolObservation(
        "s", "t", "r", "main", model, "default", name, str(i), i - 1, i, 1,
        "failed" if failed else "success", "timeout" if failed else "",
    )
    timeline.completed(obs)
    timeline.record("tool", vars(obs), key=f"r/{i}", stamp=i)


def test_heatmap_groups_top_tools_and_prioritizes_failures(tmp_path):
    from prompt_toolkit.utils import get_cwidth

    from agent.companion.chart import ACTIVITY_CELLS, FAILED_CELL, ActivityHeatmap

    c = companion(tmp_path, lambda: "s")
    long_name = "甲乙丙工具执行记录"
    groups = [
        ("read", "read", "read"),
        ("read", "read", "shell"),
        ("write", "write", "read"),
        (long_name, long_name, long_name),
        ("shell", "web", "extra"),
        ("shell", "read", "extra"),
        (long_name,),
    ]
    for i, name in enumerate(name for group in groups for name in group):
        _observe_tool(c.timeline, i, name, failed=i in (3, 4, 13, 14))
    chart = ActivityHeatmap(c)
    rows = chart.draw(30, 8)
    assert len(rows) == 8
    assert rows[0][-1][1] == "78.9% success"
    assert rows[1][0][1] == "Last 19 calls"
    assert all(sum(get_cwidth(text) for _, text in row) <= 30 for row in rows)
    matrix = rows[2:6]
    assert [row[0][1].strip() for row in matrix] == ["read", "甲乙丙…", "shell", "other"]
    assert len({get_cwidth(row[0][1]) for row in matrix}) == 1
    for width in (30, 47, 64):
        for row in chart.draw(width, 8)[2:6]:
            label = row[0][1]
            left = len(label) - len(label.lstrip())
            right = width - sum(get_cwidth(text) for _, text in row)
            assert abs(right - left) <= 1
    cells = [row[1::2] for row in matrix]
    assert cells[0][:3] == [ACTIVITY_CELLS[3], FAILED_CELL, ACTIVITY_CELLS[1]]
    assert cells[2][:2] == [ACTIVITY_CELLS[0], ACTIVITY_CELLS[1]]
    assert cells[3][2] == ACTIVITY_CELLS[2]
    assert cells[3][4:6] == [FAILED_CELL, ACTIVITY_CELLS[1]]
    assert all(len(row) == 7 for row in cells)
    assert all(set(row[2::2]) == {("", " ")} for row in matrix)
    assert all(get_cwidth(glyph) == 1 and glyph in "■×" for row in cells for _, glyph in row)
    legend = rows[-2]
    assert [cell for cell in legend if cell[1] == "■"] == [
        ("#FFFFFF", "■"), ("#FFD5EC", "■"), ("#F7BDD7", "■"), ("#F18AB6", "■"),
    ]
    assert "× Failed" in "".join(text for _, text in legend)
    assert rows[-1][0][1] == "19 calls·15 success·4 failed"


def test_heatmap_keeps_latest_60_across_models_and_restores_completion_order(tmp_path):
    from agent.companion.chart import ACTIVITY_CELLS, FAILED_CELL, ActivityHeatmap
    from agent.companion.timeline import restore_session, transact

    c = companion(tmp_path, lambda: "s")
    chart = ActivityHeatmap(c)
    for i in reversed(range(65)):
        _observe_tool(
            c.timeline, i, "retired" if i < 5 else "execute", failed=i < 11,
            model=f"model-{i % 2}",
        )
    assert [obs.finished_at for obs in c.timeline.recent_tool_calls] == list(range(5, 65))
    wide = chart.draw(64, 9)
    assert wide[0][-1][1] == "90% success"
    assert wide[1][0][1] == "Last 60 calls"
    assert wide[2][0][1].strip() == "execute"
    assert wide[2][1::2] == [FAILED_CELL] * 2 + [ACTIVITY_CELLS[3]] * 18
    assert wide[-1][0][1] == "60 calls · 54 success · 6 failed"
    _observe_tool(c.timeline, -1, "old", failed=True)
    assert chart.draw(64, 9) is wide
    narrow = chart.draw(30, 9)
    assert narrow[1][0][1] == "Last 60 calls · 33 shown"
    assert narrow[2][1::2] == [ACTIVITY_CELLS[3]] * 11
    assert narrow[-1][0][1] == "60 calls·54 success·6 failed"
    transact(c.database, c.timeline.pending)
    c.timeline = restore_session(c.database, "s", c.window)
    assert chart.draw(64, 9) == wide


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


def test_focus_shortcuts_and_resize_preserve_both_drafts():
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
            app.application.timeoutlen = None

            def press(sequence):
                pipe.send_bytes(sequence)
                app.application.key_processor.feed_multiple(pipe.read_keys())
                app.application.key_processor.process_keys()

            with set_app(app.application):
                app.buffer.text = "main draft"
                press(b"\x1b[1;5C")
                assert app.application.layout.current_control is app.lain_editor
                app.lain_buffer.text = "chat draft"
                press(b"\x1b[1;5D")
                assert app.application.layout.current_control is app.editor_control
                press(b"\x1b[1;5C")
                assert app.application.layout.current_control is app.lain_editor
                app.application.output.get_size = lambda: Size(24, 80)
                app.application.before_render.fire()
                press(b"\x1b[1;5C")
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


def test_distributed_decline_with_low_individual_rates_survives_resume(tmp_path):
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
    assert c.timeline.overall.rate == 10
    incident = c.timeline.incidents[SESSION_RELIABILITY]
    assert incident["state"] == "open" and incident["scope"] == "session"
    assert sum(key == SESSION_RELIABILITY for key, _, _ in c.timeline.queue) == 1
    transact(c.database, c.timeline.pending)
    restored = restore_session(c.database, "s", 20)
    assert restored.overall.statistics() == c.timeline.overall.statistics()
    assert restored.incidents[SESSION_RELIABILITY]["id"] == incident["id"]


def test_incident_recovers_only_after_clean_results(tmp_path):
    c = companion(tmp_path, lambda: "s")
    key = ("fixed model", "default", "probe")
    for i in range(3):
        _observe_tool(c.timeline, i, "probe", failed=True)
    assert len(c.timeline.queue) == 1
    assert c.timeline.incidents[key]["state"] == "open"
    for i in range(3, 7):
        _observe_tool(c.timeline, i, "probe")
        assert c.timeline.incidents[key]["state"] == "open"
    _observe_tool(c.timeline, 7, "probe")
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
            retry_basis="matching_visible_arguments",
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
    assert reply_text("Uncertain.", proactive=True) == "Uncertain."
    assert reply_text("No evidence.") == "No evidence."
    assert reply_text("先看依赖声明。") == "先看依赖声明。"
    long = (
        "查了。先把面板没说的数字摊开。failures：4 条，都在同一个 turn 里。"
        "Order: call_1 exit_code, call_2 exit_code, call_3 exit_code, call_4 permission."
        "完整命令需要 tool_detail。"
    )
    assert reply_text(long) == long
    assert reply_text("x" * 90, proactive=True).endswith("…")


def test_diagnostic_window_filters_events_before_the_row_limit(tmp_path):
    from agent.companion.timeline import timeline_window, transact

    path = tmp_path / "timeline.db"
    transact(path, [
        ("s", "tool", "call", 1, "turn", {"tool_name": "execute"}),
        ("s", "incident", "notice", 2, "turn", {"state": "open"}),
        ("s", "turn", "turn", 3, "turn", {"turn": "turn"}),
        ("s", "git", "git", 4, "turn", {"branch": "fixture"}),
        ("other", "incident", "other", 5, "turn", {"state": "resolved"}),
        *(("s", "resources", str(i), i, "turn", {"percent": [50]}) for i in range(6, 25)),
    ])
    recent = timeline_window(path, "s", "timeline", 0, 30, limit=3)
    assert [kind for _, kind, _ in recent["records"]] == ["turn", "incident", "tool"]
    assert not recent["truncated"]
    incidents = timeline_window(path, "s", "timeline", 0, 30, event_type="incident", turn_id="turn", limit=1)
    assert incidents == {"records": [(2.0, "incident", {"state": "open"})], "truncated": False}
    git = timeline_window(path, "s", "git", 0, 30, limit=1)
    assert git["records"][0][1] == "git"
