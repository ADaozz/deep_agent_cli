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


@pytest.mark.parametrize("url", [
    "https://user:credential-xyz@example.com/mcp",
    "https://credential-xyz@example.com/mcp",
    "https://example.com/mcp?%74oken=credential-xyz",
])
def test_url_credentials_are_rejected_without_echoing_value(url):
    from agent.mcp.config import server_from_fields
    with pytest.raises(MCPConfigError) as caught:
        server_from_fields(name="private", transport="streamable_http", url=url)
    assert "credential-xyz" not in str(caught.value)


@pytest.mark.parametrize("args", [
    ["--token=credential-xyz"], ["--api-key", "credential-xyz"],
    ["--password", "credential-xyz"], ["--client-secret=credential-xyz"],
])
def test_explicit_stdio_secret_flags_are_rejected(args):
    from agent.mcp.config import server_from_fields
    with pytest.raises(MCPConfigError) as caught:
        server_from_fields(name="private", transport="stdio", command="python", args=args)
    assert "credential-xyz" not in str(caught.value)


def test_normal_stdio_args_and_env_names_remain_valid():
    from agent.mcp.config import server_from_fields, redact_config_text
    args = ["server.py", "/path/token/cache", "/path/--token=ordinary-file",
            "--tokenizer", "normal", "--api-key-file", "/keys/local"]
    config = server_from_fields(name="local", transport="stdio", command="python", args=args,
                                env_passthrough=["MCP_TOKEN"])
    assert config.args == tuple(args)
    assert redact_config_text(" ".join(args)) == " ".join(args)


@pytest.mark.parametrize("target", [
    "python server.py --token=credential-xyz",
    "python server.py --password 'credential-xyz with spaces'",
    "https://user:credential-xyz@word@example.com/mcp",
    "https://example.com/mcp?token=credential-xyz",
    "https://example.com/mcp?%74oken=credential-xyz",
])
def test_public_config_rendering_is_shared_and_hides_credentials(target):
    from agent.mcp.config import redact_config_text
    from agent.cli.mcp_ui import _summary
    from agent.cli.interactions import InteractionController
    transport = "streamable_http" if target.startswith("http") else "stdio"
    assert "credential-xyz" not in redact_config_text(target)
    assert "credential-xyz" not in _summary({"transport": transport, "target": target})
    card = InteractionController.approval([{"name": "mcp_add", "args": {
        "transport": transport, "command": target, "url": target,
    }}])
    assert "credential-xyz" not in card.question


@pytest.mark.parametrize("url", ["https://[broken/mcp", "https://host:invalid/mcp"])
def test_invalid_authority_is_a_config_error_without_leaking_input(url):
    from agent.mcp.config import server_from_fields
    with pytest.raises(MCPConfigError, match="invalid URL authority"):
        server_from_fields(name="local", transport="streamable_http", url=url)



def test_invalid_utf8_catalog_is_reported_and_never_overwritten(tmp_path):
    from agent.mcp.manager import MCPManager

    path = tmp_path / "mcp.json"
    content = b'{"servers": {}}\xff'
    path.write_bytes(content)
    manager = MCPManager(path)
    try:
        manager.load()
        assert "UTF-8" in manager.config_error
        result = manager.add_from_fields(name="local", transport="stdio", command="python")
        assert not result.ok
        assert path.read_bytes() == content
    finally:
        manager.close()


@pytest.mark.parametrize("remote", [
    "read_file", "read.file", "read file", "读取文件", "x" * 200,
])
def test_remote_alias_is_stable_and_fits_maximum_server_name(remote):
    import re
    from agent.mcp.names import external_tool_name

    server = "s" * 33
    config = server_from_fields(name=server, transport="stdio", command="fixture")
    first = external_tool_name(config.name, remote)
    assert first == external_tool_name(config.name, remote)
    assert first.startswith(f"mcp__{server}__")
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,80}", first)
    if remote == "read_file":
        assert first == f"mcp__{server}__read_file"


def test_remote_aliases_are_unique_for_normalization_and_truncation():
    from agent.mcp.names import external_tool_name

    names = ["read.file", "read_file", "read file", "读取文件", "read-file",
             "x" * 100 + "a", "x" * 100 + "b"]
    aliases = [external_tool_name("local", name) for name in names]
    assert len(set(aliases)) == len(names)
    assert external_tool_name("other", "read.file") not in aliases


@pytest.mark.parametrize("name", ["", "\ud800"])
def test_unsafe_remote_names_have_explicit_mapping_errors(name):
    from agent.mcp.names import external_tool_name
    with pytest.raises(ValueError, match="remote tool name"):
        external_tool_name("local", name)
