"""Explicit local gateway with bearer injection and unbuffered event transport."""
from contextlib import asynccontextmanager
import json
import re
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from agent.web_gateway.config import GatewayConfig

ROUTES = (
    ("GET", "/health", "/health"),
    ("GET", "/workspaces", "/v1/workspaces"),
    ("POST", "/workspaces", "/v1/workspaces"),
    ("POST", "/workspace-directories", "/v1/workspace-directories"),
    ("GET", "/projects/{project_id}/git/diff", "/v1/projects/{project_id}/git/diff"),
    ("GET", "/projects/{project_id}/git/commits", "/v1/projects/{project_id}/git/commits"),
    ("GET", "/projects/{project_id}/files", "/v1/projects/{project_id}/files"),
    ("GET", "/projects/{project_id}/file-preview", "/v1/projects/{project_id}/file-preview"),
    ("GET", "/projects", "/v1/projects"),
    ("GET", "/projects/{project_id}/git", "/v1/projects/{project_id}/git"),
    ("GET", "/projects/{project_id}/capabilities", "/v1/projects/{project_id}/capabilities"),
    ("GET", "/sessions/{session_id}/permission", "/v1/sessions/{session_id}/permission"),
    ("POST", "/sessions/{session_id}/permission", "/v1/sessions/{session_id}/permission"),
    ("GET", "/projects/{project_id}/models", "/v1/projects/{project_id}/models"),
    ("GET", "/sessions/{session_id}/model", "/v1/sessions/{session_id}/model"),
    ("POST", "/sessions/{session_id}/model", "/v1/sessions/{session_id}/model"),
    ("GET", "/sessions", "/v1/sessions"),
    ("POST", "/sessions", "/v1/sessions"),
    ("GET", "/sessions/{session_id}", "/v1/sessions/{session_id}"),
    ("DELETE", "/sessions/{session_id}", "/v1/sessions/{session_id}"),
    ("GET", "/sessions/{session_id}/runs", "/v1/sessions/{session_id}/runs"),
    ("POST", "/sessions/{session_id}/runs", "/v1/sessions/{session_id}/runs"),
    ("GET", "/projects/{project_id}/active-run", "/v1/projects/{project_id}/active-run"),
    ("GET", "/runs/{run_id}", "/v1/runs/{run_id}"),
    ("GET", "/runs/{run_id}/events", "/v1/runs/{run_id}/events"),
    *( ("POST", f"/runs/{{run_id}}/{action}", f"/v1/runs/{{run_id}}/{action}")
       for action in ("approvals", "human-input", "continue", "cancel") ),
)
QUERY = {"/sessions": {"project_id"}, "/sessions/{session_id}/runs": {"limit", "cursor"},
         "/projects/{project_id}/git/commits": {"limit", "cursor"},
         "/projects/{project_id}/files": {"path", "offset", "limit"},
         "/projects/{project_id}/file-preview": {"path"},
         "/runs/{run_id}/events": {"after"},
         "/projects/{project_id}/git/diff": {"path", "scope", "full"}}


def create_app(config: GatewayConfig, *, transport=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(base_url=config.upstream, transport=transport, trust_env=False,
                                     follow_redirects=False, timeout=httpx.Timeout(15, read=30)) as client:
            app.state.client = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = {urlsplit(origin).netloc for origin in config.origins}

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        if host not in allowed_hosts or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Only configured local origins may access this gateway"}, status_code=403)
        if origin and origin not in config.origins:
            return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)
        if request.method not in {"GET", "HEAD"}:
            referer = request.headers.get("referer")
            source = origin or (f"{urlsplit(referer).scheme}://{urlsplit(referer).netloc}" if referer else "")
            if source not in config.origins or urlsplit(source).netloc != host:
                return JSONResponse({"detail": "Same-origin POST required"}, status_code=403)
            if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
                return JSONResponse({"detail": "JSON POST required"}, status_code=415)
        return await call_next(request)

    def endpoint(template: str, upstream: str):
        async def proxy(request: Request):
            if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) for value in request.path_params.values()):
                return JSONResponse({"detail": "Invalid resource ID"}, status_code=422)
            if set(request.query_params) - QUERY.get(template, set()):
                return JSONResponse({"detail": "Unsupported query parameter"}, status_code=422)
            headers = {"Authorization": f"Bearer {config.token}", "Accept": "application/json"}
            stream = template.endswith("/events")
            if stream:
                headers["Accept"] = "text/event-stream"
                headers["Accept-Encoding"] = "identity"
                for cursor in (request.query_params.get("after"), request.headers.get("last-event-id")):
                    if cursor is not None and not re.fullmatch(r"\d+", cursor):
                        return JSONResponse({"detail": "Event cursor must be nonnegative"}, status_code=422)
                if request.headers.get("last-event-id") is not None:
                    headers["Last-Event-ID"] = request.headers["last-event-id"]
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > config.max_body_bytes:
                    return JSONResponse({"detail": "Request body too large"}, status_code=413)
            if request.method == "POST":
                headers["Content-Type"] = "application/json"
                if body:
                    try:
                        json.loads(body)
                    except (ValueError, UnicodeDecodeError):
                        return JSONResponse({"detail": "Invalid JSON"}, status_code=422)
            client = request.app.state.client
            path = upstream.format(**request.path_params)
            try:
                outgoing = client.build_request(request.method, path, params=request.query_params.multi_items(),
                                                headers=headers, content=bytes(body),
                                                timeout=httpx.Timeout(15, read=None if stream else 30))
                response = await client.send(outgoing, stream=stream)
            except httpx.HTTPError:
                return JSONResponse({"detail": "Agent service is unavailable"}, status_code=503)
            if stream and response.is_success:
                async def frames():
                    try:
                        async for chunk in response.aiter_raw():
                            yield chunk
                    except httpx.HTTPError:
                        # End this connection; the browser reconciles and resumes
                        # durable events. Never cancel the underlying Run.
                        return
                    finally:
                        await response.aclose()
                return StreamingResponse(frames(), media_type="text/event-stream", headers={
                    "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
                })
            try:
                raw = await response.aread()
                data = json.loads(raw)
                def redact(value):
                    if isinstance(value, str):
                        return value.replace(config.token, "[REDACTED]")
                    if isinstance(value, list):
                        return [redact(item) for item in value]
                    if isinstance(value, dict):
                        return {redact(key): redact(item) for key, item in value.items()}
                    return value
                data = redact(data)
            except (ValueError, UnicodeDecodeError):
                data = {"detail": "Unexpected response from Agent service"}
            finally:
                await response.aclose()
            return JSONResponse(data, status_code=response.status_code,
                                headers={"Cache-Control": "no-store"})
        return proxy

    for method, path, upstream in ROUTES:
        app.add_api_route("/api/agent" + path, endpoint(path, upstream), methods=[method])

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])
    def unknown_api(rest: str):
        return JSONResponse({"detail": "Unknown gateway route"}, status_code=404)

    if config.dist_dir is not None:
        app.mount("/", StaticFiles(directory=config.dist_dir, html=True))
    return app
