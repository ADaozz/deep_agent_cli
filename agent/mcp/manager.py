"""MCP server catalog, connections, and the tool snapshot handed to the agent."""
from __future__ import annotations

import atexit
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
import threading
from typing import Any, NamedTuple

from langchain_core.tools import BaseTool

from agent.mcp.adapter import close_client, open_server
from agent.mcp.bridge import MCPCallCancelled, MCPCallTimeout, SyncAsyncBridge
from agent.mcp.config import (
    MCPAuthError,
    MCPConfigError,
    MCPFile,
    MCPServerConfig,
    atomic_write_text,
    dumps_mcp_file,
    load_mcp_file,
    parse_server,
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


class _Snapshot(NamedTuple):
    clients: list[Any]
    count: int
    include_mcp: bool


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
        trust_env: bool = False,
    ) -> None:
        self.path = path
        self._trust_env = trust_env
        self._call_timeout = call_timeout
        self._connect_timeout = connect_timeout
        self._oauth_callback_timeout = oauth_callback_timeout
        self._lock = threading.RLock()
        self._bridge = SyncAsyncBridge()
        self._document = MCPFile()
        self._states: dict[str, ServerState] = {}
        self._retired: list[Any] = []
        self._published_clients: list[Any] = []
        self._versions: dict[str, int] = {}
        self._connecting: dict[int, Future[ServerState]] = {}
        self._oauth_browser_attempts: dict[str, float] = {}
        self._connection_cancel = threading.Event()
        self._attempt_cancels: dict[int, threading.Event] = {}
        self._activation_epoch = 0
        self._switch_epoch = 0
        self._switch_notice: str | None = None
        self._snapshots: dict[int, _Snapshot] = {}
        self._revision = 0
        self._published_revision = 0
        self._config_error = ""
        self._loaded = False
        self._closed = False
        atexit.register(self.close)

    @property
    def enabled(self) -> bool:
        """Global MCP switch. A broken catalog stays inactive so startup cannot connect."""
        with self._lock:
            return self._document.enabled and not self._config_error

    @property
    def switch_epoch(self) -> int:
        """Increments on each global enable or disable. Stale connection tasks compare it."""
        with self._lock:
            return self._switch_epoch

    @property
    def config_error(self) -> str:
        return self._config_error

    def load(self) -> None:
        """Read the catalog. A later load marks the toolset dirty without rewriting the file."""
        with self._lock:
            if self._closed:
                return
            # Serialize disk reads with our writes, so a slow reload cannot
            # resurrect a configuration removed while it was being read.
            document = load_mcp_file(self.path)
            for name in self._versions:
                self._versions[name] += 1
            for state in self._states.values():
                if state.client is not None:
                    self._retired.append(state.client)
            self._document = document
            self._config_error = document.fatal or ""
            self._loaded = True
            self._revision += 1
            self._states = {
                name: ServerState(
                    config=server,
                    status=ServerStatus.DISCONNECTED if server.enabled else ServerStatus.DISABLED,
                )
                for name, server in document.servers.items()
            }
        self._close_unpublished()

    def set_enabled(self, enabled: bool) -> MCPResult:
        """Persist the global MCP switch and drop or restore tool publication.

        Disable cancels this round of connection attempts without using the
        permanent shutdown flag. A later enable can connect again. Clients a
        published graph still holds stay open until that graph is rebuilt.
        """
        if not isinstance(enabled, bool):
            return MCPResult(False, False, "MCP enabled must be a boolean. The file was not modified.")
        if not self._loaded and self.path.exists():
            # Read before creating a switch file, but do not reload a catalog
            # add() or remove() has already written. load() closes clients,
            # so it must run outside the switch lock.
            self.load()
        with self._lock:
            if self._closed or self._connection_cancel.is_set():
                return MCPResult(False, False, "MCP manager is closed")
            if not self._loaded:
                self._loaded = True
            if self._config_error:
                return MCPResult(
                    False, False,
                    f"MCP config error: {self._config_error} The file was not modified.",
                )
            if self._document.enabled is enabled:
                state = "enabled" if enabled else "disabled"
                return MCPResult(True, False, f"MCP is already {state}.")
            previous = self._document.enabled
            self._document.enabled = enabled
            try:
                self._write_locked()
            except OSError as exc:
                self._document.enabled = previous
                return MCPResult(False, False, f"Could not save MCP config: {exc.strerror or exc}")
            if enabled:
                self._switch_epoch += 1
                self._switch_notice = "enabled"
                self._revision += 1
            else:
                self._deactivate_connections_locked()
        if not enabled:
            self._close_unpublished()
        return MCPResult(True, True, "MCP enabled." if enabled else "MCP disabled.", pending=True)

    def consume_switch_notice(self) -> str | None:
        """Return the pending enable/disable notice once the graph has published it."""
        with self._lock:
            if self._revision != self._published_revision:
                return None
            notice = self._switch_notice
            self._switch_notice = None
            return notice

    def connect_pending(self) -> MCPResult:
        """Connect at startup using cached credentials, refreshing or authorizing as needed."""
        with self._lock:
            if self._closed or self._connection_cancel.is_set():
                return MCPResult(False, False, "MCP connections are shutting down")
            if not self._document.enabled:
                return MCPResult(True, False, "MCP is disabled.")
            names = [
                name for name, state in self._states.items()
                if state.config.enabled and state.status is ServerStatus.DISCONNECTED
            ]
        results = [self.test(name) for name in names]
        return MCPResult(
            all(result.ok for result in results), False,
            "\n".join(result.message for result in results) or
            "MCP catalog loaded.",
        )

    def list_servers(self) -> list[ServerState]:
        with self._lock:
            return [
                replace(state, client=None, tools=list(state.tools), warnings=list(state.warnings))
                for state in self._states.values()
            ]

    def get(self, name: str) -> ServerState | None:
        with self._lock:
            state = self._states.get(name)
            if state is None:
                return None
            return replace(state, client=None, tools=list(state.tools), warnings=list(state.warnings))

    def invalid_message(self, name: str) -> str | None:
        with self._lock:
            item = self._document.invalid.get(name)
        return None if item is None else item[1]

    def invalid_servers(self) -> dict[str, str]:
        with self._lock:
            return {name: error for name, (_raw, error) in self._document.invalid.items()}

    def describe(self) -> str:
        if self._config_error:
            return f"MCP config error: {self._config_error}\nThe file was not modified."
        rows = self.list_servers()
        invalid = self.invalid_servers()
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
            for warning in state.warnings:
                lines.append(f"  Warning: {warning}")
        for name, error in invalid.items():
            lines.append(f"{name}  invalid  {error}")
        return "\n".join(lines)

    def add_from_fields(self, *, allow_oauth_browser: bool = True, **fields: Any) -> MCPResult:
        try:
            server = server_from_fields(**fields)
        except MCPConfigError as exc:
            return MCPResult(False, False, str(exc))
        return self.add(server, allow_oauth_browser=allow_oauth_browser)

    def add(self, server: MCPServerConfig, *, allow_oauth_browser: bool = True) -> MCPResult:
        """Save a server and try to connect. Identical repeats are a no-op."""
        try:
            server = parse_server(server.name, server.to_json())
        except MCPConfigError as exc:
            return MCPResult(False, False, str(exc))
        with self._lock:
            if self._closed:
                return MCPResult(False, False, "MCP manager is closed")
            if self._config_error:
                return MCPResult(False, False, f"MCP config error: {self._config_error} The file was not modified.")
            if not self._document.enabled:
                return MCPResult(
                    False, False,
                    "MCP is disabled. Run /mcp enable first. The file was not modified.",
                )
            current = self._document.servers.get(server.name)
            if current is not None and current.fingerprint() != server.fingerprint():
                return MCPResult(
                    False, False,
                    f"MCP server {server.name} already exists with a different configuration. "
                    "Remove it before adding it again. The file was not modified.",
                )
            already = current is not None and current.fingerprint() == server.fingerprint()
            if already:
                server = current
            existing = self._states.get(server.name)
            if (
                already and existing is not None and existing.status is ServerStatus.CONNECTED
                and _client_connected(existing.client)
            ):
                names = ", ".join(tool.name for tool in existing.tools) or "(none)"
                return MCPResult(
                    True, False,
                    f"MCP server {server.name} is already connected. {existing.tool_count} tools: {names}." + _warning_summary(existing),
                )
            if not already:
                self._document.servers[server.name] = server
                try:
                    self._write_locked()
                except OSError as exc:
                    self._document.servers.pop(server.name, None)
                    return MCPResult(False, False, f"Could not save MCP config: {exc.strerror or exc}")
        state = self._activate(server, allow_oauth_browser=allow_oauth_browser)
        if state.status is ServerStatus.DISABLED:
            return MCPResult(True, not already, f"Saved MCP server {server.name} as disabled. It exposes no tools.", pending=True)
        if state.status is ServerStatus.CONNECTED:
            names = ", ".join(tool.name for tool in state.tools) or "(none)"
            prefix = "already configured and connected" if already else "saved and connected"
            return MCPResult(
                True, not already,
                f"MCP server {server.name} is {prefix}. Discovered {state.tool_count} tools: {names}." + _warning_summary(state),
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
            self._versions[name] = self._versions.get(name, 0) + 1
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

    def test(self, name: str, *, allow_oauth_browser: bool = True) -> MCPResult:
        with self._lock:
            if self._config_error:
                return MCPResult(False, False, f"MCP config error: {self._config_error}")
            if not self._document.enabled:
                return MCPResult(False, False, "MCP is disabled. Run /mcp enable first.")
            server = self._document.servers.get(name)
            invalid = self._document.invalid.get(name)
        if invalid is not None:
            return MCPResult(False, False, invalid[1])
        if server is None:
            return MCPResult(False, False, f"No MCP server named {name}.")
        if not server.enabled:
            return MCPResult(True, False, f"{name} is disabled.")
        state = self._activate(server, allow_oauth_browser=allow_oauth_browser)
        if state.status is ServerStatus.CONNECTED:
            names = ", ".join(tool.name for tool in state.tools) or "(none)"
            return MCPResult(True, False, f"{name} connected. {state.tool_count} tools: {names}." + _warning_summary(state))
        return MCPResult(False, False, f"{name} {state.status.value}: {state.error}")

    def snapshot(self) -> tuple[int, list[BaseTool]]:
        """Pin this revision's clients until commit() or discard() finishes the build.

        A disabled catalog still takes a snapshot so the empty tool list can be
        published. Otherwise the revision stays dirty and the graph rebuilds forever.
        """
        with self._lock:
            include = self._document.enabled and not self._config_error
            tools: list[BaseTool] = []
            clients: list[Any] = []
            if include:
                for state in self._states.values():
                    if state.config.enabled and state.status is ServerStatus.CONNECTED and state.client is not None:
                        tools.extend(state.tools)
                        clients.append(state.client)
            current = self._snapshots.get(self._revision)
            count = current.count if current is not None else 0
            self._snapshots[self._revision] = _Snapshot(clients, count + 1, include)
            return self._revision, tools

    def snapshot_includes_mcp(self, revision: int) -> bool:
        """Whether the graph built from this snapshot should register MCP tools."""
        with self._lock:
            current = self._snapshots.get(revision)
            return bool(current.include_mcp) if current is not None else False

    def discard(self, revision: int) -> None:
        """Release a failed graph build's snapshot without changing publication."""
        with self._lock:
            self._discard_locked(revision)
        self._close_unpublished()

    def _discard_locked(self, revision: int) -> None:
        item = self._snapshots.get(revision)
        if item is not None:
            if item.count > 1:
                self._snapshots[revision] = _Snapshot(item.clients, item.count - 1, item.include_mcp)
            else:
                self._snapshots.pop(revision)

    def commit(self, revision: int) -> None:
        with self._lock:
            snapshot = self._snapshots.get(revision)
            if self._closed or revision != self._revision or snapshot is None:
                raise RuntimeError("MCP registry changed during graph build; retry at the next safe point")
            live = snapshot.clients
            published = {id(client) for client in live}
            for client in self._published_clients:
                if id(client) not in published:
                    self._retired.append(client)
            self._published_clients = list(live)
            self._published_revision = revision
            self._discard_locked(revision)
        self._close_unpublished()

    def toolset_dirty(self) -> bool:
        with self._lock:
            return self._revision != self._published_revision

    def cancel_pending_connections(self) -> None:
        """Stop startup/authorization work when the UI exits; leave active tools to runner.close().

        This is permanent for the process. Runtime disable uses a separate,
        reversible cancellation of the current connection round.
        """
        self._connection_cancel.set()
        with self._lock:
            for event in self._attempt_cancels.values():
                event.set()
            self._activation_epoch += 1

    def close(self) -> None:
        self.cancel_pending_connections()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            clients = [state.client for state in self._states.values() if state.client is not None]
            clients.extend(self._published_clients)
            clients.extend(self._retired)
            clients.extend(client for snapshot in self._snapshots.values() for client in snapshot.clients)
            self._snapshots.clear()
            self._retired.clear()
            for state in self._states.values():
                state.client = None
                state.status = ServerStatus.DISCONNECTED if state.status is ServerStatus.CONNECTED else state.status
        self._close_clients(clients)
        self._bridge.shutdown()

    def _deactivate_connections_locked(self) -> None:
        """Cancel the current connection round and hide MCP tools until republished."""
        self._activation_epoch += 1
        self._switch_epoch += 1
        self._switch_notice = "disabled"
        for event in self._attempt_cancels.values():
            event.set()
        for name in list(self._versions):
            self._versions[name] += 1
        for state in self._states.values():
            if state.client is not None:
                self._retired.append(state.client)
                state.client = None
            state.tools = []
            state.warnings = []
            if state.config.enabled:
                state.status = ServerStatus.DISCONNECTED
                state.error = ""
        self._revision += 1

    def _activate(self, server: MCPServerConfig, *, allow_oauth_browser: bool) -> ServerState:
        # Startup, /mcp test and mcp_add share an in-flight connection for the
        # same catalog entry. Removal/reload still invalidate the old attempt.
        key = id(server)
        with self._lock:
            if self._closed or self._connection_cancel.is_set():
                return ServerState(server, ServerStatus.DISCONNECTED, "MCP connections are shutting down")
            if not self._document.enabled:
                return ServerState(server, ServerStatus.DISCONNECTED, "MCP is disabled")
            attempt = self._connecting.get(key)
            owner = attempt is None
            if owner:
                attempt = Future()
                self._connecting[key] = attempt
                cancel = threading.Event()
                self._attempt_cancels[key] = cancel
        if not owner:
            return attempt.result()
        try:
            state = self._activate_once(
                server, allow_oauth_browser=allow_oauth_browser, cancel_event=cancel,
            )
        except BaseException as exc:
            attempt.set_exception(exc)
            raise
        else:
            attempt.set_result(state)
            return state
        finally:
            with self._lock:
                self._connecting.pop(key, None)
                self._attempt_cancels.pop(key, None)

    def _activate_once(
        self,
        server: MCPServerConfig,
        *,
        allow_oauth_browser: bool,
        cancel_event: threading.Event,
    ) -> ServerState:
        with self._lock:
            if (
                self._closed or self._connection_cancel.is_set() or not self._document.enabled
                or self._document.servers.get(server.name) is not server
            ):
                return ServerState(server, ServerStatus.DISCONNECTED, "Connection superseded by a catalog change")
            epoch = self._activation_epoch
            version = self._versions.get(server.name, 0) + 1
            self._versions[server.name] = version
        try:
            if not server.enabled:
                loaded = None
            else:
                loaded = open_server(
                    server,
                    self._bridge,
                    call_timeout=self._call_timeout,
                    connect_timeout=self._connect_timeout,
                    oauth_callback_timeout=self._oauth_callback_timeout,
                    trust_env=self._trust_env,
                    oauth_storage_dir=self.path.parent / "oauth",
                    allow_oauth_browser=allow_oauth_browser,
                    oauth_browser_attempts=self._oauth_browser_attempts,
                    cancel_event=cancel_event,
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
                status=ServerStatus.CONNECTED if loaded is not None else ServerStatus.DISABLED,
                tools=loaded.tools if loaded is not None else [],
                client=loaded.client if loaded is not None else None,
                warnings=loaded.warnings if loaded is not None else [],
            )
        with self._lock:
            valid = (
                not self._closed and not self._connection_cancel.is_set()
                and self._document.enabled and self._activation_epoch == epoch
                and self._versions.get(server.name) == version
                and self._document.servers.get(server.name) is server
            )
            if valid:
                previous = self._states.get(server.name)
                if previous is not None and previous.client is not None and previous.client is not state.client:
                    self._retired.append(previous.client)
                self._states[server.name] = state
                self._revision += 1
        if not valid:
            self._close_clients([state.client])
            return ServerState(server, ServerStatus.DISCONNECTED, "Connection superseded by a catalog change")
        self._close_unpublished()
        return state

    def _write_locked(self) -> None:
        atomic_write_text(self.path, dumps_mcp_file(self._document))
        self._loaded = True

    def _close_unpublished(self) -> None:
        with self._lock:
            published = {id(client) for client in self._published_clients}
            published.update(
                id(client) for snapshot in self._snapshots.values() for client in snapshot.clients
            )
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


def _warning_summary(state: ServerState) -> str:
    return "".join(f"\nWarning: {note}" for note in state.warnings)


def _command_line(server: MCPServerConfig) -> str:
    return " ".join((server.command or "", *server.args)).strip()


def _client_connected(client: Any) -> bool:
    check = getattr(client, "is_connected", None)
    return bool(check()) if callable(check) else True


def _public_error(exc: BaseException) -> str:
    text = str(exc)[:400]
    text = " ".join(text.split())
    return text or exc.__class__.__name__


def _auth_text(message: str) -> bool:
    folded = message.lower()
    return any(token in folded for token in ("401", "unauthorized", "invalid_token", "oauth", "not set"))

