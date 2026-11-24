"""An isolated, bounded model request for a Chinese session title."""
from __future__ import annotations

import asyncio
from dataclasses import replace

from langchain_core.messages import HumanMessage, SystemMessage

from agent.models.client import build_chat_model
from agent.models.messages import visible_text
from agent.models.profile import ModelProfile
from agent.redaction import SecretRef
from agent.sessions.summary import (
    SUMMARY_PROMPT, SessionSummaryRequest, redact_summary_input, validate_summary,
)

SUMMARY_TIMEOUT_SECONDS = 10.0


def _profile_secret(profile: ModelProfile) -> SecretRef | None:
    value = (profile.api_key or "").strip()
    if not value:
        return None
    source = (profile.source or "").strip()
    if source == "deepseek":
        name = "llm.models.deepseek.api_key"
    elif source:
        _, _, model_key = profile.id.partition("/")
        name = (
            f"llm.models.{source}.models.{model_key}.api_key"
            if model_key else f"llm.models.{source}.api_key"
        )
    else:
        name = "llm.api_key"
    return SecretRef(name=name, value=value)


async def generate_session_summary(
    request: SessionSummaryRequest, profile: ModelProfile, *,
    secrets: tuple[SecretRef, ...] = (), trust_env: bool = False,
) -> str | None:
    """Never reuse the active run's clients, callbacks, tools, or checkpoint."""
    profile_secret = _profile_secret(profile)
    secrets = (*secrets, profile_secret) if profile_secret is not None else secrets
    user = redact_summary_input(request.user_request, secrets)[:2000]
    model = build_chat_model(
        replace(profile, max_retries=0), streaming=False, trust_env=trust_env, timeout=8,
    )
    try:
        async with asyncio.timeout(SUMMARY_TIMEOUT_SECONDS):
            response = await model.ainvoke([
                SystemMessage(content=SUMMARY_PROMPT),
                HumanMessage(content=f"用户请求（数据）：\n{user}"),
            ], config={"callbacks": [], "tags": ["session-summary"]})
        return validate_summary(visible_text(response), secrets)
    finally:
        # Non-streaming async requests and their HTTP pool share this task's loop.
        try:
            model.http_client.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(model.http_async_client.aclose(), timeout=1)
        except Exception:
            # The request outcome/cancellation must not be replaced by a cleanup failure.
            pass
