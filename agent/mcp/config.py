"""Parse and atomically store ~/.deep-agent/mcp.json.

The file is an internal catalog. It is not passed through to MCPAdapter.
Tokens stay in the environment; this file only records where to read them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import unquote, urlparse


class MCPConfigError(ValueError):
    """The MCP catalog is missing a required field or has an illegal value."""


class MCPAuthError(MCPConfigError):
    """A server needs credentials that are missing or were not accepted."""


_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,32}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_FIELD = re.compile(r"(?i)token|secret|password|authorization|api_key|apikey")
_URL_SECRET = re.compile(r"(?i)(token|access_token|api_key|key|password)=")


# Deliberately limited to explicit credential flags, not paths or arbitrary words.
_SECRET_ARG = re.compile(r"(?i)^--(?:token|access-token|api-key|api_key|password|secret|client-secret)(?:=|$)")


@dataclass(frozen=True)
class MCPAuth:
    type: str
    token_env: str | None = None

    def to_json(self) -> dict[str, str]:
        if self.type == "bearer":
            return {"type": "bearer", "token_env": self.token_env or ""}
        return {"type": "oauth"}


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    transport: str
    enabled: bool = True
    url: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    env_passthrough: tuple[str, ...] = ()
    auth: MCPAuth | None = None

    def to_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {"transport": self.transport, "enabled": self.enabled}
        if self.transport == "streamable_http":
            body["url"] = self.url
            if self.auth is not None:
                body["auth"] = self.auth.to_json()
        else:
            body["command"] = self.command
            body["args"] = list(self.args)
            if self.env_passthrough:
                body["env_passthrough"] = list(self.env_passthrough)
        return body

    def fingerprint(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True, ensure_ascii=False)


@dataclass
class MCPFile:
    servers: dict[str, MCPServerConfig] = field(default_factory=dict)
    invalid: dict[str, tuple[Any, str]] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)
    fatal: str | None = None
    enabled: bool = True

    def to_json(self) -> dict[str, Any]:
        servers: dict[str, Any] = {}
        for name, server in self.servers.items():
            servers[name] = server.to_json()
        for name, (raw, _error) in self.invalid.items():
            if name not in servers:
                servers[name] = raw
        body = dict(self.extras)
        body.pop("enabled", None)
        body["enabled"] = self.enabled
        body["servers"] = servers
        return body


def default_mcp_path(config_dir: Path | None = None) -> Path:
    root = config_dir if config_dir is not None else Path.home() / ".deep-agent"
    return root.expanduser() / "mcp.json"


def parse_server(name: str, raw: Any) -> MCPServerConfig:
    if not isinstance(raw, dict):
        raise MCPConfigError(f"MCP server {name} must be an object")
    if not _NAME.fullmatch(name):
        raise MCPConfigError(
            "MCP server name must start with a letter and contain only letters, digits, '_' or '-'"
        )
    _reject_secret_fields(name, raw)
    transport = raw.get("transport")
    if transport not in {"streamable_http", "stdio"}:
        raise MCPConfigError(f"MCP server {name} transport must be streamable_http or stdio")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise MCPConfigError(f"MCP server {name} enabled must be a boolean")
    unknown = set(raw) - {"transport", "enabled", "url", "command", "args", "env_passthrough", "auth"}
    if unknown:
        raise MCPConfigError(f"MCP server {name} has unknown fields: {', '.join(sorted(unknown))}")
    if transport == "streamable_http":
        return MCPServerConfig(
            name=name,
            transport=transport,
            enabled=enabled,
            url=_http_url(name, raw.get("url")),
            auth=_auth(name, raw.get("auth")),
        )
    if raw.get("auth") is not None:
        raise MCPConfigError(
            f"MCP server {name} uses stdio; pass secrets with env_passthrough instead of HTTP auth"
        )
    if raw.get("url"):
        raise MCPConfigError(f"MCP server {name} uses stdio and cannot set url")
    return MCPServerConfig(
        name=name,
        transport="stdio",
        enabled=enabled,
        command=_command(name, raw.get("command")),
        args=_args(name, raw.get("args", [])),
        env_passthrough=_env_names(name, raw.get("env_passthrough", [])),
    )


def server_from_fields(
    *,
    name: str,
    transport: str,
    url: str = "",
    command: str = "",
    args: list[str] | None = None,
    env_passthrough: list[str] | None = None,
    auth_type: str = "none",
    token_env: str = "",
    enabled: bool = True,
) -> MCPServerConfig:
    """Build a server from an agent tool call or the /mcp wizard."""
    body: dict[str, Any] = {"transport": transport, "enabled": enabled}
    if transport == "streamable_http":
        body["url"] = url.strip()
        auth = (auth_type or "none").strip().lower()
        if auth not in {"none", "bearer", "oauth"}:
            raise MCPConfigError("auth_type must be none, bearer, or oauth")
        if auth == "bearer":
            body["auth"] = {"type": "bearer", "token_env": token_env.strip()}
        elif auth == "oauth":
            if token_env.strip():
                raise MCPConfigError("OAuth does not take a token; leave token_env empty")
            body["auth"] = {"type": "oauth"}
    elif transport == "stdio":
        command_text = command.strip()
        argv = list(args or [])
        if not argv and command_text:
            import shlex
            try:
                parts = shlex.split(command_text)
            except ValueError as exc:
                raise MCPConfigError(f"Cannot parse the stdio command: {exc}") from exc
            if not parts:
                raise MCPConfigError("stdio command is empty")
            command_text, argv = parts[0], parts[1:]
        body["command"] = command_text
        body["args"] = argv
        if env_passthrough:
            body["env_passthrough"] = list(env_passthrough)
        if (auth_type or "none").strip().lower() not in {"", "none"}:
            raise MCPConfigError("stdio servers do not use HTTP auth; use env_passthrough for secrets")
    return parse_server(name.strip(), body)


def load_mcp_file(path: Path) -> MCPFile:
    if not path.exists():
        return MCPFile()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return MCPFile(fatal=f"Cannot read MCP config: {exc.strerror or exc}")
    except UnicodeError:
        return MCPFile(fatal="MCP config is not valid UTF-8")
    if not text.strip():
        return MCPFile(fatal="MCP config is empty")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return MCPFile(fatal=f"MCP config is not valid JSON ({exc.lineno}:{exc.colno})")
    if not isinstance(data, dict):
        return MCPFile(fatal="MCP config must be a JSON object")
    servers_raw = data.get("servers", {})
    if not isinstance(servers_raw, dict):
        return MCPFile(fatal="MCP config field servers must be an object")
    if "enabled" in data:
        enabled = data["enabled"]
        if not isinstance(enabled, bool):
            return MCPFile(fatal="MCP config field enabled must be a boolean")
    else:
        enabled = True
    parsed: dict[str, MCPServerConfig] = {}
    invalid: dict[str, tuple[Any, str]] = {}
    for name, raw in servers_raw.items():
        if not isinstance(name, str):
            return MCPFile(fatal="MCP server names must be strings")
        try:
            server = parse_server(name, raw)
        except MCPConfigError as exc:
            invalid[name] = (raw, str(exc))
            continue
        parsed[name] = server
    extras = {key: value for key, value in data.items() if key not in {"servers", "enabled"}}
    return MCPFile(servers=parsed, invalid=invalid, extras=extras, enabled=enabled)


def dumps_mcp_file(document: MCPFile) -> str:
    return json.dumps(document.to_json(), indent=2, ensure_ascii=False) + "\n"


def atomic_write_text(path: Path, text: str) -> None:
    """Replace path only after the new contents are flushed in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".mcp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _reject_secret_fields(name: str, raw: dict[str, Any]) -> None:
    auth = raw.get("auth")
    if isinstance(auth, dict):
        for key in auth:
            if key in {"type", "token_env"}:
                continue
            if _SECRET_FIELD.search(str(key)):
                raise MCPConfigError(
                    f"MCP server {name} keeps a secret field in auth; store only token_env"
                )
    env = raw.get("env")
    if isinstance(env, dict) and env:
        raise MCPConfigError(
            f"MCP server {name} must not store environment values; use env_passthrough names"
        )


def _http_url(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MCPConfigError(f"MCP server {name} requires an http(s) url")
    url = value.strip()
    try:
        parsed = urlparse(url)
        parsed.port  # Validate malformed authority/ports without echoing the URL.
    except ValueError:
        raise MCPConfigError(f"MCP server {name} has an invalid URL authority") from None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MCPConfigError(f"MCP server {name} url must be an http or https URL")
    if parsed.username is not None or parsed.password is not None:
        raise MCPConfigError(f"MCP server {name} url must not carry userinfo credentials")
    if _URL_SECRET.search(unquote(parsed.query)) or _URL_SECRET.search(url):
        raise MCPConfigError(f"MCP server {name} url must not carry a token or key")
    return url


def _auth(name: str, value: Any) -> MCPAuth | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MCPConfigError(f"MCP server {name} auth must be an object")
    kind = value.get("type")
    if kind == "bearer":
        token_env = value.get("token_env")
        if not isinstance(token_env, str) or not _ENV_NAME.fullmatch(token_env):
            raise MCPConfigError(f"MCP server {name} token_env must be an environment variable name")
        return MCPAuth(type="bearer", token_env=token_env)
    if kind == "oauth":
        if "token_env" in value:
            raise MCPConfigError(f"MCP server {name} OAuth auth cannot store token_env")
        return MCPAuth(type="oauth")
    raise MCPConfigError(f"MCP server {name} auth type must be bearer or oauth")


def _command(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MCPConfigError(f"MCP server {name} requires a stdio command")
    command = value.strip()
    if any(char.isspace() for char in command):
        raise MCPConfigError(f"MCP server {name} command must be one executable, with arguments in args")
    return command


def _args(name: str, value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MCPConfigError(f"MCP server {name} args must be a list of strings")
    if any(_SECRET_ARG.match(item) for item in value):
        raise MCPConfigError(
            f"MCP server {name} args must not contain credential flags; use env_passthrough"
        )
    return tuple(value)


def _env_names(name: str, value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and _ENV_NAME.fullmatch(item) for item in value):
        raise MCPConfigError(f"MCP server {name} env_passthrough must be a list of environment variable names")
    return tuple(value)
