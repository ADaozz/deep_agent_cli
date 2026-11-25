"""Execution ownership and root session context.

- ``execution_thread_id()`` identifies the direct owner of an execution
  (root session ID or SubAgent ``run_id``) for ownership & cleanup isolation.
- ``root_session_id()`` identifies the root user session so SubAgents inherit
  the root session's authorized ``sandbox`` credentials without widening scope.
"""

from __future__ import annotations

from contextvars import ContextVar, Token


_THREAD_ID: ContextVar[str] = ContextVar("deep_agent_exec_thread_id", default="")
_ROOT_SESSION_ID: ContextVar[str] = ContextVar("deep_agent_root_session_id", default="")


def execution_thread_id() -> str:
    """Prefer the active LangGraph thread (root session or SubAgent run_id)."""
    try:
        from langgraph.config import get_config

        configured = str((get_config() or {}).get("configurable", {}).get("thread_id") or "").strip()
        if configured:
            return configured
    except Exception:  # noqa: BLE001
        pass
    return _THREAD_ID.get()


def set_execution_thread_id(thread_id: str) -> Token[str]:
    return _THREAD_ID.set(thread_id)


def reset_execution_thread_id(token: Token[str]) -> None:
    _THREAD_ID.reset(token)


def root_session_id() -> str:
    """Return the root user session ID for sandbox credential inheritance."""
    return _ROOT_SESSION_ID.get() or execution_thread_id()


def set_root_session_id(session_id: str) -> Token[str]:
    return _ROOT_SESSION_ID.set(session_id)


def reset_root_session_id(token: Token[str]) -> None:
    _ROOT_SESSION_ID.reset(token)
