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
    import json
    import threading
    import urllib.request
    from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
    from fastmcp.server.auth.auth import ClientRegistrationOptions
    monkeypatch.setattr(
        "fastmcp.server.auth.providers.in_memory.DEFAULT_ACCESS_TOKEN_EXPIRY_SECONDS", 3,
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
        manager.path.write_text(json.dumps({'servers': {'oauth': {
            'transport': 'streamable_http', 'url': protected.url, 'auth': {'type': 'oauth'},
        }}}))
        manager.load()
        result = manager.connect_pending()  # First startup authorizes without /mcp test.
        assert result.ok, (result.message, errors)
        state = manager.get("oauth")
        assert state.status is ServerStatus.CONNECTED
        check = next(tool for tool in state.tools if tool.name == "mcp__oauth__auth_check")
        assert "subject:" in text_of(check.invoke({}))
        assert browsers
        issued = set(protected.mcp.auth.access_tokens)
        browser_count = len(browsers)
        time.sleep(3.1)
        assert "subject:" in text_of(check.invoke({}))
        assert set(protected.mcp.auth.access_tokens) - issued
        assert len(browsers) == browser_count  # Official OAuth refresh, no new consent.
        manager.close()
        manager = MCPManager(tmp_path / "mcp.json", connect_timeout=5, oauth_callback_timeout=10)
        manager.load()
        time.sleep(3.1)  # Refresh an expired token using the encrypted cache after restart.
        reconnected = manager.connect_pending()
        assert reconnected.ok, reconnected.message
        check = next(tool for tool in manager.get("oauth").tools if tool.name == "mcp__oauth__auth_check")
        assert "subject:" in text_of(check.invoke({}))
        assert len(browsers) == browser_count
        # Revoke both credentials: refresh fails and startup must authorize again.
        manager.close()
        protected.mcp.auth.access_tokens.clear()
        protected.mcp.auth.refresh_tokens.clear()
        manager = MCPManager(tmp_path / "mcp.json", connect_timeout=5, oauth_callback_timeout=10)
        manager.load()
        time.sleep(3.1)
        assert manager.connect_pending().ok
        assert len(browsers) == browser_count + 1
        # The already connected client must also be able to reauthorize later.
        manager._oauth_browser_attempts['oauth'] -= 61
        protected.mcp.auth.access_tokens.clear()
        protected.mcp.auth.refresh_tokens.clear()
        time.sleep(3.1)
        check = next(tool for tool in manager.get("oauth").tools if tool.name == "mcp__oauth__auth_check")
        assert "subject:" in text_of(check.invoke({}))
        assert len(browsers) == browser_count + 2
        for thread in browsers:
            thread.join(3)
        assert not errors
        for token in protected.mcp.auth.access_tokens:
            assert token not in manager.describe()
            assert token not in manager.path.read_text()
    finally:
        manager.close()
        protected.close()


def test_exit_cancels_pending_oauth_before_browser_opens(tmp_path, monkeypatch):
    import asyncio
    import json
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from fastmcp.client.auth import OAuth
    from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
    from fastmcp.server.auth.auth import ClientRegistrationOptions

    started, cancelled = threading.Event(), threading.Event()
    opened = []

    async def delayed_redirect(self, url):
        started.set()
        try:
            await asyncio.sleep(60)
            opened.append(url)
        finally:
            cancelled.set()

    monkeypatch.setattr(OAuth, 'redirect_handler', delayed_redirect)
    protected = HttpFixture(auth_factory=lambda base: InMemoryOAuthProvider(
        base_url=base, client_registration_options=ClientRegistrationOptions(enabled=True),
    ))
    manager = MCPManager(tmp_path / 'mcp.json', oauth_callback_timeout=120)
    manager.path.write_text(json.dumps({'servers': {'oauth': {
        'transport': 'streamable_http', 'url': protected.url, 'auth': {'type': 'oauth'},
    }}}))
    manager.load()
    try:
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(manager.connect_pending)
            assert started.wait(5)
            manager.cancel_pending_connections()
            assert not pending.result(3).ok
            assert cancelled.wait(3)
            assert not manager.test('oauth').ok  # Exit cannot start another connection.
        assert opened == []
    finally:
        manager.close()
        protected.close()


def test_bearer_echo_and_stdio_environment_outputs_are_redacted(tmp_path, monkeypatch, http_mcp, caplog):
    import json
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
            unknown = '{  "api_key": "unknown", "Authorization": "Bearer unknown", "payload": "' + "A" * 600 + '" }\n'
            for text in (unknown, json.dumps({"token": token, "unknown": unknown})):
                args = {"text": text}
                result = echo.invoke(args)
                returned = next(block["text"] for block in result if block["type"] == "text")
                assert returned == text.replace(token, "[redacted]")
                assert json.loads(returned) == json.loads(text.replace(token, "[redacted]"))
                assert args == {"text": text}
        assert token not in manager.describe()
        from agent.mcp.middleware import MCPMiddleware
        # mcp_list uses the same public rendering as the CLI.
        middleware = MCPMiddleware(manager, [])
        listing = next(tool for tool in middleware.tools if tool.name == "mcp_list")
        assert token not in str(listing.invoke({}))
        assert token not in caplog.text
    finally:
        manager.close()


def test_oauth_cache_is_encrypted_and_survives_reopening(tmp_path):
    import asyncio
    import stat
    from fastmcp.client.auth.oauth import TokenStorageAdapter
    from mcp.shared.auth import OAuthToken
    from agent.mcp.oauth_storage import oauth_token_storage

    directory = tmp_path / 'oauth'
    token = 'private-persistent-access-token'

    async def check():
        first = TokenStorageAdapter(oauth_token_storage(directory), 'https://first.example/mcp')
        await first.set_tokens(OAuthToken(access_token=token, token_type='Bearer', expires_in=60))
        reopened = TokenStorageAdapter(oauth_token_storage(directory), 'https://first.example/mcp')
        assert (await reopened.get_tokens()).access_token == token
        other = TokenStorageAdapter(oauth_token_storage(directory), 'https://other.example/mcp')
        assert await other.get_tokens() is None

    asyncio.run(check())
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / 'key').stat().st_mode) == 0o600
    for path in directory.rglob('*'):
        if path.is_file():
            assert token.encode() not in path.read_bytes()


def test_persistent_oauth_transport_does_not_emit_memory_storage_warning(tmp_path):
    import warnings
    from agent.mcp.adapter import build_transport
    from agent.mcp.config import parse_server

    server = parse_server('oauth', {
        'transport': 'streamable_http', 'url': 'https://example.invalid/mcp',
        'auth': {'type': 'oauth'},
    })
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter('always')
        build_transport(server, [], oauth_callback_timeout=1, oauth_storage_dir=tmp_path / 'oauth')
    assert not any('in-memory token storage' in str(item.message) for item in recorded)


def test_explicitly_disabled_oauth_browser_uses_cached_credentials_only(tmp_path, monkeypatch):
    import json

    opened = []
    monkeypatch.setattr('webbrowser.open', lambda url: opened.append(url))
    path = tmp_path / 'mcp.json'
    path.write_text(json.dumps({'servers': {'oauth': {
        'transport': 'streamable_http', 'url': 'https://example.invalid/mcp',
        'auth': {'type': 'oauth'},
    }}}))
    manager = MCPManager(path, connect_timeout=3)
    try:
        manager.load()
        result = manager.test('oauth', allow_oauth_browser=False)
        assert not result.ok
        assert manager.get('oauth').status is ServerStatus.AUTH_REQUIRED
        assert '/mcp test oauth' in result.message
        for _ in range(2):
            response = manager.add_from_fields(name='oauth', transport='streamable_http',
                url='https://example.invalid/mcp', auth_type='oauth', allow_oauth_browser=False)
            assert '/mcp test oauth' in response.message
        assert opened == []
    finally:
        manager.close()


def test_oauth_browser_guard_is_shared_and_allows_later_reauthorization(monkeypatch):
    import asyncio
    from fastmcp.client.auth import OAuth
    from agent.mcp.oauth import ManagedOAuth
    from agent.mcp.config import MCPAuthError
    from key_value.aio.stores.memory import MemoryStore

    calls = []
    clock = [100.0]
    monkeypatch.setattr('agent.mcp.oauth.time.monotonic', lambda: clock[0])
    attempts = {}

    async def redirect(self, url):
        calls.append(url)

    monkeypatch.setattr(OAuth, 'redirect_handler', redirect)
    auth = ManagedOAuth(server_name='demo', allow_browser=False, token_storage=MemoryStore(), browser_attempts=attempts)
    with pytest.raises(MCPAuthError, match='/mcp test demo'):
        asyncio.run(auth.redirect_handler('https://example.invalid/authorize'))
    assert calls == []
    auth.allow_browser = True
    asyncio.run(auth.redirect_handler('https://example.invalid/authorize'))
    with pytest.raises(MCPAuthError):
        asyncio.run(auth.redirect_handler('https://example.invalid/authorize'))
    assert len(calls) == 1
    replacement = ManagedOAuth(server_name='demo', allow_browser=True, token_storage=MemoryStore(), browser_attempts=attempts)
    with pytest.raises(MCPAuthError):
        asyncio.run(replacement.redirect_handler('https://example.invalid/authorize'))
    clock[0] += 61
    asyncio.run(replacement.redirect_handler('https://example.invalid/authorize'))
    assert len(calls) == 2
