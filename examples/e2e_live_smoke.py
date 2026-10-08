#!/usr/bin/env python3
"""Live E2E smoke: slash commands + permission gates + sandboxed coding task.

Usage (opt-in; uses real API tokens):
  python examples/e2e_live_smoke.py --model source/model
  python examples/e2e_live_smoke.py --workspace /empty/scratch --network

Configuration is loaded from ~/.deep-agent/config.yaml (or DEEP_AGENT_CONFIG).
"""
from __future__ import annotations

import argparse
import asyncio
import tempfile
from contextlib import ExitStack
import sys
import time
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from prompt_toolkit.input.defaults import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

from agent.cli.app import CliApplication, default_config_dir  # noqa: E402
from agent.config import Settings, require_keybindings_outside_workspace  # noqa: E402
from agent.permission import PermissionMode, allow_mode_available  # noqa: E402
from agent.runner import AgentRunner  # noqa: E402
from agent.sandbox import ExecutionMode, SandboxUnavailableError  # noqa: E402
from agent.session import SessionStore  # noqa: E402


CODING_PROMPT = """在 /workspace 创建 hello_lib.py：函数 greet(name) 返回 "Hello, {name}!"。
再创建 test_hello_lib.py（使用 stdlib unittest）。
用 execute 运行（不要设置 network）：python3 -m unittest test_hello_lib.py -v
不要联网。完成后用一句话说明测试是否通过。"""

WRITE_PROMPT = (
    "只用 write_file 工具在 /workspace/e2e_ask_write.txt 写入一行文本 exactly: e2e-ask-ok。"
    "不要调用其它工具。写完后用一句话确认。"
)

NETWORK_PROMPT = (
    "只用 execute 工具，且必须设置 network=true，运行命令："
    "curl -sS -m 10 -o /dev/null -w '%{http_code}' https://example.com。"
    "不要写文件。把 HTTP 状态码作为最终回答。"
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        suffix = f" — {detail}" if detail else ""
        print(f"[{mark}] {name}{suffix}", flush=True)

    @property
    def failed(self) -> list[Check]:
        return [item for item in self.checks if not item.ok]


def _transcript_text(app: CliApplication) -> str:
    parts: list[str] = []
    for block in app.state.blocks:
        content = getattr(block, "content", "") or ""
        parts.append(str(content))
    return "\n".join(parts)


async def _cmd(app: CliApplication, text: str) -> None:
    await app._dispatch_command(text)


def _system_blob(app: CliApplication) -> str:
    return _transcript_text(app)


async def run_slash_suite(app: CliApplication, runner: AgentRunner, report: Report) -> str:
    """Exercise slash commands; returns session id before /new for later resume."""
    await _cmd(app, "/help")
    help_text = _system_blob(app)
    report.add("/help", "Keyboard" in help_text and "Commands" in help_text)

    await _cmd(app, "/status")
    status = _system_blob(app)
    report.add(
        "/status SANDBOXED",
        "SANDBOXED" in status and "Permission" in status,
        detail=status.split("Status:")[-1][:160] if "Status:" in status else status[-160:],
    )
    report.add(
        "execution_mode",
        runner.prepared.execution_mode is ExecutionMode.SANDBOXED,
        detail=runner.prepared.execution_mode.value,
    )

    await _cmd(app, "/session")
    session_text = _system_blob(app)
    report.add(
        "/session",
        runner.thread_id[:8] in session_text and ("sqlite" in session_text.lower() or "Session:" in session_text),
    )
    seed = runner.invoke("只回复 smoke-ready，不调用工具。")
    report.add("live model response", seed.status == "completed" and bool(seed.output))
    old_thread = runner.thread_id

    app.state.add_system("marker-before-clear")
    await _cmd(app, "/clear")
    report.add("/clear", not any("marker-before-clear" in getattr(b, "content", "") for b in app.state.blocks))

    original = runner.current_model()
    profiles = runner.list_models()
    target = next((item for item in profiles if item.id != original.id), original)
    for profile in (target, original):
        await _cmd(app, f"/model {profile.id}")
        picker = app.interaction
        report.add(f"/model {profile.id} reasoning picker", picker is not None and picker.kind == "model_reasoning")
        if picker is not None and picker.kind == "model_reasoning":
            picker.accept("")
            app._finish_interaction()
            runner._apply_pending_runtime_config()
            report.add(f"/model {profile.id} applied", runner.current_model().id == profile.id)

    app.cycle_model(delta=1)
    forward = runner.pending_model() or runner.current_model()
    runner._apply_pending_runtime_config()
    app.cycle_model(delta=-1)
    runner._apply_pending_runtime_config()
    report.add("cycle_model ±1", runner.current_model().id == original.id, detail=f"{forward.id}->{original.id}")

    await _cmd(app, "/permission ask")
    report.add("/permission ask", runner.permission_mode() is PermissionMode.ASK)

    await _cmd(app, "/permission allow")
    if allow_mode_available(runner.prepared.execution_mode):
        if app.interaction and app.interaction.kind == "permission_confirm":
            app.interaction.accept("nope")
            app._finish_interaction()
        report.add("/permission allow wrong token", runner.permission_mode() is PermissionMode.ASK)

        await _cmd(app, "/permission allow")
        if app.interaction and app.interaction.kind == "permission_confirm":
            app.interaction.accept("ALLOW")
            app._finish_interaction()
        runner._apply_pending_runtime_config()
        report.add("/permission allow ALLOW", runner.permission_mode() is PermissionMode.ALLOW)

        await _cmd(app, "/permission ask")
        runner._apply_pending_runtime_config()
        report.add("/permission back to ask", runner.permission_mode() is PermissionMode.ASK)
    else:
        report.add(
            "/permission allow rejected",
            runner.permission_mode() is PermissionMode.ASK and app.interaction is None,
        )

    await _cmd(app, "/compact")
    compact_text = _system_blob(app)
    report.add("/compact unavailable", "unavailable" in compact_text.lower() or "compaction" in compact_text.lower())

    await _cmd(app, "/pause")
    report.add("/pause", "pause" in app.state.status.lower())

    app.new_session()
    new_thread = runner.thread_id
    report.add("/new", new_thread != old_thread and len(app.state.blocks) <= 1, detail=f"{old_thread[:8]}->{new_thread[:8]}")

    await app.resume_session(old_thread[:8])
    report.add(
        "/resume",
        runner.thread_id == old_thread,
        detail=f"now={runner.thread_id[:8]} want={old_thread[:8]}",
    )
    return old_thread


def _invoke_with_retry(
    runner: AgentRunner,
    prompt: str,
    *,
    expect_status: str,
    label: str,
    report: Report,
    retries: int = 1,
    timeout_s: float = 180.0,
):
    last = None
    for attempt in range(retries + 1):
        text = prompt if attempt == 0 else (
            prompt + "\n\n重要：你必须调用指定工具，不要只回复文字。"
        )
        started = time.monotonic()
        try:
            last = runner.invoke(text)
        except Exception as exc:  # noqa: BLE001
            report.add(label, False, detail=f"exception: {exc}")
            traceback.print_exc()
            return None
        elapsed = time.monotonic() - started
        if elapsed > timeout_s:
            report.add(label, False, detail=f"timeout {elapsed:.1f}s status={last.status}")
            return last
        if last.status == expect_status:
            report.add(label, True, detail=f"status={last.status} {elapsed:.1f}s")
            return last
        if attempt < retries:
            print(f"  retry {label}: got {last.status}, output={last.output!r}"[:200], flush=True)
    report.add(
        label,
        False,
        detail=f"want={expect_status} got={getattr(last, 'status', None)} output={getattr(last, 'output', None)!r}"[:240],
    )
    return last


def run_permission_suite(runner: AgentRunner, workspace: Path, report: Report, *, network: bool = False) -> None:
    runner.set_permission_mode(PermissionMode.ASK)

    waiting = _invoke_with_retry(
        runner, WRITE_PROMPT, expect_status="waiting_confirmation", label="ask write_file interrupt", report=report,
    )
    if waiting and waiting.status == "waiting_confirmation":
        names = [c.get("name") for c in waiting.pending_tool_calls]
        report.add("ask write pending name", "write_file" in names, detail=str(names))
        resumed = runner.approve_tool(waiting.pending_tool_calls[0].get("toolCallId"))
        # May still need more tool rounds; drain approvals up to 5 times.
        for _ in range(5):
            if resumed.status != "waiting_confirmation":
                break
            call = resumed.pending_tool_calls[0]
            resumed = runner.approve_tool(call.get("toolCallId"))
        path = workspace / "e2e_ask_write.txt"
        report.add(
            "ask write_file approve",
            resumed.status == "completed" and path.is_file() and "e2e-ask-ok" in path.read_text(encoding="utf-8"),
            detail=f"status={resumed.status} exists={path.is_file()}",
        )

    if not network:
        print("Network tool checks not requested (enable with --network).", flush=True)
        return

    waiting_net = _invoke_with_retry(
        runner, NETWORK_PROMPT, expect_status="waiting_confirmation", label="ask network execute interrupt", report=report,
    )
    if waiting_net and waiting_net.status == "waiting_confirmation":
        call = waiting_net.pending_tool_calls[0]
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        report.add(
            "ask network declared",
            call.get("name") == "execute" and bool(args.get("network")),
            detail=str(args)[:120],
        )
        resumed = runner.approve_tool(call.get("toolCallId"))
        for _ in range(5):
            if resumed.status != "waiting_confirmation":
                break
            nxt = resumed.pending_tool_calls[0]
            resumed = runner.approve_tool(nxt.get("toolCallId"))
        report.add(
            "ask network approve completed",
            resumed.status == "completed",
            detail=f"status={resumed.status} out={resumed.output!r}"[:200],
        )

    if allow_mode_available(runner.prepared.execution_mode):
        runner.set_permission_mode(PermissionMode.ALLOW)
        allow_net = _invoke_with_retry(
            runner, NETWORK_PROMPT, expect_status="completed", label="allow network execute", report=report, retries=1,
        )
        if allow_net:
            report.add(
                "allow network no interrupt",
                allow_net.status == "completed",
                detail=f"out={allow_net.output!r}"[:160],
            )
    else:
        try:
            runner.set_permission_mode(PermissionMode.ALLOW)
            report.add("allow rejected when not sandboxed", False, detail="ALLOW unexpectedly accepted")
        except ValueError as exc:
            report.add("allow rejected when not sandboxed", "SANDBOXED" in str(exc))


def run_coding_suite(runner: AgentRunner, workspace: Path, report: Report) -> None:
    if allow_mode_available(runner.prepared.execution_mode):
        runner.set_permission_mode(PermissionMode.ALLOW)
    # Clean prior artifacts
    for name in ("hello_lib.py", "test_hello_lib.py"):
        path = workspace / name
        if path.exists():
            path.unlink()

    result = _invoke_with_retry(
        runner,
        CODING_PROMPT,
        expect_status="completed",
        label="coding invoke completed",
        report=report,
        retries=1,
        timeout_s=180.0,
    )
    lib = workspace / "hello_lib.py"
    test = workspace / "test_hello_lib.py"
    report.add("coding hello_lib.py", lib.is_file() and "greet" in lib.read_text(encoding="utf-8", errors="replace"))
    report.add(
        "coding test_hello_lib.py",
        test.is_file() and "unittest" in test.read_text(encoding="utf-8", errors="replace"),
    )
    if lib.is_file() and test.is_file():
        verify = runner.prepared.backend.execute("python3 -m unittest test_hello_lib.py -v", timeout=60)
        report.add(
            "coding unittest exit 0",
            verify.exit_code == 0,
            detail=f"code={verify.exit_code} out={verify.output[-240:]!r}",
        )
    else:
        report.add("coding unittest exit 0", False, detail="missing files")
    if result is not None:
        report.add("coding result status", result.status == "completed", detail=result.status)


async def async_main(args: argparse.Namespace) -> int:
    settings = Settings.load()
    if args.model:
        settings = replace(settings, llm_default=settings.get_profile(args.model).id)
    report = Report()
    with ExitStack() as resources:
        if args.workspace:
            workspace = args.workspace.expanduser().resolve()
            workspace.mkdir(parents=True, exist_ok=True)
            if any(workspace.iterdir()):
                print("E2E workspace must be empty; omit --workspace for an isolated temporary directory.", file=sys.stderr)
                return 2
        else:
            workspace = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="deep-agent-live-work-")))
        state_root = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="deep-agent-live-state-")))
        settings = replace(settings, sandbox=replace(settings.sandbox, workspace=workspace, allow_unsandboxed=False),
                           state_path=state_root / "sessions.sqlite3")
        config_dir = settings.config_dir or default_config_dir()
        require_keybindings_outside_workspace(config_dir, workspace)
        print(f"workspace={workspace} config={settings.source_path} model={settings.active_profile.id}", flush=True)
        store = SessionStore(settings.state_path)
        resources.callback(store.close)
        def make_runner():
            runner = AgentRunner(settings=settings, sandbox_config=settings.sandbox, session_store=store, workspace=workspace)
            resources.callback(runner.close)
            return runner
        try:
            runner = make_runner()
            with create_pipe_input() as pipe:
                app = CliApplication(runner, config_dir=config_dir, input=pipe, output=DummyOutput())
                resources.callback(app._io_executor.shutdown, wait=True)
                await run_slash_suite(app, runner, report)
            print("\n--- permission / optional network ---", flush=True)
            run_permission_suite(make_runner(), workspace, report, network=args.network)
            print("\n--- coding ---", flush=True)
            run_coding_suite(make_runner(), workspace, report)
        except SandboxUnavailableError as exc:
            print(f"UNSUPPORTED SANDBOX ENVIRONMENT [{exc.kind}]: {exc}", file=sys.stderr)
            return 2
        failed = report.failed
        print(f"\npassed={len(report.checks) - len(failed)} failed={len(failed)} total={len(report.checks)}", flush=True)
        for item in failed:
            print(f"  FAIL {item.name}: {item.detail}", flush=True)
        return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Opt-in live model smoke; uses real API tokens and an isolated workspace.")
    parser.add_argument("--model", help="Configured source/model ID or unique prefix (defaults to llm.default)")
    parser.add_argument("--workspace", type=Path, help="Empty scratch directory; default creates and removes a temporary workspace")
    parser.add_argument("--network", action="store_true", help="Also run external network tool checks against example.com")
    args = parser.parse_args(argv)
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
