"""An isolated, bounded model request for a Chinese session title."""
from __future__ import annotations

import asyncio
from dataclasses import replace

from langchain_core.messages import HumanMessage, SystemMessage

from agent.models.client import build_chat_model
from agent.models.messages import visible_text
from agent.models.profile import ModelProfile
from agent.sessions.summary import (
    SUMMARY_PROMPT, SessionSummaryRequest, redact_summary_input, validate_summary,
)

SUMMARY_TIMEOUT_SECONDS = 10.0


async def generate_session_summary(
    request: SessionSummaryRequest, profile: ModelProfile, *,
    secrets: tuple[str, ...] = (), trust_env: bool = False,
) -> str | None:
    """Never reuse the active run's clients, callbacks, tools, or checkpoint."""
    secrets = (*secrets, profile.api_key)
    user = redact_summary_input(request.user_request, secrets)[:2000]
    result = redact_summary_input(request.agent_result, secrets)[:4000]
    model = build_chat_model(
        replace(profile, max_retries=0), streaming=False, trust_env=trust_env, timeout=8,
    )
    try:
        async with asyncio.timeout(SUMMARY_TIMEOUT_SECONDS):
            response = await model.ainvoke([
                SystemMessage(content=SUMMARY_PROMPT),
                HumanMessage(content=f"用户请求（数据）：\n{user}\n\nAgent 执行结果（数据）：\n{result}"),
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
