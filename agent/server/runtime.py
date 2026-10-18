"""One worker per admitted run; all Runner operations stay on that worker."""
from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
import logging
import os
from queue import Queue
from threading import Event, RLock, Thread
import time
from typing import Any, Callable
from uuid import uuid4

from agent.config import Settings
from agent.permission import PermissionMode, allow_mode_available, PERMISSION_ALLOW_WARNING
from agent.runner import AgentRunner, RunResult
from agent.tools.human_interaction import validate_human_input_values
from agent.sandbox import select_backend, sandbox_pool_of
from agent.session import SessionStore, try_acquire_workspace
from agent.session_lock import SessionLockBusyError
from agent.server.config import ServerConfig
from agent.server.workspaces import restore_workspaces, prepare_workspace, save_workspace
from agent.server.store import ServiceStore, TERMINAL

LOG = logging.getLogger(__name__)
WAITING = frozenset({"waiting_confirmation", "waiting_human", "paused", "runtime_config_boundary"})


class ServiceError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ActiveRun:
    project_id: str
    commands: Queue = field(default_factory=Queue)
    cancelled: Event = field(default_factory=Event)
    runner: Any = None
    thread: Thread | None = None
    workspace_lease: Any = None


def runner_factory(config: ServerConfig, project_id: str, session_id: str) -> AgentRunner:
    return _runner_from_settings(config.settings_for(project_id), session_id)


def open_session_store(settings: Settings) -> SessionStore:
    """The CLI session catalog for this workspace. See SessionStore.for_workspace."""
    return SessionStore.for_workspace(settings.sandbox.workspace, override=settings.state_path)


def _runner_from_settings(settings: Settings, session_id: str) -> AgentRunner:
    store = open_session_store(settings)
    try:
        runner = AgentRunner(
            settings=settings, sandbox_config=settings.sandbox, session_store=store,
            thread_id=session_id, workspace=settings.sandbox.workspace,
        )
        return runner
    except BaseException:
        store.close()
        raise


def _cancel_outcome(result: RunResult) -> RunResult:
    """Terminal result of a run whose cancel was accepted.

    The Runner's own outcome decides: work that completed (or failed) before
    the cancel took effect keeps that status, and a cancel whose model request
    was not confirmed closed is a failure rather than a clean cancellation.
    Only a worker that stopped waiting for an interaction has no Runner
    outcome of its own; nothing ran after the cancel there.
    """
    if result.status in {"completed", "failed"}:
        return result
    if result.status == "cancelled" and result.error:
        return RunResult(status="failed", error=result.error)
    return RunResult(status="cancelled")


class RunManager:
    def __init__(self, config: ServerConfig, factory: Callable = runner_factory) -> None:
        self.config = config
        self.factory = factory
        self.lock = RLock()
        self.active: dict[str, ActiveRun] = {}
        self.closing = False
        self.settings_by_project: dict[str, Settings] = {}
        config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lease = (config.state_dir / "service.lock").open("a+")
        os.chmod(config.state_dir / "service.lock", 0o600)
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lease.close()
            raise RuntimeError("Only one service process may use this state directory") from None
        try:
            restore_workspaces(config)
            # Validate model/paths and sandbox before accepting requests.
            secrets = [config.token]
            if factory is runner_factory:
                for project_id in config.projects:
                    settings = config.settings_for(project_id)
                    self.settings_by_project[project_id] = settings
                    select_backend(settings.sandbox)
                    secrets.extend(profile.api_key for profile in settings.llm_profiles)
                    secrets.append(settings.tavily_api_key or "")
            self.store = ServiceStore(config.state_dir / "service.sqlite3", tuple(secrets))
        except BaseException:
            self.lease.close()
            raise

    def require_project(self, project_id: str) -> None:
        if project_id not in self.config.projects:
            raise ServiceError(404, "Unknown project")

    def _catalog(self, project_id: str) -> SessionStore:
        settings = self.settings_by_project.get(project_id)
        override = settings.state_path if settings is not None else None
        return SessionStore.for_workspace(self.config.projects[project_id], override=override)

    def _catalog_session(self, session_id: str) -> dict[str, Any] | None:
        for project_id in self.config.projects:
            catalog = self._catalog(project_id)
            try:
                info = catalog.get(session_id)
            finally:
                catalog.close()
            if info is None:
                continue
            title = "" if info.title in ("", "New session") else info.title
            return {
                "id": info.id, "project_id": project_id,
                "created_at": info.created_at.isoformat(), "title": title,
            }
        return None

    def session(self, session_id: str) -> dict[str, Any]:
        session = self.store.session(session_id)
        if session is None:
            session = self._catalog_session(session_id)
        if session is None:
            raise ServiceError(404, "Unknown session")
        self.require_project(session["project_id"])
        return session

    def create_shared_session(self, project_id: str) -> dict[str, Any]:
        """Create one thread in the CLI session catalog and remember it for HTTP runs."""
        self.require_project(project_id)
        catalog = self._catalog(project_id)
        try:
            info = catalog.create_session()
        finally:
            catalog.close()
        record = self.store.create_session(info.id, project_id)
        record["title"] = ""
        return record

    def list_sessions(self, project_id: str | None) -> list[dict[str, Any]]:
        rows = {
            row["id"]: row for row in self.store.sessions(project_id)
            if row["project_id"] in self.config.projects
        }
        projects = [project_id] if project_id else list(self.config.projects)
        for pid in projects:
            catalog = self._catalog(pid)
            try:
                for info in catalog.list_sessions(limit=100):
                    title = "" if info.title in ("", "New session") else info.title
                    current = rows.get(info.id)
                    if current is None:
                        rows[info.id] = {
                            "id": info.id, "project_id": pid,
                            "created_at": info.created_at.isoformat(), "title": title,
                        }
                    elif title and not current.get("title"):
                        current["title"] = title
            finally:
                catalog.close()
        return sorted(rows.values(), key=lambda row: row["created_at"], reverse=True)[:100]

    def discard_shared_session(self, session_id: str) -> dict[str, Any]:
        found = self.session(session_id)
        if not self.store.delete_empty_session(session_id):
            raise ServiceError(409, "会话已有任务，不能删除")
        catalog = self._catalog(found["project_id"])
        try:
            catalog.delete_unused(session_id)
        finally:
            catalog.close()
        return {"deleted": True}

    def run(self, run_id: str) -> dict[str, Any]:
        run = self.store.run(run_id)
        if run is None:
            raise ServiceError(404, "Unknown run")
        self.require_project(run["project_id"])
        return run

    def workspaces(self) -> dict[str, Any]:
        with self.lock:
            return {"workspaces": [{"id": key, "path": str(path)} for key, path in self.config.projects.items()]}

    def select_workspace(self, path: str) -> dict[str, Any]:
        with self.lock:
            if self.closing:
                raise ServiceError(503, "Service is shutting down")
            try:
                project_id, candidate = prepare_workspace(self.config, path)
                if project_id in self.config.projects:
                    return {"id": project_id, "path": str(self.config.projects[project_id])}
                settings = candidate.settings_for(project_id)
                if self.factory is runner_factory:
                    select_backend(settings.sandbox)
            except (OSError, ValueError):
                raise ServiceError(422, "无法选择该目录：请确认路径存在，且不与已配置 workspace 或服务配置/状态目录重叠") from None
            try:
                save_workspace(self.config, project_id, candidate.projects[project_id])
            except (OSError, ValueError):
                raise ServiceError(503, "Unable to save workspace selection") from None
            self.config.projects.update(candidate.projects)
            self.settings_by_project[project_id] = settings
            return {"id": project_id, "path": str(candidate.projects[project_id])}

    def switch_branch(self, project_id: str, branch: str) -> dict[str, Any]:
        from agent.server.gitinfo import switch_branch
        with self.lock:
            self.require_project(project_id)
            if self.closing:
                raise ServiceError(503, "Service is shutting down")
            if any(active.project_id == project_id for active in self.active.values()):
                raise ServiceError(409, "请先结束当前 workspace 的任务或交互")
            try:
                return switch_branch(self.config.projects[project_id], branch)
            except ValueError as exc:
                raise ServiceError(409, str(exc)) from None

    def _model_settings(self, project_id: str) -> Settings:
        self.require_project(project_id)
        if project_id not in self.settings_by_project:
            try:
                self.settings_by_project[project_id] = self.config.settings_for(project_id)
            except Exception:
                raise ServiceError(503, "Model configuration is unavailable") from None
        return self.settings_by_project[project_id]

    @staticmethod
    def _model_selection(settings: Settings, model_id: str, effort: str) -> dict[str, Any]:
        try:
            profile = settings.get_profile(model_id)
        except KeyError:
            profile = settings.active_profile
            effort = "default"
        if effort != "default" and effort not in profile.reasoning_efforts:
            effort = "default"
        return {"model_id": profile.id, "model": profile.model,
                "source": profile.source or profile.id.split("/", 1)[0], "reasoning_effort": effort}

    def models(self, project_id: str) -> dict[str, Any]:
        with self.lock:
            settings = self._model_settings(project_id)
            return {"models": [{"id": profile.id, "model": profile.model,
                                "source": profile.source or profile.id.split("/", 1)[0],
                                "reasoning_efforts": list(profile.reasoning_efforts) or ["default"],
                                "input": list(profile.input)}
                               for profile in settings.list_profiles()],
                    "current": self._model_selection(settings, settings.llm_default, "default")}

    def session_model(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            session = self.session(session_id)
            settings = self._model_settings(session["project_id"])
            model_id, effort = settings.llm_default, "default"
            store = open_session_store(settings)
            try:
                saved = store.get(session_id)
                if saved is not None:
                    model_id, effort = saved.model_id or model_id, saved.reasoning_effort
            finally:
                store.close()
            return self._model_selection(settings, model_id, effort)

    def change_session_model(self, session_id: str, model_id: str, effort: str) -> dict[str, Any]:
        # No active Runner is touched from a request thread. Reserve the project
        # under the admission lock while a short-lived idle Runner owns its lease.
        # Selection and persistence use the same path as CLI /model.
        with self.lock:
            session = self.session(session_id)
            if self.closing:
                raise ServiceError(503, "Service is shutting down")
            if any(active.project_id == session["project_id"] for active in self.active.values()):
                raise ServiceError(409, "Finish the current run or interaction before switching models")
            settings = self._model_settings(session["project_id"])
            try:
                profile = settings.get_profile(model_id)
                if profile.id != model_id or (effort != "default" and effort not in profile.reasoning_efforts):
                    raise ValueError
            except (KeyError, ValueError):
                raise ServiceError(422, "Unknown model or unsupported reasoning effort") from None
            runner = None
            try:
                runner = (_runner_from_settings(settings, session_id) if self.factory is runner_factory
                          else self.factory(self.config, session["project_id"], session_id))
                if runner.current_interrupt() is not None:
                    raise ServiceError(409, "Resolve the session interruption before switching models")
                events = []
                runner.request_model_change(profile.id, reasoning_effort=effort)
                runner._apply_pending_runtime_config(events.append)
                if any(event.type == "runtime_config_failed" for event in events):
                    raise ServiceError(503, "Unable to apply model configuration")
                current = runner.current_model()
                return self._model_selection(settings, current.id, runner.reasoning_effort())
            except ServiceError:
                raise
            except SessionLockBusyError:
                raise ServiceError(409, "Session is already open in another process") from None
            except Exception:
                raise ServiceError(503, "Unable to apply model configuration") from None
            finally:
                if runner is not None:
                    try:
                        runner.close()
                    finally:
                        if runner.session_store is not None:
                            runner.session_store.close()

    def validate_file_paths(self, session_id: str, paths: list[str]) -> None:
        from agent.server.file_references import RULES, is_image_path
        if not any(is_image_path(path) for path in paths):
            return
        with self.lock:
            session = self.session(session_id)
            settings = self._model_settings(session["project_id"])
            selection = self.session_model(session_id)
            if not settings.get_profile(selection["model_id"]).supports_input("image"):
                raise ServiceError(422, RULES["image_model_required_message"])

    def capabilities(self, project_id: str) -> dict[str, Any]:
        # Use the same backend sources and native loader as CLI /skill.
        from types import SimpleNamespace
        from agent.cli.skills import load_skill_catalog, SkillDraft
        from agent.factory import _default_skill_sources
        with self.lock:
            settings = self._model_settings(project_id)
            selected = select_backend(settings.sandbox)
            try:
                catalog = load_skill_catalog(SimpleNamespace(
                    backend=selected.backend, skill_sources=_default_skill_sources(selected.backend) or [],
                ))
                skills = []
                for item in sorted(catalog.get("skills_metadata", []), key=lambda item: item["name"]):
                    draft = SkillDraft()
                    marker = draft.display(item["name"], item["path"])
                    skills.append({"name": item["name"], "path": item["path"],
                                   "description": item["description"], "instruction": draft.expand(marker)})
                return {"skills": skills, "warnings": [str(e) for e in catalog.get("skills_load_errors", [])],
                        "allow_available": allow_mode_available(selected.mode), "allow_warning": PERMISSION_ALLOW_WARNING}
            finally:
                pool = sandbox_pool_of(selected.backend)
                if pool is not None:
                    pool.shutdown()

    def session_permission(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            session = self.session(session_id)
            settings = self._model_settings(session["project_id"])
            mode = "ask"
            store = open_session_store(settings)
            try:
                saved = store.get(session_id)
                if saved is not None:
                    mode = saved.permission_mode or "ask"
            finally:
                store.close()
            return {"mode": mode}

    def change_session_permission(self, session_id: str, mode: str, confirmation: str) -> dict[str, Any]:
        with self.lock:
            session = self.session(session_id)
            if self.closing:
                raise ServiceError(503, "Service is shutting down")
            if any(active.project_id == session["project_id"] for active in self.active.values()):
                raise ServiceError(409, "Finish the current run or interaction before switching permission")
            if mode == "allow" and confirmation != "ALLOW":
                raise ServiceError(422, "Type ALLOW to confirm")
            settings = self._model_settings(session["project_id"])
            runner = None
            try:
                runner = (_runner_from_settings(settings, session_id) if self.factory is runner_factory
                          else self.factory(self.config, session["project_id"], session_id))
                if runner.current_interrupt() is not None:
                    raise ServiceError(409, "Resolve the session interruption before switching permission")
                if mode == "allow" and not allow_mode_available(runner.prepared.execution_mode):
                    raise ServiceError(422, "ALLOW requires SANDBOXED execution")
                events = []
                runner.request_permission_change(PermissionMode(mode))
                runner._apply_pending_runtime_config(events.append)
                if any(event.type == "runtime_config_failed" for event in events):
                    raise ServiceError(503, "Unable to apply permission configuration")
                return {"mode": runner.permission_mode().value}
            except ServiceError:
                raise
            except SessionLockBusyError:
                raise ServiceError(409, "Session is already open in another process") from None
            except Exception:
                raise ServiceError(503, "Unable to apply permission configuration") from None
            finally:
                if runner is not None:
                    try:
                        runner.close()
                    finally:
                        if runner.session_store is not None:
                            runner.session_store.close()

    def session_runs(self, session_id: str, limit: int, cursor: str | None) -> dict[str, Any]:
        self.session(session_id)
        try:
            return self.store.session_runs(session_id, limit, cursor)
        except ValueError as exc:
            raise ServiceError(422, str(exc)) from exc

    def active_run(self, project_id: str) -> dict[str, Any]:
        with self.lock:
            self.require_project(project_id)
            for run_id, active in self.active.items():
                if active.project_id == project_id:
                    run = self.run(run_id)
                    if run["status"] in TERMINAL:
                        return {"run": None, "blocked": True}
                    keys = ("id", "session_id", "project_id", "status", "created_at", "updated_at")
                    return {"run": {key: run[key] for key in keys}}
            return {"run": None}

    def submit(self, session_id: str, text: str | None, resume: bool) -> dict[str, Any]:
        with self.lock:
            session = self.session(session_id)
            if self.closing:
                raise ServiceError(503, "Service is shutting down")
            if any(active.project_id == session["project_id"] for active in self.active.values()):
                raise ServiceError(409, "Project already has an unfinished run")
            if len(self.active) >= self.config.max_runs:
                raise ServiceError(429, "Run capacity reached")
            settings = self.settings_by_project.get(session["project_id"])
            lease = try_acquire_workspace(
                self.config.projects[session["project_id"]],
                state_path=None if settings is None else settings.state_path,
            )
            if lease is None:
                raise ServiceError(409, "这个 workspace 正在被另一个 Deep Agent 使用")
            if self.store.session(session["id"]) is None:
                self.store.create_session(session["id"], session["project_id"])
            run_id = str(uuid4())
            result = self.store.create_run(run_id, session, "" if resume or not text else text)
            active = ActiveRun(session["project_id"], workspace_lease=lease)
            self.active[run_id] = active
            self.store.emit(run_id, "run_started", result)
            active.thread = Thread(target=self._work, args=(run_id, session, active, text, resume), daemon=True)
            try:
                active.thread.start()
            except BaseException:
                lease.release()
                del self.active[run_id]
                self.store.finish(run_id, "failed", {"status": "failed", "error": "Unable to start worker"})
                raise
            return result

    def respond(self, run_id: str, kind: str, interrupt_id: str, payload: Any) -> dict[str, Any]:
        with self.lock:
            run = self.run(run_id)
            expected = {"approvals": "waiting_confirmation", "human-input": "waiting_human"}
            allowed = {expected[kind]} if kind in expected else {"paused", "runtime_config_boundary"}
            if run["status"] not in allowed or run["interrupt_id"] != interrupt_id or run_id not in self.active:
                raise ServiceError(409, "Interaction is no longer pending")
            if kind == "approvals":
                pending = {call["toolCallId"] for call in run["result"]["pending_tool_calls"]}
                ids = [decision["tool_call_id"] for decision in payload]
                if len(ids) != len(set(ids)) or set(ids) != pending:
                    raise ServiceError(409, "Decisions must cover each pending tool exactly once")
            if kind == "human-input":
                # This waiting snapshot contains Runner's InterruptState.payload;
                # the matching interrupt_id ties it to the current declaration.
                try:
                    validate_human_input_values(payload, run["result"]["human_input"])
                except ValueError as exc:
                    raise ServiceError(422, str(exc)) from exc
            self.store.update(run_id, "running")
            self.store.emit(run_id, "interaction_submitted", {"interrupt_id": interrupt_id})
            self.active[run_id].commands.put((kind, payload))
            return self.run(run_id)

    def cancel(self, run_id: str) -> dict[str, Any]:
        """Accept a cancel request; the worker publishes the terminal state."""
        with self.lock:
            run = self.run(run_id)
            active = self.active.get(run_id)
            if active is None or run["status"] in TERMINAL:
                raise ServiceError(409, "Run has already ended")
            first = not active.cancelled.is_set()
            runner = active.runner
            if first:
                active.cancelled.set()
                active.commands.put(("cancel", None))
                self.store.update(run_id, "cancelling")
            snapshot = self.run(run_id)
        # Signalling the Runner touches tools, sandboxes and SQLite, so it
        # stays outside the manager lock. A worker that has not published its
        # runner yet sees ``cancelled`` (or replays it at run_started).
        if first and runner is not None:
            runner.request_cancel()
        return snapshot

    @staticmethod
    def _interruption(runner: Any) -> RunResult:
        state = runner.current_interrupt()
        if state is None:
            raise ValueError("Session has no interruption to resume; submit a new text task")
        return RunResult(
            status=state.kind.value, pending_tool_calls=list(state.pending_tools),
            human_input=state.payload if state.kind.value == "waiting_human" else {},
        )

    def _work(self, run_id: str, session: dict[str, Any], active: ActiveRun, text: str | None, resume: bool) -> None:
        runner = None
        final = RunResult(status="failed", error="Worker failed")
        def on_event(event: Any) -> None:
            # begin_run clears Runner's cancel flag. Reapply cancellation at its
            # first event so a cancel racing with startup cannot be lost.
            if event.type == "run_started" and active.cancelled.is_set():
                runner.request_cancel()
            self.store.emit(run_id, "agent_event", event)

        callbacks = {
            "on_delta": lambda kind, text: self.store.emit(run_id, "delta", {"kind": kind, "text": text}),
            "on_event": on_event,
        }
        try:
            if self.factory is runner_factory:
                runner = _runner_from_settings(self.settings_by_project[session["project_id"]], session["id"])
            else:
                runner = self.factory(self.config, session["project_id"], session["id"])
            with self.lock:
                active.runner = runner
            if active.cancelled.is_set():
                final = RunResult(status="cancelled")
            else:
                if resume:
                    result = self._interruption(runner)
                elif runner.current_interrupt() is not None:
                    result = RunResult(status="failed", error="Session is interrupted; submit resume=true")
                else:
                    result = runner.invoke(text, **callbacks)
                while result.status in WAITING and not active.cancelled.is_set():
                    with self.lock:
                        if active.cancelled.is_set():
                            break
                        self.store.update(run_id, result.status, result, str(uuid4()))
                        self.store.emit(run_id, "interaction_requested", self.run(run_id))
                    kind, payload = active.commands.get()
                    if kind == "cancel" or active.cancelled.is_set():
                        break
                    try:
                        if kind == "approvals":
                            result = runner.decide_tools(payload, **callbacks)
                        elif kind == "human-input":
                            result = runner.submit_human_input(payload, **callbacks)
                        elif result.status == "paused":
                            result = runner.continue_run(**callbacks)
                        else:
                            result = runner.resume_runtime_config(**callbacks)
                    except ValueError:
                        # Bad answers must not lose a pending question or release the project.
                        self.store.emit(run_id, "interaction_rejected", {"error": "Invalid interaction response"})
                        result = self._interruption(runner)
                final = _cancel_outcome(result) if active.cancelled.is_set() else result
        except Exception:
            # Exceptions may embed endpoint credentials. Keep public errors generic.
            LOG.error("Agent worker failed (%s)", run_id)
            final = RunResult(status="failed", error="Agent initialization or execution failed")
        finally:
            if runner is not None:
                try:
                    runner.close()
                    if getattr(runner, "session_store", None) is not None:
                        runner.session_store.close()
                except Exception:
                    LOG.error("Runner cleanup failed (%s)", run_id)
                    # Keep the workspace lease and the admission slot until restart.
                    with self.lock:
                        self.store.finish(run_id, "failed", {"status": "failed", "error": "Runner cleanup failed; restart service"})
                    return
            lease = active.workspace_lease
            if lease is not None:
                lease.release()
                active.workspace_lease = None
            with self.lock:
                self.store.finish(run_id, final.status, final)
                self.active.pop(run_id, None)

    def close(self) -> None:
        with self.lock:
            if self.closing:
                return
            self.closing = True
            active_runs = list(self.active.items())
        for run_id, _active in active_runs:
            try:
                self.cancel(run_id)
            except ServiceError:
                pass
        deadline = time.monotonic() + 35
        for _, active in active_runs:
            if active.thread is not None:
                active.thread.join(max(0, deadline - time.monotonic()))
        if any(active.thread and active.thread.is_alive() for _, active in active_runs):
            raise RuntimeError("Agent workers did not drain; service lease retained until process exit")
        if self.active:
            raise RuntimeError("Runner cleanup failed; service lease retained until process exit")
        self.store.close()
        self.lease.close()
