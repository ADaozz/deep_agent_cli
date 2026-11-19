"""Companion geometry, offline animation, observation evidence and isolation."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import shutil
import signal
import sys
from zoneinfo import ZoneInfo

from deepagents.backends import StateBackend
from langchain_core.messages import AIMessage
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
import pytest

from agent.cli.app import CliApplication
from agent.companion import STATES
from agent.companion.theme import ACCENT
from agent.companion.animation import ASSETS, frames
from agent.companion.controller import Companion
from agent.companion.monitor import Service, services
from agent.companion.worker import create_observer
from agent.execution.process import _run_process
from agent.runtime.contracts import RunEvent
from agent.runtime.runner import AgentRunner
from tests.cli_support import paint
from tests.conftest import scripted_model


def test_resizing_preserves_draft_and_clips_both_columns():
    async def run():
        runner = AgentRunner(model=scripted_model([]), backend=StateBackend())
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app.buffer.text = "输入 very long " * 28
            app.state.add_system("tool output " * 500)
            app.companion.log("独立滚动")
            for columns, rows, count in [(180, 48, 0), (142, 32, 1), (106, 26, 1), (80, 24, 0), (180, 48, 2)]:
                app.companion.services = tuple(Service(300 + i, "", 300 + i, "job", False) for i in range(count))
                app.application.output.get_size = lambda: Size(rows=rows, columns=columns)
                paint(app)
                screen = app.application.renderer._last_screen
                width = app._width()
                assert app.transcript_window.render_info.window_width == width
                assert app.application.layout.current_window.render_info.window_width == width - 2
                assert app.buffer.text == "输入 very long " * 28
                if width == columns:
                    continue
                assert all(screen.data_buffer[y][width].char == "│" for y in range(2, rows))
                right = "\n".join("".join(screen.data_buffer[y][x].char for x in range(width+1, columns))
                                  for y in range(rows))
                for label in ("Lain Iwakura", "CPU", "GPU", "RAM", "ROM", "Status:", "services", "F6", "No elevated"):
                    assert label in right
                panel = right.splitlines()[2:]
                assert all(row.startswith("  ") and row.endswith("  ") for row in panel)
                assert columns - width - 1 == round(columns * .28)
                if columns >= 180:
                    assert panel[0].strip() == "Companion Agent [ Lain Iwakura ]"
                    top = next(row for row in panel if "┌" in row)
                    bottom = next(row for row in panel if "└" in row)
                    assert "┐  ┌" in top and bottom == top.translate(str.maketrans("┌┐", "└┘"))
                assert any(row.strip() == "IDLE" for row in panel)
                resources = next(row for row in panel if "CPU" in row)
                assert resources.count(" │ ") == 3
                assert (f"{count} running" if count else "no │") in right
                for service in app.companion.services:
                    assert f"PID {service.pid}" in right
                assert "tool output" not in right
        runner.close()
    asyncio.run(run())


@pytest.mark.parametrize("behavior", STATES)
def test_each_supplied_gif_animates_inside_a_fixed_viewport(behavior):
    assert json.loads((ASSETS / "manifest.json").read_text())["states"][behavior]["file"] == f"lain_{behavior}.gif"
    rendered = set()
    for fragments, delay in frames(behavior, 64, 16):
        assert delay > 0
        text = "".join(s for _, s in fragments)
        assert [len(row) for row in text.splitlines()] == [64] * 16
        assert all(color == ACCENT for color, _ in fragments)
        assert all(c in " \n" or 0x2801 <= ord(c) <= 0x28ff for c in text)
        assert all(row[:6].isspace() for row in text.splitlines())
        rendered.add(text)
        if len(rendered) > 1:
            break
    assert len(rendered) > 1


def companion(tmp_path, session):
    return Companion(workspace=tmp_path, config_dir=tmp_path, session=session, geometry=lambda: (64,16),
                     invalidate=lambda: None, timezone=ZoneInfo("UTC"))


def test_statistics_idempotent_attributed_at_start_and_persisted(tmp_path):
    async def run():
        c = companion(tmp_path, lambda: "session")
        c.event(RunEvent("run_started"), "first")
        for i in range(3):
            event = RunEvent("tool_started", tool_call_id=str(i), name="read", message_id="parallel")
            c.event(event, "first")
            c.event(event, "second")
        c.event(RunEvent("tool_started", tool_call_id="four", name="edit"), "second")
        c.event(RunEvent("tool_completed", tool_call_id="0"), "second")
        c.event(RunEvent("tool_completed", tool_call_id="1", is_error=True), "second")
        c.event(RunEvent("run_completed"), "second")
        assert c.batches == {"parallel": ["read"] * 3}
        await c.save_statistics()
        other = companion(tmp_path, lambda: "session")
        await other.save_statistics(read=True)
        series = other.timeline.series
        assert series["first","default","read"].counts == {"success":1, "failed":1, "unknown":1}
        assert series["second","default","edit"].counts == {"unknown":1}
    asyncio.run(run())


def test_real_react_graph_can_express_or_remain_silent():
    async def run():
        observe = AIMessage(content="", tool_calls=[dict(name="observe", args={}, id="look")])
        speak = AIMessage(content="", tool_calls=[dict(name="express", args=dict(behavior="curious", message="看到变化了。"), id="show")])
        respond = create_observer(scripted_model([observe, speak, AIMessage(content="")]))
        assert await respond({"windows": []}) == {"behavior":"curious","message":"看到变化了。"}
        assert await respond({"windows": []}) == {}
    asyncio.run(run())


def test_late_reply_from_old_session_and_worker_crash(tmp_path, monkeypatch):
    async def run():
        session = ["first"]
        c = companion(tmp_path, lambda: session[0])
        # Separate fake process waits on a test signal before sending a stale reply.
        signal_path = tmp_path / "release"
        code = """
import json, sys, time
from pathlib import Path
print('{"status":"ready"}', flush=True)
r = json.loads(input())
p = Path(sys.argv[1])
p.with_suffix('.waiting').touch()
while not p.exists(): time.sleep(.01)
print(json.dumps(dict(session=r['session'],seq=r['seq'],token=r['token'],behavior='curious',message='stale',status='ok')),flush=True)
"""
        async def launch(*args):
            return await asyncio.create_subprocess_exec(sys.executable,"-c",code,str(signal_path),
                                                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        monkeypatch.setattr("agent.companion.controller.start_worker",launch)
        for i in range(3):
            c.event(RunEvent("tool_started",tool_call_id=str(i),name="write"),"a")
            c.event(RunEvent("tool_completed",tool_call_id=str(i),is_error=True),"a")
        c.event(RunEvent("run_completed"),"a")
        task = asyncio.create_task(c.worker_session())
        async with asyncio.timeout(5):
            while not signal_path.with_suffix(".waiting").exists():
                await asyncio.sleep(.01)
            session[0] = "second"
            c.sync_session()
            signal_path.touch()
            await task
        assert c.connection == "OFFLINE"
        assert all(text != "stale" for _, _, text in c.logs)
    asyncio.run(run())


def test_host_background_service_outlives_launch_shell(tmp_path):
    result = _run_process("sleep 60 </dev/null >/dev/null 2>&1 & echo $!", shell=True, cwd=tmp_path,
                          timeout=3, max_output_bytes=4096)
    pid = int(result.output.strip())
    try:
        assert any(row.pid == pid and row.name == "sleep" for row in services())
    finally:
        os.kill(pid, signal.SIGTERM)


@pytest.mark.sandbox
def test_sandbox_services_use_namespace_pids_and_expire(tmp_path):
    from agent.execution.backend import BubblewrapBackend
    from agent.execution.policy import SandboxConfig
    if not shutil.which("bwrap"):
        pytest.skip("Bubblewrap unavailable")
    probe = subprocess.run([shutil.which("bwrap"), "--unshare-user", "--unshare-net", "--ro-bind", "/", "/", "true"],
                           capture_output=True, text=True)
    if probe.returncode and any(s in probe.stderr.lower() for s in ("not permitted", "permission denied", "namespace")):
        if os.getenv("REQUIRE_BWRAP_TEST") == "1":
            pytest.fail(probe.stderr)
        pytest.skip(probe.stderr)
    assert probe.returncode == 0, probe.stderr
    backend = BubblewrapBackend(SandboxConfig(workspace=tmp_path), executable=shutil.which("bwrap"))
    try:
        result = backend.execute("python3 -m http.server 8001 >/dev/null 2>&1 & echo $!; "
                                 "python3 -m http.server 8002 >/dev/null 2>&1 & echo $!; "
                                 "sleep 60 >/dev/null 2>&1 & echo $!",timeout=5)
        assert result.exit_code == 0, result.output
        ids = set(map(int, result.output.split()))
        rows = [row for row in services() if row.sandbox]
        assert {row.pid for row in rows} == ids
        assert sorted(row.name for row in rows) == ["http.server","http.server","sleep"]
    finally:
        backend.pool.shutdown()
    assert not any(row.sandbox for row in services())


def test_log_anchor_survives_appends_and_resumes_at_bottom(tmp_path):
    from agent.companion.panel import ChatRows

    c = companion(tmp_path, lambda: "session")
    view = ChatRows(c)
    for i in range(15):
        c.log(f"line {i} " + "长文本" * 8)
    newest = view.visible_lines(30, 4)
    view.scroll_lines(-1)
    pinned = view.visible_lines(30, 4)
    assert pinned != newest
    c.log("the next event")
    assert view.visible_lines(30, 4) == pinned
    view.scroll_lines(100)
    assert c.anchor is None
    assert "the next event" in "".join(text for row in view.visible_lines(30, 4) for _,text in row)


def test_broken_clip_is_local_and_other_behavior_recovers(tmp_path, monkeypatch):
    c = companion(tmp_path, lambda: "session")

    def clips(state, width, height):
        if state == "idle":
            raise ValueError("corrupt GIF")
        yield [("", "frame")], .12

    monkeypatch.setattr("agent.companion.controller._frames", clips)

    async def run():
        task = asyncio.create_task(c.animate())
        try:
            async with asyncio.timeout(5):
                while not c.frame:
                    await asyncio.sleep(.02)
                assert c.frame[0][1] == "Animation unavailable"
                c.event(RunEvent("run_started"), "model")
                while c.frame != [("", "frame")]:
                    await asyncio.sleep(.02)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_repository_reads_only_workspace_source(tmp_path):
    from agent.companion.repository import Repository

    root = tmp_path / "repo"
    root.mkdir()
    (root / "code.py").write_text("import widget\nwidget.load()\n")
    private = root / "private"
    private.mkdir()
    (private / "state.json").write_text("excluded")
    (root / ".env").write_text("excluded")
    outside = tmp_path / "outside.py"
    outside.write_text("excluded")
    (root / "alias.py").symlink_to(outside)
    (root / "linked").symlink_to(tmp_path, target_is_directory=True)
    os.link(outside, root / "hard.py")
    os.mkfifo(root / "pipe.py")

    repo = Repository(root, [private])
    assert repo.read("code.py", 2) == "code.py:2 widget.load()"
    assert "code.py:1 import widget" in repo.search("widget")
    for path in ("../outside.py", str(outside), "alias.py", "linked/outside.py", "hard.py",
                 "private/state.json", ".env", "pipe.py"):
        assert repo.read(path) == "File unavailable."
    assert repo.search("excluded").startswith("No match")


def test_chat_receives_session_evidence_without_waiting_for_observer_threshold(tmp_path, monkeypatch):
    async def run():
        session = ["one"]
        c = companion(tmp_path, lambda: session[0])
        code = """
import json, sys
print('{"status":"ready"}', flush=True)
r = json.loads(input())
q = r['snapshot']
assert q['question'] == 'why?' and q['recent_errors'] == [['read', 'missing widget']]
assert 'question' in q
print(json.dumps(dict(session=r['session'], seq=r['seq'], token=r['token'], message='连续读失败了。', status='ok')), flush=True)
"""
        async def launch(*args):
            return await asyncio.create_subprocess_exec(sys.executable, "-c", code, stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.PIPE)
        monkeypatch.setattr("agent.companion.controller.start_worker", launch)
        c.event(RunEvent("tool_started", tool_call_id="a", name="read"), "model")
        c.event(RunEvent("tool_completed", tool_call_id="a", is_error=True, content="missing widget"), "model")
        c.ask("why?")
        await asyncio.wait_for(c.worker_session(), 5)
        assert any((role, msg) == ("You", "why?") for _, role, msg in c.logs)
        assert c.logs[-1][1:] == ("Lain", "连续读失败了。")
        session[0] = "two"
        c.sync_session()
        assert not (c.logs or c.errors or c.question)
    asyncio.run(run())


def test_sensitive_names_and_embedded_credentials_are_unavailable(tmp_path):
    from agent.companion.repository import Repository
    repo = Repository(tmp_path, secrets=("fixture-sensitive-value",))
    for name, content in (("credentials.json",'{"value":"private"}'),("config.yaml","private"),
                          ("innocent.py","print('fixture-sensitive-value')"),
                          ("example.md","api_key = 'private-value'")):
        (tmp_path/name).write_text(content)
        assert repo.read(name) == "File unavailable."
    assert repo.search("private").startswith("No match")


def test_quiet_activity_does_not_request_model(tmp_path, monkeypatch):
    async def run():
        c = companion(tmp_path, lambda:"s")
        def launch(*_):
            return asyncio.create_subprocess_exec(sys.executable,"-c",
                "import sys;print('{\u0022status\u0022:\u0022ready\u0022}',flush=True);"
                "sys.stdin.readline();sys.exit(2)", stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE)
        monkeypatch.setattr("agent.companion.controller.start_worker",launch)
        task = asyncio.create_task(c.worker_session())
        while not c.worker_ready:
            await asyncio.sleep(.01)
        for i in range(5):
            c.event(RunEvent("tool_started",name="read",tool_call_id=str(i)),"m")
            c.event(RunEvent("tool_completed",tool_call_id=str(i)),"m")
        c.timeline.sample("services", ["service"], c.turn)
        c.timeline.sample("resources", [1,None,2,3], c.turn)
        await c.save_statistics()
        assert not c.timeline.queue and c.request_version == 0 and not task.done()
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())


def test_missing_key_keeps_local_evidence_and_chart(tmp_path, monkeypatch):
    from agent.companion.chart import FailureChart

    async def run():
        monkeypatch.delenv("COMPANION_DEEPSEEK_API_KEY", raising=False)
        c = companion(tmp_path, lambda:"one")
        assert await c.worker_session() == "no key"
        c.event(RunEvent("tool_started", name="read", tool_call_id="id"),"fixed model")
        c.event(RunEvent("tool_completed", tool_call_id="id", is_error=True),"fixed model")
        assert "100% n=1" in str(FailureChart(c).draw(36,5))
        assert c.request_version == 0
        await c.finish()
    asyncio.run(run())


def test_old_worker_cannot_read_new_session(tmp_path, monkeypatch):
    async def run():
        session = ["old"]
        c = companion(tmp_path, lambda:session[0])
        release = tmp_path/"release"
        code = """
import json,sys,time
from pathlib import Path
p=Path(sys.argv[1])
print(json.dumps({'status':'ready'}),flush=True)
r=json.loads(input())
identity={k:r[k] for k in ('session','seq','token')}
p.with_suffix('.waiting').touch()
while not p.exists(): time.sleep(.01)
print(json.dumps(dict(**identity,query='turns',args={})),flush=True)
p.with_suffix('.result').write_text(json.loads(input())['data'])
print(json.dumps(dict(**identity,status='ok',message='old answer')),flush=True)
"""
        async def launch(*args):
            return await asyncio.create_subprocess_exec(sys.executable,"-c",code,str(release),
                                                       stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE)
        monkeypatch.setattr("agent.companion.controller.start_worker",launch)
        c.ask("review")
        task=asyncio.create_task(c.worker_session())
        async with asyncio.timeout(6):
            while not release.with_suffix(".waiting").exists():
                await asyncio.sleep(.01)
            session[0]="new"
            c.sync_session()
            release.touch()
            await task
        assert release.with_suffix(".result").read_text()=="Expired request."
        assert not any(msg=="old answer" for _,_,msg in c.logs)
    asyncio.run(run())
