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


def test_oauth_success_authorizes_discovers_and_calls_real_server(tmp_path, monkeypatch):
    import threading
    import urllib.request
    from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
    from fastmcp.server.auth.auth import ClientRegistrationOptions
    monkeypatch.setattr(
        "fastmcp.server.auth.providers.in_memory.DEFAULT_ACCESS_TOKEN_EXPIRY_SECONDS", 1,
    )

    browsers, errors = [], []

    def browser(url, *args, **kwargs):
        def follow():
            deadline = time.monotonic() + 5
            while True:
                try:
                    with urllib.request.urlopen(url, timeout=3) as response:
                        response.read()
                    return
                except Exception as exc:
                    if time.monotonic() >= deadline:
                        errors.append(exc)
                        return
                    time.sleep(.05)
        thread = threading.Thread(target=follow, daemon=True)
        browsers.append(thread)
        thread.start()
        return True

    monkeypatch.setattr("webbrowser.open", browser)
    protected = HttpFixture(auth_factory=lambda base: InMemoryOAuthProvider(
        base_url=base,
        client_registration_options=ClientRegistrationOptions(enabled=True),
    ))
    manager = MCPManager(tmp_path / "mcp.json", connect_timeout=5, oauth_callback_timeout=10)
    try:
        result = manager.add_from_fields(
            name="oauth", transport="streamable_http", url=protected.url, auth_type="oauth",
        )
        assert result.ok, (result.message, errors)
        state = manager.get("oauth")
        assert state.status is ServerStatus.CONNECTED
        check = next(tool for tool in state.tools if tool.name == "mcp__oauth__auth_check")
        assert "subject:" in text_of(check.invoke({}))
        assert browsers
        issued = set(protected.mcp.auth.access_tokens)
        browser_count = len(browsers)
        time.sleep(1.1)
        assert "subject:" in text_of(check.invoke({}))
        assert set(protected.mcp.auth.access_tokens) - issued
        assert len(browsers) == browser_count  # Official OAuth refresh, no new consent.
        for thread in browsers:
            thread.join(3)
        assert not errors
        for token in protected.mcp.auth.access_tokens:
            assert token not in manager.describe()
            assert token not in manager.path.read_text()
    finally:
        manager.close()
        protected.close()


def test_bearer_echo_and_stdio_environment_outputs_are_redacted(tmp_path, monkeypatch, http_mcp, caplog):
    from tests.mcp_fixture import stdio_argv

    token = "credential-xyz-echo-test"
    monkeypatch.setenv("MCP_FIXTURE_TOKEN", token)
    executable, script = stdio_argv()
    manager = MCPManager(tmp_path / "mcp.json", connect_timeout=8)
    try:
        assert manager.add_from_fields(
            name="bearer", transport="streamable_http", url=http_mcp.url,
            auth_type="bearer", token_env="MCP_FIXTURE_TOKEN",
        ).ok
        assert manager.add_from_fields(
            name="stdio", transport="stdio", command=executable, args=[script],
            env_passthrough=["MCP_FIXTURE_TOKEN"],
        ).ok
        for name in ("bearer", "stdio"):
            echo = next(tool for tool in manager.get(name).tools if tool.name.endswith("__echo"))
            result = echo.invoke({"text": token})
            assert token not in str(result)
            assert "[redacted]" in str(result)
        assert token not in manager.describe()
        from agent.mcp.middleware import MCPMiddleware
        # mcp_list uses the same public rendering as the CLI.
        middleware = MCPMiddleware(manager, [])
        listing = next(tool for tool in middleware.tools if tool.name == "mcp_list")
        assert token not in str(listing.invoke({}))
        assert token not in caplog.text
    finally:
        manager.close()
