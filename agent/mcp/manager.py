"""MCP server catalog, connections, and the tool snapshot handed to the agent."""
from __future__ import annotations

import atexit
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
import threading
from typing import Any

from langchain_core.tools import BaseTool

from agent.mcp.adapter import close_client, open_server, redact
from agent.mcp.bridge import MCPCallCancelled, MCPCallTimeout, SyncAsyncBridge
from agent.mcp.config import (
    MCPAuthError,
    MCPConfigError,
    MCPFile,
    MCPServerConfig,
    atomic_write_text,
    dumps_mcp_file,
    load_mcp_file,
    server_from_fields,
)


class ServerStatus(StrEnum):
    DISABLED = "disabled"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    AUTH_REQUIRED = "auth_required"
    ERROR = "error"


@dataclass
class ServerState:
    config: MCPServerConfig
    status: ServerStatus
    error: str = ""
    tools: list[BaseTool] = field(default_factory=list)
    client: Any = None
    warnings: list[str] = field(default_factory=list)

    @property
    def tool_count(self) -> int:
        return len(self.tools)


@dataclass(frozen=True)
class MCPResult:
    ok: bool
    changed: bool
    message: str
    pending: bool = False


class MCPManager:
    """Owns mcp.json and the host-side MCP connections.

    Tool objects published to a graph stay valid until the next safe rebuild.
    Removing a server does not close a client the current graph still holds.
    """

    def __init__(
        self,
        path: Path,
        *,
        call_timeout: float = 120,
        connect_timeout: float = 20,
        oauth_callback_timeout: float = 120,
    ) -> None:
        self.path = path
        self._call_timeout = call_timeout
        self._connect_timeout = connect_timeout
        self._oauth_callback_timeout = oauth_callback_timeout
        self._lock = threading.RLock()
        self._bridge = SyncAsyncBridge()
        self._document = MCPFile()
        self._states: dict[str, ServerState] = {}
        self._retired: list[Any] = []
        self._published_clients: list[Any] = []
        self._revision = 0
        self._published_revision = 0
        self._config_error = ""
        self._loaded = False
        self._closed = False
        atexit.register(self.close)

    @property
    def config_error(self) -> str:
        return self._config_error

    def load(self) -> None:
        """Read the catalog. A later load marks the toolset dirty without rewriting the file."""
        document = load_mcp_file(self.path)
        with self._lock:
            first = not self._loaded
            for state in self._states.values():
                if state.client is not None:
                    self._retired.append(state.client)
            self._states = {}
            self._document = document
            self._config_error = document.fatal or ""
            self._loaded = True
            if not first:
                self._revision += 1
            servers = () if document.fatal else tuple(document.servers.values())
        self._close_unpublished()
        if document.fatal:
            return
        for server in servers:
            self._activate(server, bump=False)

    def list_servers(self) -> list[ServerState]:
        with self._lock:
            return [
                ServerState(
                    config=state.config,
                    status=state.status,
                    error=state.error,
                    tools=list(state.tools),
                    warnings=list(state.warnings),
                )
                for state in self._states.values()
            ]

    def get(self, name: str) -> ServerState | None:
        with self._lock:
            state = self._states.get(name)
            if state is None:
                return None
            return ServerState(
                config=state.config,
                status=state.status,
                error=state.error,
                tools=list(state.tools),
                warnings=list(state.warnings),
            )

    def invalid_message(self, name: str) -> str | None:
        with self._lock:
            item = self._document.invalid.get(name)
        return None if item is None else item[1]

    def describe(self) -> str:
        if self._config_error:
            return f"MCP config error: {self._config_error}\nThe file was not modified."
        rows = self.list_servers()
        with self._lock:
            invalid = dict(self._document.invalid)
        if not rows and not invalid:
            return "No MCP servers are configured."
        lines = ["MCP servers run on the host, outside the sandbox."]
        for state in rows:
            target = state.config.url or _command_line(state.config)
            error = f" — {state.error}" if state.error else ""
            lines.append(
                f"{state.config.name}  {state.config.transport}  {state.status.value}  "
                f"{state.tool_count} tools  {target}{error}"
            )
        for name, (_raw, error) in invalid.items():
            lines.append(f"{name}  invalid  {error}")
        return "\n".join(lines)

    def add_from_fields(self, **fields: Any) -> MCPResult:
        try:
            server = server_from_fields(**fields)
        except MCPConfigError as exc:
            return MCPResult(False, False, str(exc))
        return self.add(server)

    def add(self, server: MCPServerConfig) -> MCPResult:
        """Save a server and try to connect. Identical repeats are a no-op."""
        with self._lock:
            if self._closed:
                return MCPResult(False, False, "MCP manager is closed")
            if self._config_error:
                return MCPResult(False, False, f"MCP config error: {self._config_error} The file was not modified.")
            current = self._document.servers.get(server.name)
            if current is not None and current.fingerprint() != server.fingerprint():
                return MCPResult(
                    False, False,
                    f"MCP server {server.name} already exists with a different configuration. "
                    "Remove it before adding it again. The file was not modified.",
                )
            already = current is not None and current.fingerprint() == server.fingerprint()
            existing = self._states.get(server.name)
            if already and existing is not None and existing.status is ServerStatus.CONNECTED:
                names = ", ".join(tool.name for tool in existing.tools) or "(none)"
                return MCPResult(
                    True, False,
                    f"MCP server {server.name} is already connected. {existing.tool_count} tools: {names}.",
                )
            if not already:
                self._document.servers[server.name] = server
                try:
                    self._write_locked()
                except OSError as exc:
                    self._document.servers.pop(server.name, None)
                    return MCPResult(False, False, f"Could not save MCP config: {exc.strerror or exc}")
        if not server.enabled:
            with self._lock:
                self._states[server.name] = ServerState(config=server, status=ServerStatus.DISABLED)
                self._revision += 1
            return MCPResult(True, not already, f"Saved MCP server {server.name} as disabled. It exposes no tools.", pending=True)
        state = self._activate(server, bump=True)
        if state.status is ServerStatus.CONNECTED:
            names = ", ".join(tool.name for tool in state.tools) or "(none)"
            prefix = "already configured and connected" if already else "saved and connected"
            return MCPResult(
                True, not already,
                f"MCP server {server.name} is {prefix}. Discovered {state.tool_count} tools: {names}.",
                pending=True,
            )
        if state.status is ServerStatus.AUTH_REQUIRED:
            return MCPResult(
                False, not already,
                f"MCP server {server.name} needs authentication: {state.error} Tools are not available.",
                pending=True,
            )
        return MCPResult(
            False, not already,
            f"MCP server {server.name} was saved but is not connected: {state.error} Tools are not available.",
            pending=True,
        )

    def remove(self, name: str) -> MCPResult:
        with self._lock:
            if self._closed:
                return MCPResult(False, False, "MCP manager is closed")
            if self._config_error:
                return MCPResult(False, False, f"MCP config error: {self._config_error} The file was not modified.")
            if name not in self._document.servers and name not in self._document.invalid:
                return MCPResult(False, False, f"No MCP server named {name}. The file was not modified.")
            removed_server = self._document.servers.pop(name, None)
            removed_invalid = self._document.invalid.pop(name, None)
            try:
                self._write_locked()
            except OSError as exc:
                if removed_server is not None:
                    self._document.servers[name] = removed_server
                if removed_invalid is not None:
                    self._document.invalid[name] = removed_invalid
                return MCPResult(False, False, f"Could not save MCP config: {exc.strerror or exc}")
            state = self._states.pop(name, None)
            if state is not None and state.client is not None:
                self._retired.append(state.client)
            self._revision += 1
        self._close_unpublished()
        return MCPResult(
            True, True,
            f"Removed MCP server {name}. Tools leave the agent at the next safe point. "
            "A graph that already started this call keeps its current tools until then.",
        )

    def test(self, name: str) -> MCPResult:
        with self._lock:
            if self._config_error:
                return MCPResult(False, False, f"MCP config error: {self._config_error}")
            server = self._document.servers.get(name)
            invalid = self._document.invalid.get(name)
        if invalid is not None:
            return MCPResult(False, False, invalid[1])
        if server is None:
            return MCPResult(False, False, f"No MCP server named {name}.")
        if not server.enabled:
            return MCPResult(True, False, f"{name} is disabled.")
        state = self._activate(server, bump=True)
        if state.status is ServerStatus.CONNECTED:
            names = ", ".join(tool.name for tool in state.tools) or "(none)"
            return MCPResult(True, False, f"{name} connected. {state.tool_count} tools: {names}.")
        return MCPResult(False, False, f"{name} {state.status.value}: {state.error}")

    def snapshot(self) -> tuple[int, list[BaseTool]]:
        with self._lock:
            tools: list[BaseTool] = []
            for state in self._states.values():
                if state.config.enabled and state.status is ServerStatus.CONNECTED:
                    tools.extend(state.tools)
            return self._revision, tools

    def commit(self, revision: int) -> None:
        with self._lock:
            live = [
                state.client
                for state in self._states.values()
                if state.client is not None and state.status is ServerStatus.CONNECTED
            ]
            published = set(id(client) for client in live)
            for client in self._published_clients:
                if id(client) not in published:
                    self._retired.append(client)
            self._published_clients = live
            self._published_revision = revision
        self._close_unpublished()

    def toolset_dirty(self) -> bool:
        with self._lock:
            return self._revision != self._published_revision

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            clients = [state.client for state in self._states.values() if state.client is not None]
            clients.extend(self._published_clients)
            clients.extend(self._retired)
            self._retired.clear()
            for state in self._states.values():
                state.client = None
                state.status = ServerStatus.DISCONNECTED if state.status is ServerStatus.CONNECTED else state.status
        self._close_clients(clients)
        self._bridge.shutdown()

    def _activate(self, server: MCPServerConfig, *, bump: bool) -> ServerState:
        if not server.enabled:
            state = ServerState(config=server, status=ServerStatus.DISABLED)
            with self._lock:
                previous = self._states.get(server.name)
                if previous is not None and previous.client is not None:
                    self._retired.append(previous.client)
                self._states[server.name] = state
                if bump:
                    self._revision += 1
            self._close_unpublished()
            return state
        try:
            loaded = open_server(
                server,
                self._bridge,
                call_timeout=self._call_timeout,
                connect_timeout=self._connect_timeout,
                oauth_callback_timeout=self._oauth_callback_timeout,
            )
        except MCPAuthError as exc:
            state = ServerState(config=server, status=ServerStatus.AUTH_REQUIRED, error=_public_error(exc))
        except (MCPCallTimeout, MCPCallCancelled) as exc:
            message = _public_error(exc)
            if server.auth is not None and server.auth.type == "oauth":
                message = "OAuth authorization timed out or was cancelled"
                state = ServerState(config=server, status=ServerStatus.AUTH_REQUIRED, error=message)
            else:
                state = ServerState(config=server, status=ServerStatus.ERROR, error=message)
        except Exception as exc:  # noqa: BLE001 — one server must not block the others
            message = _public_error(exc)
            auth_type = server.auth.type if server.auth is not None else ""
            if auth_type == "bearer" and "error response" in message.lower():
                message = "Authentication failed"
            if auth_type == "oauth" or message == "Authentication failed" or _auth_text(message):
                status = ServerStatus.AUTH_REQUIRED
            else:
                status = ServerStatus.ERROR
            state = ServerState(config=server, status=status, error=message)
        else:
            state = ServerState(
                config=server,
                status=ServerStatus.CONNECTED,
                tools=loaded.tools,
                client=loaded.client,
                warnings=loaded.warnings,
            )
        with self._lock:
            previous = self._states.get(server.name)
            if previous is not None and previous.client is not None and previous.client is not state.client:
                self._retired.append(previous.client)
            self._states[server.name] = state
            if bump:
                self._revision += 1
        self._close_unpublished()
        return state

    def _write_locked(self) -> None:
        atomic_write_text(self.path, dumps_mcp_file(self._document))

    def _close_unpublished(self) -> None:
        with self._lock:
            published = {id(client) for client in self._published_clients}
            closing = [client for client in self._retired if id(client) not in published]
            self._retired = [client for client in self._retired if id(client) in published]
        self._close_clients(closing)

    def _close_clients(self, clients: list[Any]) -> None:
        seen: set[int] = set()
        for client in clients:
            if client is None or id(client) in seen:
                continue
            seen.add(id(client))
            try:
                self._bridge.call(close_client(client), timeout=5)
            except Exception:  # noqa: BLE001 — shutdown still has to finish
                pass


def _command_line(server: MCPServerConfig) -> str:
    return " ".join((server.command or "", *server.args)).strip()


def _public_error(exc: BaseException) -> str:
    text = redact(str(exc), [])
    text = " ".join(text.split())
    return text or exc.__class__.__name__


def _auth_text(message: str) -> bool:
    folded = message.lower()
    return any(token in folded for token in ("401", "unauthorized", "invalid_token", "oauth", "not set"))

