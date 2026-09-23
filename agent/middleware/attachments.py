"""Resolve image references only in model requests, leaving checkpoints compact."""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from agent.attachments import AttachmentStore
from agent.llm import QwenChatOpenAI, materialize_attachment_refs


_active_store: ContextVar[AttachmentStore | None] = ContextVar("attachment_store", default=None)


def set_attachment_store(store: AttachmentStore) -> Token[AttachmentStore | None]:
    return _active_store.set(store)


def reset_attachment_store(token: Token[AttachmentStore | None]) -> None:
    _active_store.reset(token)


class AttachmentMaterializationMiddleware(AgentMiddleware):
    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        if isinstance(request.model, QwenChatOpenAI):
            return handler(request)
        store = _active_store.get()
        if store is None:
            return handler(request)
        messages = materialize_attachment_refs(request.messages, store)
        return handler(request.override(messages=messages)) if messages is not request.messages else handler(request)
