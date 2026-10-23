"""Global networking settings: parsing, real proxy routing, and process envs."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from agent.config import ModelProfile, Settings
from agent.http_environment import HTTP_ENV_VARS
from agent.llm import build_chat_model
from agent.mcp.adapter import build_transport
from agent.mcp.config import parse_server
from agent.sandbox import sandbox_environment
from agent.tools.web_search import build_web_search_tool


@pytest.mark.parametrize("value", [True, False])
def test_global_setting_and_process_environments(monkeypatch, tmp_path, value):
    for name in HTTP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://host-proxy:8080")
    monkeypatch.setenv("SSL_CERT_FILE", "/host/ca.pem")
    settings = Settings.from_mapping({"trust_env": value}, workspace=tmp_path)
    assert settings.trust_env is value
    config = replace(settings.sandbox, env_set={"HTTP_PROXY": "http://explicit:8080"})
    env = sandbox_environment(config)
    transport = build_transport(parse_server("local", {
        "transport": "stdio", "command": "node", "args": ["server.js"],
        "env_passthrough": ["HTTPS_PROXY"],
    }), [], oauth_callback_timeout=1, trust_env=value)
    for child_env in (env, transport.env or {}):
        assert (child_env.get("HTTPS_PROXY") == "http://host-proxy:8080") is value
        assert (child_env.get("SSL_CERT_FILE") == "/host/ca.pem") is value
        assert (child_env.get("NODE_USE_ENV_PROXY") == "1") is value
    assert (env.get("HTTP_PROXY") == "http://explicit:8080") is value


@pytest.mark.parametrize("value", ["true", 1, None, [], {}])
def test_global_setting_rejects_non_boolean(value):
    with pytest.raises(ValueError, match="trust_env must be a boolean"):
        Settings.from_mapping({"trust_env": value})


@pytest.mark.parametrize("enabled", [False, True])
def test_model_and_mcp_clients_route_via_environment_proxy(monkeypatch, enabled):
    for name in HTTP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("HTTP_PROXY", endpoint)
    target = "http://proxy-only.invalid/check" if enabled else endpoint + "/check"
    model = build_chat_model(ModelProfile("test", "test"), trust_env=enabled)
    transport = build_transport(parse_server("remote", {
        "transport": "streamable_http", "url": "http://example.invalid/mcp",
        "auth": {"type": "oauth"},
    }), [], oauth_callback_timeout=1, trust_env=enabled)

    async def check_async():
        assert (await model.http_async_client.get(target)).text == "ok"
        async with transport.httpx_client_factory() as client:
            assert (await client.get(target)).text == "ok"
        # OAuth must use the same client policy as the MCP transport.
        async with transport.auth.httpx_client_factory() as client:
            assert (await client.get(target)).text == "ok"
        await model.http_async_client.aclose()

    try:
        assert model.http_client.get(target).text == "ok"
        asyncio.run(check_async())
        assert requests == [target if enabled else "/check"] * 4
    finally:
        model.http_client.close()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("enabled", [False, True])
def test_tavily_uses_global_policy(monkeypatch, enabled):
    import httpx
    original = httpx.Client
    policies = []

    def client(**kwargs):
        policies.append(kwargs["trust_env"])
        return original(transport=httpx.MockTransport(lambda req: httpx.Response(
            200, json={"results": []}, request=req,
        )), **kwargs)

    monkeypatch.setattr("agent.tools.web_search.httpx.Client", client)
    build_web_search_tool("test-key", trust_env=enabled).invoke({"query": "test"})
    assert policies == [enabled]


@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_socks_environment_allows_model_and_mcp_startup(monkeypatch, scheme):
    for name in HTTP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALL_PROXY", f"{scheme}://127.0.0.1:1080")
    model = build_chat_model(ModelProfile("test", "test"), trust_env=True)
    transport = build_transport(parse_server("remote", {
        "transport": "streamable_http", "url": "http://example.invalid/mcp",
    }), [], oauth_callback_timeout=1, trust_env=True)

    async def close_clients():
        async with transport.httpx_client_factory():
            pass
        await model.http_async_client.aclose()

    try:
        asyncio.run(close_clients())
    finally:
        model.http_client.close()


@pytest.mark.parametrize("enabled", [False, True])
def test_runner_preserves_policy_when_switching_models(monkeypatch, enabled):
    from deepagents.backends import StateBackend
    from langchain_core.messages import AIMessage
    from agent.runner import AgentRunner
    from tests.conftest import scripted_model

    settings = Settings(trust_env=enabled, llm_profiles=(
        ModelProfile("first", "first"), ModelProfile("second", "second"),
    ), llm_default="first")
    policies = []

    def build(profile, **kwargs):
        policies.append(kwargs["trust_env"])
        return scripted_model([AIMessage(content="ok")])

    monkeypatch.setattr("agent.runner.build_chat_model", build)
    runner = AgentRunner(settings=settings, backend=StateBackend())
    try:
        assert runner._spec.trust_env is enabled
        runner.switch_model("second")
        assert runner.invoke("hello").status == "completed"
        assert policies == [enabled, enabled]
    finally:
        runner.close()
