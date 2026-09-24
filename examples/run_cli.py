#!/usr/bin/env python3
"""pi-inspired interactive CLI over the existing DeepAgent runtime."""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from prompt_toolkit import PromptSession  # noqa: E402
from prompt_toolkit.validation import Validator  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402

from agent.cli import CliApplication  # noqa: E402
from agent.cli.app import default_config_dir  # noqa: E402
from agent.bootstrap import initialize_user_files  # noqa: E402
from agent.config import Settings, require_keybindings_outside_workspace  # noqa: E402
from agent.runner import AgentRunner  # noqa: E402
from agent.sandbox import ExecutionMode, SandboxUnavailableError, UNSANDBOXED_WARNING  # noqa: E402
from agent.session import SessionStore  # noqa: E402


def create_runner(settings: Settings) -> AgentRunner:
    session_store = SessionStore.for_workspace(
        settings.sandbox.workspace,
        override=settings.state_path,
    )
    try:
        runner = AgentRunner(
            settings=settings,
            sandbox_config=settings.sandbox,
            session_store=session_store,
            enable_sessions=True,
            workspace=settings.sandbox.workspace,
        )
    except SandboxUnavailableError as exc:
        if not sys.stdin.isatty():
            raise SystemExit(
                "Non-interactive startup refused UNSANDBOXED fallback. Install bwrap or set "
                "sandbox.allow_unsandboxed: true in ~/.deep-agent/config.yaml。"
            ) from exc
        _confirm_unsandboxed(str(exc))
        runner = AgentRunner(
            settings=settings,
            sandbox_config=replace(settings.sandbox, allow_unsandboxed=True),
            session_store=session_store,
            enable_sessions=True,
            workspace=settings.sandbox.workspace,
        )
    return runner


def _confirm_unsandboxed(error: str) -> None:
    Console(stderr=True).print(Panel(
        f"[bold red]{error}[/bold red]\n\n{UNSANDBOXED_WARNING}\n\n"
        "Type [bold]UNSANDBOXED[/bold] to accept the risk. Escape/Ctrl+C cancels startup.",
        title="Sandbox unavailable",
        border_style="red",
    ))
    validator = Validator.from_callable(
        lambda value: value == "UNSANDBOXED",
        error_message="Enter UNSANDBOXED exactly, or press Ctrl+C to cancel.",
        move_cursor_to_end=True,
    )
    try:
        PromptSession().prompt("Confirm: ", validator=validator, validate_while_typing=False)
    except (EOFError, KeyboardInterrupt) as exc:
        raise SystemExit("Startup cancelled.") from exc


def parse_startup_args(argv: list[str]) -> tuple[str | None, bool]:
    if not argv:
        return None, False
    if argv[0] in {"-h", "--help"}:
        raise SystemExit("Usage: deep-agent [resume [session-id]]")
    if argv[0] != "resume":
        raise SystemExit(f"Unknown command: {argv[0]}\nUsage: deep-agent [resume [session-id]]")
    return (argv[1] if len(argv) > 1 else None), True


def main(argv: list[str] | None = None) -> None:
    created = initialize_user_files()
    if created is not None:
        Console(stderr=True).print(
            f"Created {created} and ~/.deep-agent/skills/.\n"
            "Edit the model endpoint and API key in the config, then run deep-agent again."
        )
        return
    resume_id, open_picker = parse_startup_args(sys.argv[1:] if argv is None else argv)
    settings = Settings.load()
    config_dir = settings.config_dir or default_config_dir()
    require_keybindings_outside_workspace(config_dir, settings.sandbox.workspace)
    runner = create_runner(settings)
    if runner.prepared.execution_mode is ExecutionMode.UNSANDBOXED:
        Console(stderr=True).print(f"[bold red]UNSANDBOXED: {runner.prepared.security_warning}[/bold red]")
    Console(stderr=True).print(f"[dim]workspace={settings.sandbox.workspace}[/dim]")
    app = CliApplication(runner, config_dir=config_dir)
    if resume_id:
        try:
            app._apply_session_snapshot(runner.switch_session(resume_id))
        except (KeyError, RuntimeError) as exc:
            raise SystemExit(str(exc)) from exc
    elif open_picker:
        app._resume_picker_on_start = True
    try:
        app.run()
    finally:
        message = app.continue_session_message()
        if message:
            Console().print(message)


if __name__ == "__main__":
    main()
