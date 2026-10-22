"""Local FastMCP server for the MCP tests. Run as __main__ for stdio."""
from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token

FIXTURE_PATH = Path(__file__).resolve()


def build_server(auth: Any | None = None) -> FastMCP:
    mcp = FastMCP("deep-agent-fixture", auth=auth)

    @mcp.tool(annotations={"readOnlyHint": True, "title": "Echo"})
    def echo(text: str) -> str:
        """Return the text unchanged."""
        return text

    @mcp.tool
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    @mcp.tool
    def fail() -> str:
        """Always fail."""
        raise RuntimeError("fixture failure")

    @mcp.tool
    def sleep(seconds: float) -> str:
        """Sleep, then return."""
        time.sleep(max(0.0, seconds))
        return "awake"

    @mcp.tool
    async def wait_for_cancel(started: str, cancelled: str) -> str:
        """Wait until the MCP client sends cancellation."""
        Path(started).write_text("started")
        try:
            await asyncio.sleep(30)
            return "finished"
        except asyncio.CancelledError:
            import anyio
            with anyio.CancelScope(shield=True):
                await asyncio.sleep(.02)  # Exercise asynchronous server cleanup.
                Path(cancelled).write_text("cancelled")
            raise

    @mcp.tool
    def write_marker(path: str) -> str:
        """Write a marker file on the host."""
        Path(path).write_text("marker", encoding="utf-8")
        return "wrote"

    @mcp.tool
    def auth_check() -> str:
        """Report the authenticated client id, never the token."""
        token = get_access_token()
        if token is None:
            return "anonymous"
        return f"subject:{token.client_id}"

    return mcp


def stdio_argv() -> list[str]:
    return [sys.executable, str(FIXTURE_PATH)]


def text_of(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(text_of(item) for item in value)
    if isinstance(value, dict):
        return " ".join(text_of(item) for item in value.values())
    content = getattr(value, "content", None)
    if content is not None:
        return text_of(content)
    return str(value)


class HttpFixture:
    """One Streamable HTTP server on 127.0.0.1 with an ephemeral port."""

    def __init__(self, *, auth: Any | None = None, auth_factory: Any = None, server_factory: Any = None) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(128)
        self.port = self._sock.getsockname()[1]
        self.mcp = (server_factory or build_server)(auth_factory(f"http://127.0.0.1:{self.port}") if auth_factory else auth)
        self._server: Any = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name=f"mcp-fixture-{self.port}", daemon=True)
        self._thread.start()
        self._wait_until_ready()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def close(self) -> None:
        server = self._server
        if server is not None:
            server.should_exit = True
        self._thread.join(timeout=3)
        try:
            self._sock.close()
        except OSError:
            pass

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:  # noqa: BLE001 — surface startup failures to the waiter
            self._error = exc

    async def _serve(self) -> None:
        import uvicorn

        app = self.mcp.http_app(path="/mcp", transport="http", host_origin_protection=False)
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on")
        server = uvicorn.Server(config)
        self._server = server
        await server.serve(sockets=[self._sock])

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self._error is not None:
                raise RuntimeError("fixture server failed to start") from self._error
            try:
                urllib.request.urlopen(self.url, timeout=0.2)
                return
            except urllib.error.HTTPError:
                return
            except Exception:
                time.sleep(0.05)
        raise TimeoutError(f"fixture server did not accept HTTP on {self.url}")


if __name__ == "__main__":
    asyncio.run(build_server().run_stdio_async(show_banner=False, log_level="error"))


COMPAT_TOOL_NAMES = (
    "read.file", "read_file", "read file", "读取文件",
    "long_tool_" + "a" * 110 + "x", "long_tool_" + "a" * 110 + "y",
)


def build_compat_server(auth: Any | None = None) -> FastMCP:
    mcp = FastMCP("compatibility-fixture", auth=auth)

    def implementation(name):
        async def call(value: str) -> str:
            return f"{name}:{value}"
        return call

    for name in COMPAT_TOOL_NAMES:
        mcp.tool(name=name, description="Read a value from the original remote tool.")(
            implementation(name),
        )
    return mcp
