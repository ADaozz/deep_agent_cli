"""Bearer tokens stay in the environment. OAuth cancel does not hang."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from agent.mcp.manager import MCPManager, ServerStatus
from tests.mcp_fixture import HttpFixture, text_of


def test_missing_and_wrong_bearer_tokens_are_not_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_FIXTURE_TOKEN", raising=False)
    protected = HttpFixture(auth=StaticTokenVerifier(tokens={
        "good-token": {"client_id": "fixture", "scopes": ["mcp"]},
    }))
    path = tmp_path / "mcp.json"
    manager = MCPManager(path, call_timeout=8, connect_timeout=5)
    try:
        missing = manager.add_from_fields(
            name="secure", transport="streamable_http", url=protected.url,
            auth_type="bearer", token_env="MCP_FIXTURE_TOKEN",
        )
        assert not missing.ok
        assert "MCP_FIXTURE_TOKEN" in missing.message
        assert "not set" in missing.message.lower()
        saved = path.read_text(encoding="utf-8")
        assert "MCP_FIXTURE_TOKEN" in saved
        assert "good-token" not in saved
        state = manager.get("secure")
        assert state is not None and state.status is ServerStatus.AUTH_REQUIRED

        monkeypatch.setenv("MCP_FIXTURE_TOKEN", "wrong-secret-xyz")
        manager.remove("secure")
        rejected = manager.add_from_fields(
            name="secure", transport="streamable_http", url=protected.url,
            auth_type="bearer", token_env="MCP_FIXTURE_TOKEN",
        )
        assert not rejected.ok
        assert "wrong-secret-xyz" not in rejected.message
        assert "wrong-secret-xyz" not in path.read_text(encoding="utf-8")
        assert "good-token" not in path.read_text(encoding="utf-8")
    finally:
        manager.close()
        protected.close()


def test_valid_bearer_calls_auth_check_without_echoing_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_FIXTURE_TOKEN", "good-token")
    protected = HttpFixture(auth=StaticTokenVerifier(tokens={
        "good-token": {"client_id": "fixture", "scopes": ["mcp"]},
    }))
    path = tmp_path / "mcp.json"
    manager = MCPManager(path, call_timeout=8, connect_timeout=5)
    try:
        added = manager.add_from_fields(
            name="secure", transport="streamable_http", url=protected.url,
            auth_type="bearer", token_env="MCP_FIXTURE_TOKEN",
        )
        assert added.ok, added.message
        assert "good-token" not in path.read_text(encoding="utf-8")
        state = manager.get("secure")
        assert state is not None
        check = next(tool for tool in state.tools if tool.name == "mcp__secure__auth_check")
        result = text_of(check.invoke({}))
        assert "subject:fixture" in result
        assert "good-token" not in result
    finally:
        manager.close()
        protected.close()


def test_expired_bearer_is_rejected_without_the_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_FIXTURE_TOKEN", "expired-secret-xyz")
    protected = HttpFixture(auth=StaticTokenVerifier(tokens={
        "expired-secret-xyz": {"client_id": "fixture", "scopes": ["mcp"], "expires_at": 1},
    }))
    path = tmp_path / "mcp.json"
    manager = MCPManager(path, call_timeout=8, connect_timeout=5)
    try:
        result = manager.add_from_fields(
            name="secure", transport="streamable_http", url=protected.url,
            auth_type="bearer", token_env="MCP_FIXTURE_TOKEN",
        )
        assert not result.ok
        assert "expired-secret-xyz" not in result.message
        assert "expired-secret-xyz" not in path.read_text(encoding="utf-8")
        state = manager.get("secure")
        assert state is not None and state.status is ServerStatus.AUTH_REQUIRED
    finally:
        manager.close()
        protected.close()


def test_oauth_cancel_or_timeout_reports_without_hanging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("webbrowser.open", lambda *_args, **_kwargs: False)
    protected = HttpFixture(auth=StaticTokenVerifier(tokens={
        "good-token": {"client_id": "fixture", "scopes": ["mcp"]},
    }))
    manager = MCPManager(
        tmp_path / "mcp.json", call_timeout=5, connect_timeout=3, oauth_callback_timeout=1,
    )
    try:
        started = time.monotonic()
        result = manager.add_from_fields(
            name="oauth", transport="streamable_http", url=protected.url, auth_type="oauth",
        )
        elapsed = time.monotonic() - started
        assert elapsed < 20
        assert not result.ok
        state = manager.get("oauth")
        assert state is not None and state.status is ServerStatus.AUTH_REQUIRED
        folded = result.message.lower()
        assert "good-token" not in result.message
        assert any(token in folded for token in ("oauth", "timed out", "cancel", "auth"))
    finally:
        manager.close()
        protected.close()
