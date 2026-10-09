"""Playwright CLI keeps one browser session across separate execute calls.

Prerequisites (the test skips without them; isolation is never relaxed):
Bubblewrap, ``playwright-cli`` on PATH with ``node`` in the same prefix, and a browser that
playwright-cli can launch (Google Chrome under /opt/google or browsers under
PLAYWRIGHT_BROWSERS_PATH / ~/.cache/ms-playwright). Node, browsers, and fonts
are bind-mounted read-only. PLAYWRIGHT_SANDBOX_URL overrides the local test
page, e.g. https://example.com.
"""
from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
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
    # The global npm prefix that owns playwright-cli also holds its node.
    node_prefix = Path(cli).absolute().parent.parent
    if not (node_prefix / "bin" / "node").exists():
        pytest.skip(f"node is not installed next to playwright-cli in {node_prefix}")
    browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    mounts = [BindMount(node_prefix, "/opt/node")]
    if browsers.is_dir():
        mounts.append(BindMount(browsers, "/opt/ms-playwright"))
    if Path("/opt/google").is_dir():
        mounts.append(BindMount(Path("/opt/google"), "/opt/google"))
    if len(mounts) == 1:
        pytest.skip("No browser for playwright-cli: install Chrome or run `npx playwright install`")
    if Path("/etc/fonts").is_dir():
        mounts.append(BindMount(Path("/etc/fonts"), "/etc/fonts"))
    return SandboxConfig(
        workspace=workspace,
        bwrap_path=bwrap,
        extra_read_only_mounts=tuple(mounts),
        env_set={"PATH": f"/opt/node/bin:{FIXED_PATH}", "PLAYWRIGHT_BROWSERS_PATH": "/opt/ms-playwright"},
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

    try:
        opened = execute(f"playwright-cli open {page_url}")
        if "Page URL" not in opened:
            pytest.skip(f"playwright-cli could not open a browser in the sandbox:\n{opened}")
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
        assert "Page URL" in execute(f"playwright-cli open {page_url}")
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
