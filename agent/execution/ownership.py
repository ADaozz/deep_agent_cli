"""Execution ownership: LangGraph thread_id (session or SubAgent run_id)."""

from __future__ import annotations

from contextvars import ContextVar, Token


_THREAD_ID: ContextVar[str] = ContextVar("deep_agent_exec_thread_id", default="")


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
