"""Authenticated REST API and durable SSE event transport."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import json
from pathlib import Path
from typing import Annotated, Any, Literal
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.server.config import ServerConfig
from agent.server.commits import commit_history
from agent.server.files import FileRequestError, directory_files, preview_file
from agent.server.gitinfo import GitRequestError, file_diff, workspace_git
from agent.server.workspaces import directories
from agent.server.runtime import RunManager, ServiceError, runner_factory
from agent.server.store import TERMINAL


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSession(Body):
    project_id: str


class CreateRun(Body):
    text: str | None = Field(default=None, max_length=100_000)
    resume: bool = False
    file_paths: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def check_input(self) -> CreateRun:
        if self.resume == (self.text is not None or bool(self.file_paths)):
            raise ValueError("Provide text or file paths, or resume=true")
        if self.text is not None and not self.text.strip() and not self.file_paths:
            raise ValueError("text must not be blank")
        if any(not path.strip() or any(char in path for char in ('\n', '\r', '\x00')) for path in self.file_paths):
            raise ValueError("File paths must be nonempty single-line strings")
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


class WorkspacePath(Body):
    path: str = Field(min_length=1, max_length=4096)


class DirectoryPath(Body):
    path: str | None = Field(default=None, max_length=4096)


class PermissionSelection(Body):
    mode: Literal["ask", "allow"]
    confirmation: str = Field(default="", max_length=16)


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
    def projects(service: RunManager = Depends(manager)):
        return {"projects": [{"id": item["id"]} for item in service.workspaces()["workspaces"]]}

    @api.get("/workspaces")
    def workspaces(service: RunManager = Depends(manager)):
        return {**service.workspaces(), "home_path": str(Path.home())}

    @api.post("/workspaces")
    def select_workspace(body: WorkspacePath, service: RunManager = Depends(manager)):
        return service.select_workspace(body.path)

    @api.post("/workspace-directories")
    def workspace_directories(body: DirectoryPath):
        try:
            return directories(body.path)
        except (OSError, ValueError):
            raise HTTPException(422, "无法读取该目录，请检查路径和访问权限") from None

    @api.get("/projects/{project_id}/files")
    def project_files(project_id: str, path: str = Query(default='', max_length=4096),
                      offset: int = Query(default=0, ge=0, le=100000),
                      limit: int = Query(default=200, ge=1, le=500),
                      service: RunManager = Depends(manager)):
        service.require_project(project_id)
        try:
            return directory_files(config.projects[project_id], path, offset, limit)
        except FileRequestError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @api.get("/projects/{project_id}/file-preview")
    def project_file_preview(project_id: str, path: str = Query(min_length=1, max_length=4096),
                             service: RunManager = Depends(manager)):
        service.require_project(project_id)
        try:
            return preview_file(config.projects[project_id], path)
        except FileRequestError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @api.get("/projects/{project_id}/git/commits")
    def project_commits(project_id: str, limit: int = Query(default=20, ge=1, le=50),
                        cursor: str | None = Query(default=None, max_length=80),
                        service: RunManager = Depends(manager)):
        service.require_project(project_id)
        try:
            return commit_history(config.projects[project_id], limit, cursor)
        except GitRequestError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @api.get("/projects/{project_id}/git/diff")
    def project_git_diff(
        project_id: str,
        path: str = Query(min_length=1, max_length=4096),
        scope: Literal["all", "staged", "unstaged"] = "all",
        full: bool = False,
        service: RunManager = Depends(manager),
    ):
        service.require_project(project_id)
        try:
            return file_diff(config.projects[project_id], path, scope, full=full)
        except GitRequestError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @api.get("/projects/{project_id}/git")
    def project_git(project_id: str, service: RunManager = Depends(manager)):
        service.require_project(project_id)
        return workspace_git(config.projects[project_id])

    @api.get("/projects/{project_id}/capabilities")
    def capabilities(project_id: str, service: RunManager = Depends(manager)):
        return service.capabilities(project_id)

    @api.get("/sessions/{session_id}/permission")
    def permission(session_id: str, service: RunManager = Depends(manager)):
        return service.session_permission(session_id)

    @api.post("/sessions/{session_id}/permission")
    def change_permission(session_id: str, body: PermissionSelection, service: RunManager = Depends(manager)):
        return service.change_session_permission(session_id, body.mode, body.confirmation)

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
        return service.create_shared_session(body.project_id)

    @api.get("/sessions")
    def sessions(project_id: str | None = None, service: RunManager = Depends(manager)):
        if project_id is not None:
            service.require_project(project_id)
        return {"sessions": service.list_sessions(project_id)}

    @api.get("/sessions/{session_id}")
    def session(session_id: str, service: RunManager = Depends(manager)):
        return service.session(session_id)

    @api.delete("/sessions/{session_id}")
    def discard_session(session_id: str, service: RunManager = Depends(manager)):
        try:
            return service.discard_shared_session(session_id)
        except ServiceError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @api.get("/sessions/{session_id}/transcript")
    def session_transcript(session_id: str, limit: int = Query(default=50, ge=1, le=100),
                           cursor: str | None = Query(default=None, max_length=128),
                           service: RunManager = Depends(manager)):
        return service.session_transcript(session_id, limit, cursor)

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
        # Keep model validation and run admission atomic against model switches.
        with service.lock:
            service.validate_file_paths(session_id, body.file_paths)
            text = body.text
            if body.file_paths:
                parts = [text.strip()] if text and text.strip() else []
                text = "\n".join([*parts, *(path.strip() for path in body.file_paths)])
                if len(text) > 100_000:
                    raise ServiceError(422, "Prompt with file paths exceeds 100000 characters")
            return service.submit(session_id, text, body.resume)

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
