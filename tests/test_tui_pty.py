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

from tests.test_playwright_sandbox import page_url

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
    def __init__(self, tmp_path: Path, playwright=False, page_url=""):
        self.state = tmp_path / 'state.json'
        workspace = tmp_path / 'workspace'
        workspace.mkdir()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        env = {**os.environ, 'TERM': 'xterm-256color'}
        args = [sys.executable, '-c', _PROBE, str(workspace), str(self.state)]
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
