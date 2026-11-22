"""Independent DeepSeek Flash risk classification."""
from __future__ import annotations

import asyncio
import json

import httpx

from agent.models.stream_lifecycle import ModelStreamCancelled
from agent.runtime.approval.policy import INSTRUCTIONS, MANUAL, parse_assessment, validate_assessment
from agent.runtime.approval.stats import approval_stats
from agent.runtime.approval.types import Decision

async def classify(api_key, trust_env, evidence, cancelled):
    """Bound total review time and cancel the independent HTTP request with the run."""
    started = asyncio.get_running_loop().time()

    async def review():
        async with httpx.AsyncClient(
            trust_env=trust_env, timeout=8, headers={"Authorization": f"Bearer {api_key}"},
        ) as client:
            for attempt in range(2):
                prompt = INSTRUCTIONS
                if attempt:
                    prompt += " 再次审查用户授权、路径边界、网络目标及不可逆影响。仍不确定时选 ask。"
                response = await client.post("https://api.deepseek.com/v1/chat/completions", json={
                    "model": "deepseek-flash", "stream": False,
                    "reasoning_effort": "low" if attempt else "none",
                    "max_tokens": 1024 if attempt else 256,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": evidence}],
                })
                response.raise_for_status()
                value = json.loads(response.json()["choices"][0]["message"]["content"])
                assessment = parse_assessment(value)
                decision = validate_assessment(assessment)
                if assessment is not None:
                    approval_stats.record_risk(assessment.risk_level)
                # Only the model's own "ask" triggers a second look; coerced asks do not.
                if assessment is None or assessment.decision != "ask" or attempt:
                    return decision

    task = asyncio.create_task(review())
    try:
        async with asyncio.timeout(18):
            while not task.done():
                if cancelled():
                    raise ModelStreamCancelled("Auto approval cancelled")
                await asyncio.wait({task}, timeout=0.1)
            result = task.result()
            approval_stats.record_flash(result.decision, asyncio.get_running_loop().time() - started)
            return result
    except TimeoutError:
        approval_stats.record_timeout()
        return MANUAL
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        approval_stats.record_flash("ask", asyncio.get_running_loop().time() - started)
        return MANUAL
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
