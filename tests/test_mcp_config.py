"""MCP catalog parsing and atomic writes."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent.mcp.config import (
    MCPConfigError,
    atomic_write_text,
    load_mcp_file,
    server_from_fields,
)
from agent.mcp.manager import MCPManager


def test_server_fields_reject_plaintext_token_and_bad_url() -> None:
    with pytest.raises(MCPConfigError, match="token_env"):
        server_from_fields(
            name="internal", transport="streamable_http", url="https://example.com/mcp",
            auth_type="bearer", token_env="",
        )
    with pytest.raises(MCPConfigError):
        server_from_fields(
            name="internal", transport="streamable_http",
            url="https://example.com/mcp?token=super-secret", auth_type="none",
        )
    server = server_from_fields(
        name="internal", transport="streamable_http", url="https://example.com/mcp",
        auth_type="bearer", token_env="INTERNAL_MCP_TOKEN",
    )
    stored = json.dumps(server.to_json())
    assert "INTERNAL_MCP_TOKEN" in stored
    assert "super-secret" not in stored
    assert "Bearer" not in stored


def test_stdio_command_is_split_and_http_auth_is_rejected() -> None:
    server = server_from_fields(name="local", transport="stdio", command="python server.py --flag")
    assert server.command == "python"
    assert server.args == ("server.py", "--flag")
    with pytest.raises(MCPConfigError, match="stdio"):
        server_from_fields(
            name="local", transport="stdio", command="python server.py", auth_type="bearer", token_env="TOKEN",
        )


def test_atomic_write_replaces_and_sets_mode(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    atomic_write_text(path, '{"servers": {}}\n')
    assert path.read_text(encoding="utf-8") == '{"servers": {}}\n'
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".mcp-*.tmp")) == []


def test_corrupt_file_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text("{", encoding="utf-8")
    before = path.read_bytes()
    manager = MCPManager(path)
    try:
        manager.load()
        assert manager.config_error
        result = manager.add_from_fields(
            name="local", transport="streamable_http", url="https://example.com/mcp",
        )
        assert not result.ok
        assert "not modified" in result.message
        assert path.read_bytes() == before
    finally:
        manager.close()


def test_invalid_entry_is_preserved_and_secret_field_is_not_connected(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    raw = {
        "servers": {
            "bad": {
                "transport": "streamable_http",
                "url": "https://example.com/mcp",
                "auth": {"type": "bearer", "token": "super-secret-token"},
            },
        },
    }
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = path.read_bytes()
    document = load_mcp_file(path)
    assert "bad" in document.invalid
    assert "super-secret-token" not in document.invalid["bad"][1]
    manager = MCPManager(path, connect_timeout=2)
    try:
        manager.load()
        assert manager.get("bad") is None
        assert "super-secret-token" not in manager.describe()
        added = manager.add_from_fields(
            name="other", transport="streamable_http", url="https://example.com/mcp", enabled=False,
        )
        assert added.ok
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["servers"]["bad"]["auth"]["token"] == "super-secret-token"
        assert saved["servers"]["other"]["enabled"] is False
        assert before != path.read_bytes()
    finally:
        manager.close()


def test_remove_missing_server_does_not_touch_the_file(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text('{"servers": {}}\n', encoding="utf-8")
    os.chmod(path, 0o600)
    before = path.read_bytes()
    stamp = path.stat().st_mtime_ns
    manager = MCPManager(path)
    try:
        manager.load()
        result = manager.remove("missing")
        assert not result.changed
        assert "not modified" in result.message
        assert path.read_bytes() == before
        assert path.stat().st_mtime_ns == stamp
        again = manager.remove("missing")
        assert not again.changed
        assert path.read_bytes() == before
    finally:
        manager.close()


def test_duplicate_name_with_a_different_config_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    manager = MCPManager(path)
    try:
        first = manager.add_from_fields(
            name="wiki", transport="streamable_http", url="https://example.com/mcp", enabled=False,
        )
        assert first.ok
        saved = path.read_bytes()
        second = manager.add_from_fields(
            name="wiki", transport="streamable_http", url="https://example.com/other", enabled=False,
        )
        assert not second.ok
        assert not second.changed
        assert path.read_bytes() == saved
    finally:
        manager.close()
