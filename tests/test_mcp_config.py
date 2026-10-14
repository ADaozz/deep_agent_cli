import os

import pytest

from agent.mcp.config import MCPConfig, MCPProfile, config_directory, redacted_url


def test_missing_roundtrip_and_none(tmp_path):
    path = tmp_path / "config" / "mcp.yaml"
    assert MCPConfig.load(path) == MCPConfig()
    profile = MCPProfile("github", "https://example.com/mcp")
    config = MCPConfig("github", {"github": profile})
    config.save(path)
    assert MCPConfig.load(path) == config
    assert path.stat().st_mode & 0o777 == 0o600
    MCPConfig("none", config.profiles).save(path)
    assert MCPConfig.load(path).profiles == config.profiles


@pytest.mark.parametrize("url", ["server.py", "file:///mcp", "ftp://example.com", "http://", "https://user:pass@host/mcp",
                                       "https://host/mcp?token=secret", "https://host/mcp#secret", "https://host:bad/mcp"])
def test_invalid_urls(url):
    with pytest.raises(ValueError):
        MCPProfile("remote", url)


@pytest.mark.parametrize("name", ["none", "NONE", "add", "", "with space", "../name", "1name"])
def test_invalid_names(name):
    with pytest.raises(ValueError):
        MCPProfile(name, "https://host/mcp")


def test_duplicate_and_unsupported_configuration(tmp_path):
    path = tmp_path / "mcp.yaml"
    for data in ["profiles:\n  x: {url: https://host/mcp}\n  x: {url: https://host/other}\n",
                 "profiles: {x: {url: https://host, command: npx}}",
                 "profiles: {x: {url: https://host, transport: stdio}}",
                 "current: unknown\nprofiles: {}", "[]"]:
        path.write_text(data)
        with pytest.raises(ValueError):
            MCPConfig.load(path)


def test_atomic_save_failure_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / "mcp.yaml"
    MCPConfig().save(path)
    before = path.read_bytes()
    def fail(*args):
        raise OSError("disk failure")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        MCPConfig("x", {"x": MCPProfile("x", "https://host/mcp")}).save(path)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_directory_and_redaction(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEP_AGENT_CONFIG_DIR", str(tmp_path))
    assert config_directory() == tmp_path
    assert redacted_url("https://u:p@host/mcp?token=secret#fragment") == "https://host/mcp"
