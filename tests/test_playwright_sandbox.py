"""Playwright CLI keeps one browser session across separate execute calls.

Prerequisites (the test skips without them; isolation is never relaxed):
Bubblewrap, ``playwright-cli`` on PATH with ``node`` (system or separately mounted), and a browser that
playwright-cli can launch (Google Chrome under /opt/google or browsers under
PLAYWRIGHT_BROWSERS_PATH / ~/.cache/ms-playwright). Node, browsers, and fonts
are bind-mounted read-only. PLAYWRIGHT_SANDBOX_URL overrides the local test
page, e.g. https://example.com.
"""
from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
import json
import shlex
from pathlib import Path
import shutil
import threading

import pytest
from langchain_core.messages import AIMessage

from agent.config import BindMount, SandboxConfig
from agent.runner import AgentRunner
from agent.sandbox import FIXED_PATH, SandboxUnavailableError
from agent.tools.execute import build_execute_tool
from tests.conftest import scripted_model
from tests.test_persistent_sandbox import _descendants, _wait_gone

pytestmark = [pytest.mark.sandbox, pytest.mark.playwright]


def _prerequisites(workspace: Path) -> SandboxConfig:
    bwrap = shutil.which("bwrap")
    cli = shutil.which("playwright-cli")
    if not (bwrap and cli):
        pytest.skip("Playwright prerequisites missing: bwrap and playwright-cli must be on PATH")
    node = shutil.which("node")
    if node is None:
        pytest.skip("BLOCKED: node is not on PATH")
    cli_prefix = Path(cli).absolute().parent.parent
    mounts = [BindMount(cli_prefix, "/opt/npm-prefix")]
    node_path = Path(node).resolve()
    node_bin = "/usr/bin" if node_path.is_relative_to("/usr") else "/opt/node/bin"
    if node_bin == "/opt/node/bin":
        mounts.append(BindMount(node_path.parent.parent, "/opt/node"))
    browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    if browsers.is_dir():
        mounts.append(BindMount(browsers, "/opt/ms-playwright"))
    if Path("/opt/google").is_dir():
        mounts.append(BindMount(Path("/opt/google"), "/opt/google"))
    browser_executable = os.environ.get("PLAYWRIGHT_SANDBOX_EXECUTABLE")
    if browser_executable:
        if not Path(browser_executable).is_file():
            pytest.skip(f"BLOCKED: browser executable missing: {browser_executable}")
        if not Path(browser_executable).resolve().is_relative_to("/usr"):
            raise ValueError("Test executable override must be in /usr (already mounted read-only)")
    elif not browsers.is_dir() and not Path("/opt/google").is_dir():
        pytest.skip("BLOCKED: no browser; prepare Chrome or use playwright-cli install-browser chromium")
    if Path("/etc/fonts").is_dir():
        mounts.append(BindMount(Path("/etc/fonts"), "/etc/fonts"))
    return SandboxConfig(
        workspace=workspace,
        bwrap_path=bwrap,
        extra_read_only_mounts=tuple(mounts),
        env_set={"PATH": f"/opt/npm-prefix/bin:{node_bin}:{FIXED_PATH}", "PLAYWRIGHT_BROWSERS_PATH": "/opt/ms-playwright"},
    )


@pytest.fixture
def page_url(tmp_path: Path):
    override = os.environ.get("PLAYWRIGHT_SANDBOX_URL")
    if override:
        yield override
        return
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<title>Sandbox Page</title><h1>persistent</h1>")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(site)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass


def _open_command(workspace: Path, page_url: str) -> str:
    open_command = f"playwright-cli open {shlex.quote(page_url)}"
    executable = os.environ.get("PLAYWRIGHT_SANDBOX_EXECUTABLE")
    if executable:
        config_file = workspace / "browser-test.json"
        config_file.write_text(json.dumps({"browser": {
            "browserName": "chromium", "launchOptions": {"executablePath": executable, "headless": True},
        }}))
        open_command += " --config=browser-test.json"
    return open_command


def test_playwright_session_survives_separate_execute_calls(tmp_path: Path, page_url: str) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    try:
        runner = AgentRunner(model=scripted_model([AIMessage(content="done")]), sandbox_config=_prerequisites(workspace))
    except SandboxUnavailableError as exc:
        pytest.skip(f"UNSUPPORTED SANDBOX ENVIRONMENT [{exc.kind}]: {exc}")
    pool = runner.sandbox_pool
    tool = build_execute_tool(runner.prepared.backend)

    def execute(command: str, *, network: bool = True) -> str:
        return tool.invoke({"command": command, "network": network, "timeout": 120})

    open_command = _open_command(workspace, page_url)
    try:
        opened = execute(open_command)
        assert "Page URL" in opened, f"Browser launch failed (not a skip):\n{opened}"
        browser_sandbox = pool.sandbox(True)
        assert "Exit code" not in execute("playwright-cli snapshot")
        execute("playwright-cli eval \"localStorage.setItem('test', '123')\"")
        execute("playwright-cli eval \"document.cookie = 'kept=yes; path=/'\"")
        assert '"123"' in execute("playwright-cli eval \"localStorage.getItem('test')\"")

        # The offline sandbox neither reaches the browser daemon nor the network.
        offline = execute("playwright-cli list", network=False)
        assert "Page URL" not in offline and page_url not in offline
        assert pool.sandbox(False) is not None

        # Back in the networked sandbox, page, storage and cookies are intact.
        assert pool.sandbox(True) is browser_sandbox
        assert '"123"' in execute("playwright-cli eval \"localStorage.getItem('test')\"")
        assert "kept=yes" in execute('playwright-cli eval "document.cookie"')
        assert page_url.rstrip("/") in execute('playwright-cli eval "location.href"')
        shot = execute("playwright-cli screenshot")
        assert "Exit code" not in shot
        assert list((workspace / ".playwright-cli").glob("*.png"))
        assert "Exit code" not in execute("playwright-cli close")

        # Closing the Agent session reaps a still-running browser and daemon.
        assert "Page URL" in execute(open_command)
        tree = {browser_sandbox.host_pid, *_descendants(browser_sandbox.host_pid)}
        browser_processes = [
            pid for pid in tree
            if b"chrom" in Path(f"/proc/{pid}/cmdline").read_bytes().lower()
        ]
        assert browser_processes
    finally:
        runner.close()
    assert _wait_gone(tree) == []
    assert pool.sandbox(True) is None and pool.sandbox(False) is None


@pytest.mark.parametrize("broken", [False, True])
def test_playwright_ask_network_approval_and_failure_metadata(tmp_path, page_url, broken) -> None:
    from agent.cli.interactions import InteractionController
    workspace = tmp_path / "work"
    workspace.mkdir()
    config = _prerequisites(workspace)
    command = _open_command(workspace, page_url)
    if broken:
        bad_config = workspace / "broken-browser.json"
        bad_config.write_text(json.dumps({"browser": {"browserName": "chromium", "launchOptions": {
            "executablePath": "/usr/bin/deep-agent-browser-does-not-exist", "headless": True,
        }}}))
        command = f"playwright-cli open {shlex.quote(page_url)} --config=broken-browser.json"
    runner = AgentRunner(model=scripted_model([
        AIMessage(content="", tool_calls=[{"id": "pw-approved", "name": "execute", "args": {
            "command": command, "network": True, "timeout": 30,
        }}]), AIMessage(content="model says done"),
    ]), sandbox_config=config)
    events = []
    try:
        waiting = runner.invoke("open browser")
        assert waiting.status == "waiting_confirmation"
        assert runner.sandbox_pool.sandbox(True) is None
        ui = InteractionController.approval(waiting.pending_tool_calls)
        assert "Network: ON" in ui.question and "host network" in ui.question
        assert ui.fields[0]["options"][0]["value"] == "reject"
        result = runner.approve_tool("pw-approved", on_event=events.append)
        completed = [e for e in events if e.type == "tool_completed" and e.tool_call_id == "pw-approved"]
        assert len(completed) == 1
        event = completed[0]
        assert event.is_error is broken
        assert (event.artifact["exit_code"] != 0) is broken
        if not broken:
            assert "Page URL" in event.content
            closed = build_execute_tool(runner.prepared.backend).invoke({
                "command": "playwright-cli close", "network": True, "timeout": 30,
            })
            assert "Exit code" not in closed
        else:
            assert "does-not-exist" in event.content
            assert result.output == "model says done"  # final model prose does not define tool success
    finally:
        runner.close()
