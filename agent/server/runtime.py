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
from agent.permission import PermissionMode
from agent.runner import AgentRunner, RunResult
from agent.tools.human_interaction import validate_human_input_values
from agent.sandbox import select_backend
from agent.session import SessionStore
from agent.server.config import ServerConfig
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


def runner_factory(config: ServerConfig, project_id: str, session_id: str) -> AgentRunner:
    return _runner_from_settings(config.settings_for(project_id), session_id)


def _runner_from_settings(settings: Settings, session_id: str) -> AgentRunner:
    store = SessionStore(settings.state_path)
    try:
        runner = AgentRunner(
            settings=settings, sandbox_config=settings.sandbox, session_store=store,
            thread_id=session_id, workspace=settings.sandbox.workspace,
        )
        runner.set_permission_mode(PermissionMode.ASK)
        return runner
    except BaseException:
        store.close()
        raise


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

    def session(self, session_id: str) -> dict[str, Any]:
        session = self.store.session(session_id)
        if session is None:
            raise ServiceError(404, "Unknown session")
        self.require_project(session["project_id"])
        return session

    def run(self, run_id: str) -> dict[str, Any]:
        run = self.store.run(run_id)
        if run is None:
            raise ServiceError(404, "Unknown run")
        self.require_project(run["project_id"])
        return run

    def submit(self, session_id: str, text: str | None, resume: bool) -> dict[str, Any]:
        with self.lock:
            session = self.session(session_id)
            if self.closing:
                raise ServiceError(503, "Service is shutting down")
            if any(active.project_id == session["project_id"] for active in self.active.values()):
                raise ServiceError(409, "Project already has an unfinished run")
            if len(self.active) >= self.config.max_runs:
                raise ServiceError(429, "Run capacity reached")
            run_id = str(uuid4())
            result = self.store.create_run(run_id, session)
            active = ActiveRun(session["project_id"])
            self.active[run_id] = active
            self.store.emit(run_id, "run_started", result)
            active.thread = Thread(target=self._work, args=(run_id, session, active, text, resume), daemon=True)
            try:
                active.thread.start()
            except BaseException:
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
        with self.lock:
            run = self.run(run_id)
            active = self.active.get(run_id)
            if active is None or run["status"] in TERMINAL:
                raise ServiceError(409, "Run has already ended")
            active.cancelled.set()
            if active.runner is not None:
                active.runner.request_cancel()
            active.commands.put(("cancel", None))
            self.store.update(run_id, "cancelling")
            return self.run(run_id)

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
                final = RunResult(status="cancelled") if active.cancelled.is_set() else result
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
                    # Retain admission reservation: unsafe to admit another writer.
                    with self.lock:
                        self.store.finish(run_id, "failed", {"status": "failed", "error": "Runner cleanup failed; restart service"})
                    return
            with self.lock:
                self.store.finish(run_id, final.status, final)
                self.active.pop(run_id, None)

    def close(self) -> None:
        with self.lock:
            if self.closing:
                return
            self.closing = True
            active_runs = list(self.active.items())
            for run_id, active in active_runs:
                if self.run(run_id)["status"] not in TERMINAL:
                    self.cancel(run_id)
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
