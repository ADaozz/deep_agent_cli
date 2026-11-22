"""Independent DeepSeek Flash review request."""
from __future__ import annotations

import asyncio
import json

import httpx

from agent.models.stream_lifecycle import ModelStreamCancelled
from agent.runtime.approval.policy import INSTRUCTIONS, MANUAL, parse_verdict


async def classify(api_key, trust_env, evidence, cancelled):
    """Send one request and cancel it with the run. The batch caller bounds total time; failures go to the human."""
    async def review():
        async with httpx.AsyncClient(
            trust_env=trust_env, timeout=8, headers={"Authorization": f"Bearer {api_key}"},
        ) as client:
            response = await client.post("https://api.deepseek.com/v1/chat/completions", json={
                "model": "deepseek-flash", "stream": False, "reasoning_effort": "none", "max_tokens": 256,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": evidence}],
            })
            response.raise_for_status()
            return parse_verdict(json.loads(response.json()["choices"][0]["message"]["content"]))

    task = asyncio.create_task(review())
    try:
        while not task.done():
            if cancelled():
                raise ModelStreamCancelled("Auto approval cancelled")
            await asyncio.wait({task}, timeout=0.1)
        return task.result()
    except ModelStreamCancelled:
        raise
    except Exception:
        return MANUAL
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
