#!/usr/bin/env python3
"""Live Qwen Responses reasoning/text stream smoke through AgentRunner.on_delta."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.config import Settings  # noqa: E402
from agent.llm import build_chat_model  # noqa: E402
from agent.factory import create_agent  # noqa: E402
from agent.runner import AgentRunner  # noqa: E402


def main() -> int:
    events: list[tuple[str, str]] = []

    def on_delta(kind: str, text: str) -> None:
        extra_len = 0
        if events and events[-1][0] == kind and text.startswith(events[-1][1]):
            extra_len = len(text) - len(events[-1][1])
        else:
            extra_len = len(text)
        events.append((kind, text))
        label = "思考" if kind == "reasoning" else "回答"
        print(f"[{label} +{extra_len} chars] {text[-80:]!r}", flush=True)

    settings = Settings.load(base_dir=ROOT)
    model = build_chat_model(settings.active_profile)
    runner = AgentRunner(
        prepared=create_agent(
            model=model,
            system_prompt="只用一两句话直接回答，不要调用任何工具。",
            skills=[],
        ),
        thread_id="stream-smoke",
        on_delta=on_delta,
    )
    result = runner.invoke("用一句话解释什么是 LangGraph interrupt。")
    print(f"\nstatus={result.status}")
    print(f"output={result.output!r}")
    kinds = {kind for kind, _ in events}
    if result.status == "failed":
        print(f"error={result.error}", file=sys.stderr)
        return 1
    if "assistant" not in kinds:
        print("no assistant.delta received", file=sys.stderr)
        return 1
    if "reasoning" not in kinds:
        print("no Qwen Responses reasoning delta received", file=sys.stderr)
        return 1
    print(f"delta_events={len(events)} kinds={sorted(kinds)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
