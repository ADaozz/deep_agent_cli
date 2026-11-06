#!/usr/bin/env python3
"""Fixed-task performance baseline for the agent loop and the TUI hot paths.

  python examples/benchmark.py agent [--repeat 20]
  python examples/benchmark.py cli [--blocks 5000] [--repeat 200]
  python examples/benchmark.py live --prompt "..." [--repeat 3]

``agent`` drives a scripted model through ls/read_file/glob tool rounds on a
temporary workspace, so model latency is zero and the remaining time is the
graph, middleware and tool overhead. ``cli`` times one streamed delta, one
tool completion lookup, one transcript frame and one footer render against a
long synthetic session. ``live`` runs a real configured model (no TUI) and is
meant for before/after comparison with a fixed model, prompt and config.

Every mode prints JSON with P50/P95 of each measurement.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _percentiles(samples: list[float]) -> dict[str, float | None]:
    if not samples:
        return {"p50": None, "p95": None, "n": 0}
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, max(0, round(0.95 * len(ordered)) - 1))]
    return {"p50": statistics.median(ordered), "p95": p95, "n": len(ordered)}


def _scripted_model(messages: list[Any]) -> Any:
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    class ScriptedModel(GenericFakeChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # type: ignore[override]
            return self

        @property
        def _llm_type(self) -> str:
            return "scripted-benchmark"

    return ScriptedModel(messages=iter(messages))


def _agent_script() -> list[Any]:
    from langchain_core.messages import AIMessage

    return [
        AIMessage(content="", tool_calls=[{"id": "ls-1", "name": "ls", "args": {"path": "/"}}]),
        AIMessage(content="", tool_calls=[
            {"id": "read-1", "name": "read_file", "args": {"file_path": "/pkg/mod_0.py"}},
            {"id": "read-2", "name": "read_file", "args": {"file_path": "/pkg/mod_1.py"}},
        ]),
        AIMessage(content="", tool_calls=[{"id": "glob-1", "name": "glob", "args": {"pattern": "**/*.py"}}]),
        AIMessage(content="The package has 50 modules."),
    ]


def _workspace(root: Path) -> None:
    package = root / "pkg"
    package.mkdir(parents=True, exist_ok=True)
    for index in range(50):
        body = "\n".join(f"def function_{index}_{line}():\n    return {line}" for line in range(200))
        (package / f"mod_{index}.py").write_text(body + "\n", encoding="utf-8")


def bench_agent(repeat: int) -> dict[str, Any]:
    from deepagents.backends import FilesystemBackend

    from agent.factory import create_agent
    from agent.performance import RunMetrics
    from agent.runner import AgentRunner

    build: list[float] = []
    runs: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="deep-agent-bench-") as directory:
        root = Path(directory)
        _workspace(root)
        for index in range(repeat):
            started = time.perf_counter()
            prepared = create_agent(
                model=_scripted_model(_agent_script()),
                backend=FilesystemBackend(root_dir=root, virtual_mode=True),
            )
            metrics = RunMetrics()
            runner = AgentRunner(prepared=prepared, thread_id=f"bench-{index}", on_event=metrics)
            build.append(time.perf_counter() - started)
            try:
                result = runner.invoke("Summarize the package.")
            finally:
                runner.close()
            if result.status != "completed":
                raise SystemExit(f"benchmark run {index} ended {result.status}: {result.error}")
            runs.append(metrics.summary())
    return _report_runs(runs) | {"build_seconds": _percentiles(build)}


def bench_sandbox(repeat: int) -> dict[str, Any]:
    import shutil

    from agent.config import SandboxConfig
    from agent.sandbox import BubblewrapBackend, WorkspaceCompositeBackend, sandbox_pool_of

    report: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="deep-agent-bench-") as directory:
        root = Path(directory)
        _workspace(root)
        backend = WorkspaceCompositeBackend(
            BubblewrapBackend(SandboxConfig(workspace=root), executable=shutil.which("bwrap")), skills_dir=None,
        )
        try:
            started = time.perf_counter()
            first = backend.execute("true")
            report["cold_execute_seconds"] = time.perf_counter() - started
            if first.exit_code != 0:
                raise SystemExit(f"sandbox execute failed: {first.output}")
            report["hot_execute_true"] = _time(repeat, lambda _index: backend.execute("true"))
            report["hot_execute_64k_output"] = _time(repeat, lambda _index: backend.execute("head -c 65536 /dev/zero | tr '\\0' x"))
            probe = backend.read(str(root / "pkg" / "mod_0.py"))
            if getattr(probe, "error", None):
                raise SystemExit(f"sandbox read failed: {probe.error}")
            report["read_file"] = _time(repeat, lambda index: backend.read(str(root / "pkg" / f"mod_{index % 50}.py")))
        finally:
            pool = sandbox_pool_of(backend)
            if pool is not None:
                pool.shutdown("benchmark finished")
    return report


def bench_live(prompt: str, repeat: int) -> dict[str, Any]:
    from agent.cli.main import create_runner
    from agent.config import Settings
    from agent.performance import RunMetrics

    settings = Settings.load()
    runs: list[dict[str, Any]] = []
    statuses: list[str] = []
    for _ in range(repeat):
        metrics = RunMetrics()
        runner = create_runner(settings)
        runner.on_event = metrics
        try:
            runner.new_session(title="benchmark")
            result = runner.invoke(prompt)
        finally:
            runner.close()
        statuses.append(result.status)
        runs.append(metrics.summary())
    return _report_runs(runs) | {"statuses": statuses}


def _report_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    def values(key: str) -> list[float]:
        return [float(run[key]) for run in runs if run.get(key) is not None]

    ttft = [value for run in runs for value in run["ttft_seconds"]]
    tools: dict[str, list[float]] = {}
    for run in runs:
        for name, seconds in run["tool_seconds_by_name"].items():
            tools.setdefault(name, []).append(seconds)
    return {
        "runs": len(runs),
        "wall_seconds": _percentiles(values("wall_seconds")),
        "model_seconds": _percentiles(values("model_seconds")),
        "tool_seconds": _percentiles(values("tool_seconds")),
        "scheduling_seconds": _percentiles(values("scheduling_seconds")),
        "ttft_seconds": _percentiles(ttft),
        "model_calls": _percentiles(values("model_calls")),
        "tool_calls": _percentiles(values("tool_calls")),
        "input_tokens": _percentiles(values("input_tokens")),
        "output_tokens": _percentiles(values("output_tokens")),
        "tool_seconds_by_name": {name: _percentiles(items) for name, items in sorted(tools.items())},
    }


def _long_state(blocks: int) -> Any:
    from agent.cli.state import CliState, MessageBlock, ToolBlock, TurnSummaryBlock
    from datetime import datetime, timezone

    state = CliState()
    now = datetime.now(timezone.utc)
    for index in range(blocks // 4):
        state.blocks.append(MessageBlock(kind="user", content=f"question {index}"))
        state.blocks.append(ToolBlock(
            tool_call_id=f"call-{index}", name="read_file",
            arguments={"file_path": f"/pkg/mod_{index}.py"}, output="x = 1\n" * 20, status="completed",
        ))
        state.blocks.append(MessageBlock(kind="assistant", content=f"answer {index} " * 30))
        state.blocks.append(TurnSummaryBlock(elapsed_seconds=1.0, finished_at=now))
    return state


def _time(repeat: int, step: Callable[[int], None]) -> dict[str, float | None]:
    samples: list[float] = []
    for index in range(repeat):
        started = time.perf_counter()
        step(index)
        samples.append(time.perf_counter() - started)
    return _percentiles(samples)


def bench_cli(blocks: int, repeat: int) -> dict[str, Any]:
    from agent.cli.rendering import TranscriptRenderer
    from agent.runner import RunEvent

    report: dict[str, Any] = {"blocks": blocks}
    tracemalloc.start()
    measured = _long_state(blocks)
    TranscriptRenderer().render_document(measured, 120)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del measured
    report["traced_memory_mib"] = {"current": current / 2**20, "peak": peak / 2**20}

    state = _long_state(blocks)
    state.apply(RunEvent(type="run_started"))
    state.apply(RunEvent(type="assistant_started"))
    text: list[str] = []

    def delta(index: int) -> None:
        text.append(f"token{index} ")
        state.apply(RunEvent(type="assistant_delta", content="".join(text)))

    report["assistant_delta"] = _time(repeat, delta)
    report["tool_completed_lookup"] = _time(repeat, lambda index: state.apply(RunEvent(
        type="tool_completed", tool_call_id=f"call-{index % 10}", name="read_file", content="x = 1\n" * 20,
    )))
    renderer = TranscriptRenderer()
    renderer.render_document(state, 120)

    def frame(index: int) -> None:
        delta(repeat + index)
        renderer.render_document(state, 120)

    report["frame_after_delta"] = _time(repeat, frame)
    report["run_completed"] = _time(min(repeat, 50), lambda _index: state.apply(RunEvent(type="run_completed")))
    report["footer"] = asyncio.run(_bench_footer(repeat))
    return report


async def _bench_footer(repeat: int) -> dict[str, float | None]:
    from deepagents.backends import StateBackend
    from langchain_core.messages import AIMessage
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from agent.cli.app import CliApplication
    from agent.runner import AgentRunner

    runner = AgentRunner(model=_scripted_model([AIMessage(content="unused")]), backend=StateBackend(),
                         thread_id="bench-footer")
    try:
        with create_pipe_input() as pipe:
            app = CliApplication(runner, input=pipe, output=DummyOutput())
            app._footer_text()
            return _time(repeat, lambda _index: app._footer_text())
    finally:
        runner.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    modes = parser.add_subparsers(dest="mode", required=True)
    agent = modes.add_parser("agent")
    agent.add_argument("--repeat", type=int, default=20)
    cli = modes.add_parser("cli")
    cli.add_argument("--blocks", type=int, default=5000)
    cli.add_argument("--repeat", type=int, default=200)
    sandbox = modes.add_parser("sandbox")
    sandbox.add_argument("--repeat", type=int, default=50)
    live = modes.add_parser("live")
    live.add_argument("--prompt", required=True)
    live.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if args.mode == "agent":
        report = bench_agent(args.repeat)
    elif args.mode == "sandbox":
        report = bench_sandbox(args.repeat)
    elif args.mode == "cli":
        report = bench_cli(args.blocks, args.repeat)
    else:
        report = bench_live(args.prompt, args.repeat)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
