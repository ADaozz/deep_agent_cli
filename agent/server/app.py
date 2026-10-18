"""Authenticated REST API and durable SSE event transport."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import json
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.server.config import ServerConfig
from agent.server.gitinfo import workspace_git
from agent.server.runtime import RunManager, ServiceError, runner_factory
from agent.server.store import TERMINAL


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSession(Body):
    project_id: str


class CreateRun(Body):
    text: str | None = Field(default=None, min_length=1, max_length=100_000)
    resume: bool = False

    @model_validator(mode="after")
    def check_input(self) -> CreateRun:
        if self.resume == (self.text is not None):
            raise ValueError("Provide either nonempty text or resume=true")
        if self.text is not None and not self.text.strip():
            raise ValueError("text must not be blank")
        return self


class ToolDecision(Body):
    tool_call_id: str
    decision: Literal["approve", "reject"]
    message: str | None = None


class Approval(Body):
    interrupt_id: str
    decisions: list[ToolDecision] = Field(min_length=1)


class HumanInput(Body):
    interrupt_id: str
    values: dict[str, Any]


class Continue(Body):
    interrupt_id: str


class ModelSelection(Body):
    model_id: str = Field(min_length=1, max_length=512)
    reasoning_effort: str = Field(min_length=1, max_length=64)


def create_app(config: ServerConfig, *, factory=runner_factory) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager = await asyncio.to_thread(RunManager, config, factory)
        app.state.manager = manager
        try:
            yield
        finally:
            await asyncio.to_thread(manager.close)

    app = FastAPI(title="Deep Agent Service", version="1", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    bearer = HTTPBearer(auto_error=False)

    def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        if credentials is None or not hmac.compare_digest(credentials.credentials.encode(), config.token.encode()):
            raise HTTPException(401, "Invalid service token", headers={"WWW-Authenticate": "Bearer"})

    def manager(request: Request) -> RunManager:
        return request.app.state.manager

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok"}

    @app.get("/openapi.json", dependencies=[Depends(authenticate)], include_in_schema=False)
    def openapi():
        return app.openapi()

    api = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)])

    @api.get("/projects")
    def projects():
        return {"projects": [{"id": project_id} for project_id in config.projects]}

    @api.get("/projects/{project_id}/git")
    def project_git(project_id: str, service: RunManager = Depends(manager)):
        service.require_project(project_id)
        return workspace_git(config.projects[project_id])

    @api.get("/projects/{project_id}/models")
    def models(project_id: str, service: RunManager = Depends(manager)):
        return service.models(project_id)

    @api.get("/sessions/{session_id}/model")
    def session_model(session_id: str, service: RunManager = Depends(manager)):
        return service.session_model(session_id)

    @api.post("/sessions/{session_id}/model")
    def change_model(session_id: str, body: ModelSelection, service: RunManager = Depends(manager)):
        return service.change_session_model(session_id, body.model_id, body.reasoning_effort)

    @api.post("/sessions", status_code=201)
    def create_session(body: CreateSession, service: RunManager = Depends(manager)):
        service.require_project(body.project_id)
        return service.store.create_session(str(uuid4()), body.project_id)

    @api.get("/sessions")
    def sessions(project_id: str | None = None, service: RunManager = Depends(manager)):
        if project_id is not None:
            service.require_project(project_id)
        return {"sessions": [session for session in service.store.sessions(project_id)
                             if session["project_id"] in config.projects]}

    @api.get("/sessions/{session_id}")
    def session(session_id: str, service: RunManager = Depends(manager)):
        return service.session(session_id)

    @api.get("/sessions/{session_id}/runs")
    def session_runs(session_id: str, limit: int = Query(default=50, ge=1, le=100),
                     cursor: str | None = Query(default=None, max_length=512),
                     service: RunManager = Depends(manager)):
        return service.session_runs(session_id, limit, cursor)

    @api.get("/projects/{project_id}/active-run")
    def active_run(project_id: str, service: RunManager = Depends(manager)):
        return service.active_run(project_id)

    @api.post("/sessions/{session_id}/runs", status_code=202)
    def submit(session_id: str, body: CreateRun, service: RunManager = Depends(manager)):
        return service.submit(session_id, body.text, body.resume)

    @api.get("/runs/{run_id}")
    def run(run_id: str, service: RunManager = Depends(manager)):
        return service.run(run_id)

    @api.post("/runs/{run_id}/approvals", status_code=202)
    def approvals(run_id: str, body: Approval, service: RunManager = Depends(manager)):
        return service.respond(run_id, "approvals", body.interrupt_id,
                               [item.model_dump() for item in body.decisions])

    @api.post("/runs/{run_id}/human-input", status_code=202)
    def human_input(run_id: str, body: HumanInput, service: RunManager = Depends(manager)):
        return service.respond(run_id, "human-input", body.interrupt_id, body.values)

    @api.post("/runs/{run_id}/continue", status_code=202)
    def continue_run(run_id: str, body: Continue, service: RunManager = Depends(manager)):
        return service.respond(run_id, "continue", body.interrupt_id, None)

    @api.post("/runs/{run_id}/cancel", status_code=202)
    def cancel(run_id: str, service: RunManager = Depends(manager)):
        return service.cancel(run_id)

    @api.get("/runs/{run_id}/events", response_class=StreamingResponse,
             responses={200: {"content": {"text/event-stream": {}}}})
    async def events(
        run_id: str, request: Request, after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None), service: RunManager = Depends(manager),
    ):
        service.run(run_id)
        if last_event_id is not None:
            try:
                after = int(last_event_id)
                if after < 0:
                    raise ValueError
            except ValueError:
                raise HTTPException(422, "Last-Event-ID must be a nonnegative integer") from None

        def snapshot(cursor: int):
            with service.store.lock:
                return service.store.events(run_id, cursor), service.run(run_id)["status"]

        async def stream():
            cursor = after
            heartbeat = asyncio.get_running_loop().time()
            while not await request.is_disconnected():
                batch, status = await asyncio.to_thread(snapshot, cursor)
                for item in batch:
                    cursor = item["id"]
                    yield f'id: {cursor}\nevent: {item["type"]}\ndata: {json.dumps(item["data"], ensure_ascii=False)}\n\n'
                if status in TERMINAL and not batch:
                    return
                if asyncio.get_running_loop().time() - heartbeat >= 15:
                    yield ": keepalive\n\n"
                    heartbeat = asyncio.get_running_loop().time()
                if not batch:
                    await asyncio.sleep(0.1)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    app.include_router(api)
    return app
