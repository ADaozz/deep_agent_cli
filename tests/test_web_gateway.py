"""Local origin protection, explicit routes, token injection and SSE frames."""
import httpx
import pytest
from fastapi.testclient import TestClient
from agent.web_gateway.app import create_app
from agent.web_gateway.config import GatewayConfig

TOKEN = 'gateway-test-only-token'
ORIGIN = 'http://127.0.0.1:8765'


@pytest.mark.parametrize('upstream', ['http://example.com', 'http://127.0.0.1/a', 'http://user:pass@localhost', 'file:///tmp/x', 'http://127.0.0.1?url=evil'])
def test_gateway_only_accepts_loopback_origins(upstream):
    with pytest.raises(ValueError):
        GatewayConfig(token=TOKEN, upstream=upstream)


@pytest.mark.parametrize('status', [200, 201, 202, 401, 404, 409, 422, 429, 503])
def test_gateway_injects_private_token_and_preserves_status(status):
    seen = []
    def upstream(request):
        seen.append(request)
        return httpx.Response(status, json={'detail': 'upstream response'}, headers={'Set-Cookie': 'must-not-forward', 'Authorization': 'must-not-forward'})
    with TestClient(create_app(GatewayConfig(token=TOKEN), transport=httpx.MockTransport(upstream)), base_url=ORIGIN) as client:
        response = client.get('/api/agent/projects', headers={'Authorization': 'Bearer browser-invalid'})
        assert response.status_code == status
        assert seen[0].headers['Authorization'] == 'Bearer '+TOKEN
        assert seen[0].url.path == '/v1/projects'
        assert 'set-cookie' not in response.headers and 'authorization' not in response.headers
        assert TOKEN not in response.text


def test_gateway_rejects_cross_site_and_unmapped_requests():
    seen=[]
    with TestClient(create_app(GatewayConfig(token=TOKEN), transport=httpx.MockTransport(lambda r: seen.append(r))), base_url=ORIGIN) as client:
        assert client.get('/api/agent/projects', headers={'Host': 'evil.local:8765'}).status_code == 403
        assert client.get('/api/agent/projects', headers={'Origin': 'https://evil.test'}).status_code == 403
        assert client.post('/api/agent/sessions', json={'project_id':'p'}).status_code == 403
        assert client.post('/api/agent/sessions', headers={'Origin': ORIGIN}, content='x').status_code == 415
        assert client.post('/api/agent/sessions', headers={'Origin': ORIGIN,'Content-Type':'application/json'}, content='x').status_code == 422
        assert client.get('/api/agent/openapi.json').status_code == 404
        assert client.get('/api/agent/runs/r/execute').status_code == 404
        assert client.get('/api/agent/projects?upstream=http://evil').status_code == 422
        assert not seen


def test_gateway_body_limit_and_static_traversal(tmp_path):
    (tmp_path/'index.html').write_text('<html>workspace</html>')
    with TestClient(create_app(GatewayConfig(token=TOKEN, max_body_bytes=10, dist_dir=tmp_path)), base_url=ORIGIN) as client:
        assert client.post('/api/agent/sessions', headers={'Origin':ORIGIN}, json={'large':'x'*20}).status_code==413
        assert client.get('/').status_code==200
        assert client.get('/%2e%2e/secret').status_code==404


def test_gateway_preserves_sse_frames_and_header_cursor():
    content=b'id: 101\nevent: delta\ndata: {"kind":"assistant","text":"hello"}\n\n: keepalive\n\n'
    seen=[]
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield content[:15]
            yield content[15:]
    def upstream(request):
        seen.append(request)
        return httpx.Response(200, headers={'Content-Type':'text/event-stream'}, stream=Chunks())
    with TestClient(create_app(GatewayConfig(token=TOKEN), transport=httpx.MockTransport(upstream)), base_url=ORIGIN) as client:
        response=client.get('/api/agent/runs/r/events?after=1',headers={'Last-Event-ID':'101'})
        assert response.content==content
        assert seen[0].headers['Last-Event-ID']=='101'
        assert seen[0].url.params['after']=='1'
        assert response.headers['X-Accel-Buffering']=='no'
        assert response.headers['Cache-Control']=='no-cache, no-transform'
        assert client.get('/api/agent/runs/r/events',headers={'Last-Event-ID':'bad'}).status_code==422


def test_gateway_redacts_nonstream_error_and_handles_transport_failure():
    def upstream(request):
        if request.url.path=='/health': raise httpx.ConnectError('secret debug info',request=request)
        return httpx.Response(503,json={'detail':TOKEN})
    with TestClient(create_app(GatewayConfig(token=TOKEN),transport=httpx.MockTransport(upstream)),base_url=ORIGIN) as client:
        assert client.get('/api/agent/projects').json()=={'detail':'[REDACTED]'}
        assert client.get('/api/agent/health').json()=={'detail':'Agent service is unavailable'}


def test_gateway_streams_before_completion_and_closes_upstream_on_disconnect():
    import asyncio
    import socket
    import time
    from threading import Thread, Event
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse
    import uvicorn

    closed=Event()
    release=Event()
    upstream=FastAPI()
    @upstream.get('/v1/runs/r/events')
    async def events():
        async def chunks():
            try:
                yield b'id: 101\nevent: delta\ndata: {"kind":"assistant","text":"hello"}\n\n'
                yield b': keepalive\n\n'
                while not release.is_set(): await asyncio.sleep(0.01)
            finally:
                closed.set()
        return StreamingResponse(chunks(),media_type='text/event-stream')

    def start(app):
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen()
        port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
        thread=Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        deadline=time.monotonic()+5
        while not server.started and thread.is_alive() and time.monotonic()<deadline: time.sleep(.01)
        assert server.started
        return server,thread,sock,port

    servers=[]
    try:
        first=start(upstream);servers.append(first)
        # Loopback origin with random gateway port is known after its socket binds.
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();port=sock.getsockname()[1]
        origin=f'http://127.0.0.1:{port}'
        app=create_app(GatewayConfig(token=TOKEN,upstream=f'http://127.0.0.1:{first[3]}',origins=(origin,)))
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False));thread=Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start();servers.append((server,thread,sock,port))
        deadline=time.monotonic()+5
        while not server.started and thread.is_alive() and time.monotonic()<deadline: time.sleep(.01)
        assert server.started
        with httpx.Client(base_url=origin,trust_env=False,timeout=3) as client:
            with client.stream('GET','/api/agent/runs/r/events') as response:
                lines=response.iter_lines()
                assert next(lines)=='id: 101'
                assert next(lines)=='event: delta'
                assert 'hello' in next(lines)
                assert next(lines)==''
                assert next(lines)==': keepalive'
                assert not release.is_set() # Response has not completed.
            assert closed.wait(3) # Disconnect releases upstream, not /cancel.
    finally:
        release.set()
        for server,thread,sock,_ in reversed(servers):
            server.should_exit=True;thread.join(5);sock.close()
            assert not thread.is_alive()


def test_gateway_normalizes_loopback_configuration_and_keeps_ipv6_local():
    config=GatewayConfig(token=TOKEN,upstream='HTTP://LOCALHOST:8080/',origins=('HTTP://LOCALHOST:8765/',))
    assert config.upstream=='http://localhost:8080'
    assert config.origins==('http://localhost:8765',)
    assert 'http://[::1]:8765' in GatewayConfig(token=TOKEN).origins


def test_gateway_redacts_json_escaped_tokens_after_decoding():
    token='test-only-"quoted"-token'
    def upstream(request):
        return httpx.Response(422,json={'detail': [{'input': token}],token:'value'})
    with TestClient(create_app(GatewayConfig(token=token),transport=httpx.MockTransport(upstream)),base_url=ORIGIN) as client:
        response=client.get('/api/agent/projects')
        assert response.status_code==422
        assert response.json()=={'detail':[{'input':'[REDACTED]'}],'[REDACTED]':'value'}


def test_static_directory_is_a_build_and_cannot_read_an_existing_parent_secret(tmp_path):
    with pytest.raises(ValueError,match='index.html'):
        GatewayConfig(token=TOKEN,dist_dir=tmp_path)
    dist=tmp_path/'dist';dist.mkdir();(dist/'index.html').write_text('<html>build</html>')
    (tmp_path/'private.txt').write_text('test-only-private-file')
    with TestClient(create_app(GatewayConfig(token=TOKEN,dist_dir=dist)),base_url=ORIGIN) as client:
        response=client.get('/%2e%2e/private.txt')
        assert response.status_code==404 and 'test-only-private-file' not in response.text


def test_gateway_model_catalog_and_session_selection_routes():
    seen = []
    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={'model': 'model-three'})
    with TestClient(create_app(GatewayConfig(token=TOKEN), transport=httpx.MockTransport(upstream)), base_url=ORIGIN) as client:
        assert client.get('/api/agent/projects/app/models').status_code == 200
        assert client.get('/api/agent/sessions/s/model').status_code == 200
        response = client.post('/api/agent/sessions/s/model', headers={'Origin': ORIGIN}, json={'model_id': 'OrgB/three', 'reasoning_effort': 'high'})
        assert response.status_code == 200
        assert [request.url.path for request in seen] == ['/v1/projects/app/models', '/v1/sessions/s/model', '/v1/sessions/s/model']
        assert all(request.headers['Authorization'] == 'Bearer ' + TOKEN for request in seen)
        assert b'OrgB/three' in seen[-1].content


def test_gateway_forwards_file_diff_and_rejects_unknown_query():
    seen = []
    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={'available': True, 'hunks': []})
    with TestClient(create_app(GatewayConfig(token=TOKEN), transport=httpx.MockTransport(upstream)), base_url=ORIGIN) as client:
        response = client.get('/api/agent/projects/app/git/diff', params={'path': 'a b.txt', 'scope': 'staged', 'full': '1'})
        assert response.status_code == 200
        assert seen[0].url.path == '/v1/projects/app/git/diff'
        assert seen[0].url.params['path'] == 'a b.txt'
        assert seen[0].url.params['scope'] == 'staged'
        assert seen[0].url.params['full'] == '1'
        assert client.get('/api/agent/projects/app/git/diff', params={'path': 'a', 'extra': '1'}).status_code == 422
