"""Independent MCP profiles; no credentials or local process transports."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import yaml


def config_directory(configured: Path | None = None) -> Path:
    override = os.environ.get("DEEP_AGENT_CONFIG_DIR")
    return Path(override).expanduser() if override else configured or Path.home() / ".deep-agent"


def redacted_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))


@dataclass(frozen=True)
class MCPProfile:
    name: str
    url: str
    transport: str = "streamable_http"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", self.name) or self.name.lower() in {"none", "add"}:
            raise ValueError("MCP name must start with a letter, contain letters/digits/_/-, and cannot be none or add")
        if self.transport not in {"streamable_http", "sse"}:
            raise ValueError("MCP transport must be streamable_http or sse; local commands are unsupported")
        try:
            parts = urlsplit(self.url)
            _ = parts.port
        except ValueError:
            raise ValueError("Invalid MCP HTTP(S) URL") from None
        if parts.scheme not in {"http", "https"} or not parts.hostname or any(c.isspace() for c in self.url):
            raise ValueError("MCP URL must be an absolute HTTP(S) URL")
        if parts.username is not None or parts.password is not None or parts.fragment:
            raise ValueError("MCP URL cannot contain credentials or a fragment")
        if any(re.search(r"token|key|auth|secret|password|credential|signature", key, re.I)
               for key, _ in parse_qsl(parts.query)):
            raise ValueError("MCP URL cannot contain authentication tokens; authenticated servers are not supported yet")


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader: _UniqueLoader, node: yaml.MappingNode) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise ValueError("Duplicate MCP configuration key")
        result[key] = loader.construct_object(value_node)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


@dataclass(frozen=True)
class MCPConfig:
    current: str = "none"
    profiles: dict[str, MCPProfile] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.current != "none" and self.current not in self.profiles:
            raise ValueError("Current MCP profile does not exist")
        if any(name != profile.name for name, profile in self.profiles.items()):
            raise ValueError("MCP profile name does not match its key")

    @classmethod
    def load(cls, path: Path) -> MCPConfig:
        if not path.exists():
            return cls()
        try:
            data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
            if not isinstance(data, dict) or set(data) - {"current", "profiles"}:
                raise ValueError("Expected current and profiles in MCP configuration")
            profiles = data.get("profiles", {})
            if not isinstance(profiles, dict):
                raise ValueError("MCP profiles must be a mapping")
            parsed = {}
            for name, item in profiles.items():
                if not isinstance(name, str) or not isinstance(item, dict) or set(item) - {"url", "transport"}:
                    raise ValueError("MCP profiles accept only URL and transport")
                if not isinstance(item.get("url"), str):
                    raise ValueError("MCP profile needs a URL")
                parsed[name] = MCPProfile(name, item["url"], item.get("transport", "streamable_http"))
            return cls(data.get("current", "none"), parsed)
        except (yaml.YAMLError, TypeError) as exc:
            raise ValueError("Invalid MCP configuration YAML") from exc

    def save(self, path: Path) -> None:
        data = {"current": self.current, "profiles": {
            name: {"url": profile.url, "transport": profile.transport}
            for name, profile in self.profiles.items()
        }}
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".mcp-", suffix=".yaml", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                yaml.safe_dump(data, target, sort_keys=False)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
