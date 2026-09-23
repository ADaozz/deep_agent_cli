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
from agent.config import Settings  # noqa: E402
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
                "sandbox.allow_unsandboxed: true in config.yaml。"
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


def main() -> None:
    # Prefer config from cwd, then the template package root. workspace: . → cwd.
    settings = Settings.load()
    runner = create_runner(settings)
    if runner.prepared.execution_mode is ExecutionMode.UNSANDBOXED:
        Console(stderr=True).print(f"[bold red]UNSANDBOXED: {runner.prepared.security_warning}[/bold red]")
    Console(stderr=True).print(f"[dim]workspace={settings.sandbox.workspace}[/dim]")
    config_dir = settings.config_dir or default_config_dir()
    CliApplication(runner, config_dir=config_dir).run()


if __name__ == "__main__":
    main()
