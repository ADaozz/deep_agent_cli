"""Real PTY input/rendering with a scripted model and real Bubblewrap.

These are reconstructed checks, not the unavailable historical UX-01..13 suite.
No API credentials, tmux, or browser installation is required.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time

import pytest

from tests.test_playwright_sandbox import page_url  # noqa: F401  pytest fixture for the Playwright PTY test

pytestmark = pytest.mark.sandbox

_PROBE = r'''
import asyncio, json, sys
from pathlib import Path
from langchain_core.messages import AIMessage
from agent.cli.app import CliApplication
from agent.config import SandboxConfig
from agent.runner import AgentRunner
from tests.conftest import scripted_model

async def main():
    workspace = Path(sys.argv[1])
    messages = [AIMessage(content='', tool_calls=[{'id':'pty-ex','name':'execute',
                'args':{'command':'echo explicitly-approved'}}]), AIMessage(content='done')]
    config = SandboxConfig(workspace=workspace)
    if len(sys.argv) > 3:
        from tests.test_playwright_sandbox import _prerequisites, _open_command
        config = _prerequisites(workspace)
        (workspace/'page.html').write_text('<title>PTY Browser</title><h1>real TUI browser</h1>')
        commands = [_open_command(workspace, sys.argv[4]),
                    'playwright-cli snapshot', 'playwright-cli screenshot', 'playwright-cli close']
        messages = [AIMessage(content='', tool_calls=[{'id':f'pty-pw-{i}', 'name':'execute',
                    'args':{'command':c, 'timeout':30}}]) for i,c in enumerate(commands)]
        messages.append(AIMessage(content='done'))
    runner = AgentRunner(model=scripted_model(messages), sandbox_config=config)
    app = CliApplication(runner)
    target = Path(sys.argv[2])
    def snapshot():
        data = {'input':app.buffer.text, 'cursor':app.buffer.cursor_position,
                'completion':app.buffer.complete_state is not None,
                'permission':runner.permission_mode().value,
                'pending':runner.pending_permission_mode().value if runner.pending_permission_mode() else None,
                'interaction':app.interaction.kind if app.interaction else None,
                'question':app.interaction.question if app.interaction else '',
                'status':app.state.status,
                'footer':''.join(x[1] for x in app._footer_text()),
                'transcript':'\n'.join(str(getattr(b,'content','')) for b in app.state.blocks),
                'interrupt':runner.current_interrupt().kind.value if runner.current_interrupt() else None,
                'tools':[{'name':b.name,'output':b.output,'exit_code':b.exit_code,'is_error':b.is_error}
                         for b in app.state.blocks if hasattr(b,'tool_call_id')]}
        temporary = target.with_suffix('.new')
        temporary.write_text(json.dumps(data))
        temporary.replace(target)
    async def watch():
        while True:
            snapshot()
            await asyncio.sleep(.02)
    observer = asyncio.create_task(watch())
    try:
        await app.run_async()
    finally:
        observer.cancel()
        snapshot()
        runner.close()
asyncio.run(main())
'''


class Tui:
    def __init__(self, tmp_path: Path, playwright=False, page_url="", probe=None, extra_args=()):
        # Read the module global at call time: other suites patch _PROBE.
        probe = probe or _PROBE
        self.state = tmp_path / 'state.json'
        workspace = tmp_path / 'workspace'
        workspace.mkdir()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        env = {**os.environ, 'TERM': 'xterm-256color'}
        args = [sys.executable, '-c', probe, str(workspace), str(self.state), *extra_args]
        if playwright:
            args.extend(['playwright', page_url])
        self.workspace = workspace
        self.process = subprocess.Popen(args,
                                        stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
        os.close(slave)
        self.master = master
        self.terminal = bytearray()
        self.log = tmp_path / 'terminal.txt'

    def read(self):
        while select.select([self.master], [], [], 0)[0]:
            try:
                chunk = os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    return
                raise
            if not chunk:
                return
            self.terminal.extend(chunk)

    def send(self, text: str):
        os.write(self.master, text.encode())

    def wait(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.read()
            if self.state.exists():
                state = json.loads(self.state.read_text())
                if predicate(state):
                    return state
            if self.process.poll() is not None:
                break
            time.sleep(.02)
        raise AssertionError(f'PTY condition failed; terminal evidence: {self.log}\n'
                             + self.terminal.decode(errors='replace')[-3000:])

    def close(self):
        if self.process.poll() is None:
            self.send('\x03')
            self.send('\x03')
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        self.read()
        self.log.write_bytes(self.terminal)
        os.close(self.master)


@pytest.fixture
def tui(tmp_path):
    app = Tui(tmp_path)
    try:
        app.wait(lambda s: s['status'] == 'Ready')
        yield app
    finally:
        app.close()


def test_real_pty_completion_permission_and_pending_approval(tui):
    tui.send('/mod')
    before = tui.wait(lambda s: s['input'] == '/mod' and s['completion'])
    tui.send('\x1b')
    after = tui.wait(lambda s: s['input'] == '/mod' and not s['completion'])
    assert before['cursor'] == after['cursor'] == 4
    tui.send('e')
    tui.wait(lambda s: s['input'] == '/mode' and s['completion'])
    tui.send('\t')
    tui.wait(lambda s: s['input'].startswith('/model') and not s['completion'])
    tui.send('\x03/status\r')
    tui.wait(lambda s: 'Status: idle' in s['transcript'])
    tui.send('/permission allow\r')
    tui.wait(lambda s: s['interaction'] == 'permission_confirm')
    tui.send('ALLOW\r')
    state = tui.wait(lambda s: s['permission'] == 'allow' and s['interaction'] is None)
    assert state['pending'] is None and 'perm:allow' in state['footer']
    tui.send('/permission ask\r')
    state = tui.wait(lambda s: s['permission'] == 'ask')
    assert state['pending'] is None and 'perm:ask' in state['footer']
    tui.send('run a command\r')
    state = tui.wait(lambda s: s['interaction'] == 'approval')
    assert 'Network: OFF (default)' in state['question']
    assert 'Command: echo explicitly-approved' in state['question']
    tui.send('\x1b')
    tui.wait(lambda s: s['interaction'] is None and s['interrupt'])
    tui.send('/permission allow\r')
    tui.wait(lambda s: s['interaction'] == 'permission_confirm')
    tui.send('ALLOW\r')
    state = tui.wait(lambda s: s['pending'] == 'allow')
    assert state['permission'] == 'ask' and state['interrupt']
    tui.send('\x1bOQ')  # F2
    tui.wait(lambda s: s['interaction'] == 'approval')
    tui.send('\r')  # default Reject
    state = tui.wait(lambda s: s['permission'] == 'allow' and s['interrupt'] is None)
    assert state['pending'] is None


@pytest.mark.parametrize('gap,exits', [(0.3, True), (0.7, True), (1.2, False)])
def test_real_pty_ctrl_c_window(tui, gap, exits):
    tui.send('draft\x03')
    tui.wait(lambda s: s['input'] == '' and 'Input cleared' in s['status'])
    time.sleep(gap)
    tui.send('\x03')
    if exits:
        tui.process.wait(timeout=4)
        assert tui.process.returncode == 0
    else:
        tui.wait(lambda s: 'Input cleared' in s['status'])
        assert tui.process.poll() is None
        tui.send('still editing')
        tui.wait(lambda s: s['input'] == 'still editing')


@pytest.mark.playwright
def test_real_pty_allow_playwright_flow(tmp_path, page_url):
    from tests.test_playwright_sandbox import _prerequisites
    _prerequisites(tmp_path)  # Explicit prerequisite skip occurs in parent, never after launch failure.
    tui = Tui(tmp_path, playwright=True, page_url=page_url)
    try:
        tui.wait(lambda s: s['status'] == 'Ready')
        tui.send('/permission allow\r')
        tui.wait(lambda s: s['interaction'] == 'permission_confirm')
        tui.send('ALLOW\r')
        tui.wait(lambda s: s['permission'] == 'allow' and s['pending'] is None)
        tui.send('run Playwright browser flow\r')
        state = tui.wait(lambda s: len(s['tools']) == 4 and s['tools'][-1]['exit_code'] is not None, timeout=60)
        assert all(t['exit_code'] == 0 and not t['is_error'] for t in state['tools'])
        assert 'Page URL' in state['tools'][0]['output']
        assert list((tui.workspace/'.playwright-cli').glob('*.png'))
        assert state['interaction'] is None
    finally:
        tui.close()


_STREAM_PROBE = r'''
import asyncio, json, sys, threading
from pathlib import Path
from deepagents.backends import StateBackend
from agent.cli.app import CliApplication
from agent.config import ModelProfile, Settings
from agent.llm_stream import live_stream_count
from agent.runner import AgentRunner
from agent.session import SessionStore

async def main():
    workspace, target, base_url, api = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
    settings = Settings(llm_profiles=(ModelProfile('local/stream', 'stream-model', api_key='local',
                        base_url=base_url, api=api),), llm_default='local/stream')
    store = SessionStore(workspace / 'state.sqlite3')
    runner = AgentRunner(settings=settings, backend=StateBackend(), session_store=store, thread_id='pty-cancel')
    app = CliApplication(runner)
    requests = []
    original = runner.request_cancel
    def counted_cancel():
        requests.append(1)
        original()
    runner.request_cancel = counted_cancel
    def snapshot():
        info = store.get(runner.thread_id)
        data = {'status': app.state.status, 'running': app.state.running,
                'transcript': '\n'.join(str(getattr(b, 'content', '')) for b in app.state.blocks),
                'cancel_requests': len(requests), 'live_streams': live_stream_count(),
                'busy': runner._busy, 'last_run': info.last_run_status.value if info and info.last_run_status else None,
                'threads': sorted(t.name for t in threading.enumerate())}
        temporary = target.with_suffix('.new')
        temporary.write_text(json.dumps(data))
        temporary.replace(target)
    async def watch():
        while True:
            snapshot()
            await asyncio.sleep(.02)
    observer = asyncio.create_task(watch())
    try:
        await app.run_async()
    finally:
        observer.cancel()
        snapshot()
        runner.close()
        store.close()
asyncio.run(main())
'''


@pytest.mark.parametrize('api', ['chat_completions', 'responses'])
def test_real_pty_escape_cancels_silent_model_stream_then_accepts_next_task(tmp_path, api):
    from tests.model_stream_server import ScriptedSSEServer, Turn

    turns = [Turn(text='partial reply then silence', mode='hang', chunks_before_hang=1),
             Turn(text='second answer')]
    with ScriptedSSEServer(api, turns) as server:
        tui = Tui(tmp_path, probe=_STREAM_PROBE, extra_args=(server.base_url, api))
        try:
            tui.wait(lambda s: s['status'] == 'Ready')
            tui.send('first task\r')
            connection = server.wait_for_connection(0)
            assert connection.hanging.wait(8)
            tui.wait(lambda s: s['running'] and 'partial' in s['transcript'])
            started = time.monotonic()
            tui.send('\x1b')
            state = tui.wait(lambda s: not s['running'] and s['status'] == 'Cancelled' and not s['busy'], timeout=5)
            elapsed = time.monotonic() - started
            assert elapsed < 5, elapsed
            assert connection.closed.wait(1) and connection.closed_by_client_at is not None
            assert state['live_streams'] == 0
            assert state['cancel_requests'] == 1
            assert state['last_run'] == 'aborted'
            assert 'Operation cancelled.' in state['transcript']
            # Esc while idle neither cancels nor disturbs the next run.
            tui.send('\x1b')
            tui.send('second task\r')
            state = tui.wait(lambda s: 'second answer' in s['transcript'] and s['status'] == 'Ready')
            assert state['cancel_requests'] == 1 and state['live_streams'] == 0
            assert len(server.connections) == 2
            assert 'aborted by the user' in json.dumps(server.connections[1].body)
        finally:
            tui.close()
        assert server.open_connections() == []


def test_real_pty_mcp_connection_does_not_block_input(tmp_path):
    probe = _PROBE.replace(
        "runner = AgentRunner(model=scripted_model(messages), sandbox_config=config)",
        """
    import threading
    from deepagents.backends import StateBackend
    from agent.mcp.manager import MCPManager, MCPResult
    manager = MCPManager(workspace / 'mcp.json')
    def slow_test(name):
        (workspace / 'connecting').write_text(name)
        if not threading.Event().wait(0.8):
            return MCPResult(False, False, 'MCP connection timeout; retry available')
    manager.test = slow_test
    runner = AgentRunner(model=scripted_model(messages), backend=StateBackend(), mcp_manager=manager)
""".strip(),
    )
    tui = Tui(tmp_path, probe=probe)
    try:
        tui.wait(lambda state: state["status"] == "Ready")
        tui.send("/mcp test unreachable\r")
        tui.wait(lambda state: (tui.workspace / "connecting").exists())
        tui.send("input during connection")
        tui.wait(lambda state: state["input"] == "input during connection")
        tui.wait(lambda state: "MCP connection timeout" in state["transcript"])
    finally:
        tui.close()

# Real host stdio service, scripted model, and the production TUI/approval handlers.
# All credential literals below are deliberately fake.
_MCP_TUI_SERVER = r'''
import asyncio, json
from pathlib import Path
from fastmcp import FastMCP

server = FastMCP("tui-e2e")
@server.tool
def write_marker(nonce: str, path: str, auth: dict) -> str:
    """Write the supplied nonce on the host."""
    Path(path).write_text(nonce)
    Path(path + ".args.json").write_text(json.dumps({"nonce": nonce, "path": path, "auth": auth}))
    return "wrote marker"
asyncio.run(server.run_stdio_async(show_banner=False, log_level="error"))
'''

_MCP_TUI_PROBE = _PROBE.replace(
    "runner = AgentRunner(model=scripted_model(messages), sandbox_config=config)",
    """
    from agent.mcp.manager import MCPManager
    service = workspace.parent / 'server_with_[brackets].py'
    marker = workspace.parent / 'host-marker'
    manager = MCPManager(workspace / 'mcp.json', connect_timeout=8, call_timeout=8)
    if sys.argv[3] != 'wizard':
        added = manager.add_from_fields(name='tui_e2e', transport='stdio', command=sys.executable,
                                        args=[str(service)])
        assert added.ok, added.message
    arguments = {'nonce':'TUI_E2E_APPROVE_002', 'path':str(marker),
                 'auth':{'api_key':'FAKE_TUI_API_SECRET', 'Authorization':'Bearer FAKE_TUI_BEARER'}}
    messages = [AIMessage(content='', tool_calls=[{'id':'mcp-original-id',
                'name':'mcp__tui_e2e__write_marker', 'args':arguments}]),
                AIMessage(content='MCP task finished')]
    runner = AgentRunner(model=scripted_model(messages), sandbox_config=config, mcp_manager=manager)
""".strip(),
).replace(
    "if len(sys.argv) > 3:", "if False:",
).replace(
    "'status':app.state.status,",
    """'status':app.state.status,
                'expanded':app.state.tools_expanded,
                'rendered_interaction':render_interaction(app.interaction, 60) if app.interaction else '',
                'rendered_transcript':render_transcript(app.state, 60),""",
).replace(
    "from agent.cli.app import CliApplication",
    "from agent.cli.app import CliApplication\nfrom agent.cli.rendering import render_interaction, render_transcript",
)


def _mcp_tui(tmp_path, mode="call"):
    (tmp_path / "server_with_[brackets].py").write_text(_MCP_TUI_SERVER)
    app = Tui(tmp_path, probe=_MCP_TUI_PROBE, extra_args=(mode,))
    try:
        app.wait(lambda state: state["status"] in {"Ready", "MCP operation finished"}, timeout=20)
    except BaseException:
        app.close()
        raise
    return app


@pytest.mark.parametrize("permission", ["ask", "allow"])
@pytest.mark.parametrize("approve", [False, True])
def test_real_pty_host_mcp_arguments_redacted_but_execution_unchanged(tmp_path, permission, approve):
    tui = _mcp_tui(tmp_path)
    marker = tmp_path / "host-marker"
    try:
        if permission == "allow":
            tui.send("/permission allow\r")
            tui.wait(lambda state: state["interaction"] == "permission_confirm")
            tui.send("ALLOW\r")
            tui.wait(lambda state: state["permission"] == "allow")
        tui.send("write the host marker\r")
        state = tui.wait(lambda state: state["interaction"] == "approval", timeout=15)
        assert state["interrupt"] == "waiting_confirmation"
        assert "nonce: TUI_E2E_APPROVE_002" in state["rendered_interaction"]
        assert "Execution: Host (outside sandbox)" in state["question"]
        assert "[redacted]" in state["question"]
        assert not marker.exists()
        # Existing detail view is reached without answering the pending approval.
        tui.send("\x1b")
        tui.wait(lambda state: state["interaction"] is None and state["interrupt"])
        tui.send("\x0f")  # Ctrl+O
        state = tui.wait(lambda state: state["expanded"])
        assert "api_key: [redacted]" in state["rendered_transcript"]
        assert "Authorization: [redacted]" in state["rendered_transcript"]
        for secret in ("FAKE_TUI_API_SECRET", "FAKE_TUI_BEARER"):
            assert secret not in state["rendered_transcript"]
        tui.send("\x1bOQ")  # F2 reopens the same approval and toolCallId
        tui.wait(lambda state: state["interaction"] == "approval")
        tui.send("\x1b[B\r" if approve else "\r")
        tui.wait(lambda state: state["interrupt"] is None and state["status"] == "Ready", timeout=15)
        if approve:
            assert marker.read_text() == "TUI_E2E_APPROVE_002"
            actual = json.loads(Path(str(marker) + ".args.json").read_text())
            assert actual["auth"] == {
                "api_key": "FAKE_TUI_API_SECRET", "Authorization": "Bearer FAKE_TUI_BEARER",
            }
            assert actual["nonce"] == "TUI_E2E_APPROVE_002"
        else:
            assert not marker.exists()
            assert not Path(str(marker) + ".args.json").exists()
    finally:
        tui.close()
    for secret in (b"FAKE_TUI_API_SECRET", b"FAKE_TUI_BEARER"):
        assert secret not in tui.terminal


def _mcp_add_wizard(tui, command):
    tui.send("/mcp add\r")
    tui.wait(lambda state: state["interaction"] == "mcp_name")
    tui.send("tui_e2e\r")
    tui.wait(lambda state: state["interaction"] == "mcp_transport")
    tui.send("\x1b[B\r")
    tui.wait(lambda state: state["interaction"] == "mcp_target")
    tui.send(command + "\r")
    return tui.wait(lambda state: state["interaction"] == "mcp_confirm")


def test_real_pty_mcp_add_confirmation_multiline_cancel_and_save(tmp_path):
    import shlex

    tui = _mcp_tui(tmp_path, "wizard")
    command = shlex.join([sys.executable, str(tmp_path / "server_with_[brackets].py")])
    try:
        # A real SIGWINCH exercises narrow-terminal wrapping as well as Rich text.
        fcntl.ioctl(tui.master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 60, 0, 0))
        os.kill(tui.process.pid, signal.SIGWINCH)
        state = _mcp_add_wizard(tui, command)
        lines = state["rendered_interaction"].splitlines()
        assert any(line.strip() == "Name: tui_e2e" for line in lines)
        assert any(line.strip() == "Transport: stdio" for line in lines)
        assert "[brackets]" in state["rendered_interaction"]
        assert command in state["question"]
        tui.send("\r")  # default Cancel
        tui.wait(lambda state: state["interaction"] is None)
        assert not (tui.workspace / "mcp.json").exists()
        _mcp_add_wizard(tui, command)
        tui.send("\x1b[B\r")
        tui.wait(lambda state: "connected" in state["transcript"].lower(), timeout=20)
        saved = json.loads((tui.workspace / "mcp.json").read_text())
        assert "tui_e2e" in str(saved)
    finally:
        tui.close()
