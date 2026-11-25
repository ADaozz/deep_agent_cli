"""Execution ownership context for interactive and yielded commands."""

from __future__ import annotations

from contextvars import ContextVar, Token


_THREAD_ID: ContextVar[str] = ContextVar("deep_agent_exec_thread_id", default="")
_AGENT_SCOPE: ContextVar[str] = ContextVar("deep_agent_exec_agent_scope", default="root")


def execution_thread_id() -> str:
    return _THREAD_ID.get()


def execution_agent_scope() -> str:
    return _AGENT_SCOPE.get()


def set_execution_ownership(*, thread_id: str, agent_scope: str = "root") -> tuple[Token[str], Token[str]]:
    return _THREAD_ID.set(thread_id), _AGENT_SCOPE.set(agent_scope)


def reset_execution_ownership(tokens: tuple[Token[str], Token[str]]) -> None:
    _THREAD_ID.reset(tokens[0])
    _AGENT_SCOPE.reset(tokens[1])
