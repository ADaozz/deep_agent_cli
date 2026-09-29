#!/usr/bin/env python3
"""Installed entry point for the interactive Deep Agent CLI."""
from __future__ import annotations

import sys
from dataclasses import replace
from prompt_toolkit import PromptSession
from prompt_toolkit.validation import Validator
from rich.console import Console
from rich.panel import Panel

from agent.cli import CliApplication
from agent.cli.app import default_config_dir
from agent.bootstrap import initialize_user_files
from agent.config import ConfigError, Settings, require_keybindings_outside_workspace
from agent.runner import AgentRunner
from agent.sandbox import ExecutionMode, SandboxUnavailableError, UNSANDBOXED_WARNING
from agent.session import SessionStore


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
        print("Usage: deep-agent [resume [session-id]]")
        raise SystemExit(0)
    if argv[0] != "resume" or len(argv) > 2:
        raise SystemExit(f"Unknown command: {argv[0]}\nUsage: deep-agent [resume [session-id]]")
    return (argv[1] if len(argv) > 1 else None), True


def main(argv: list[str] | None = None) -> None:
    resume_id, open_picker = parse_startup_args(sys.argv[1:] if argv is None else argv)
    created = initialize_user_files()
    if created is not None:
        Console(stderr=True).print(
            f"Created {created} and ~/.deep-agent/skills/.\n"
            "Edit the model endpoint and API key in the config, then run deep-agent again."
        )
        return
    try:
        settings = Settings.load()
    except (ConfigError, FileNotFoundError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    config_dir = settings.config_dir or default_config_dir()
    require_keybindings_outside_workspace(config_dir, settings.sandbox.workspace)
    runner = create_runner(settings)
    if runner.prepared.execution_mode is ExecutionMode.UNSANDBOXED:
        Console(stderr=True).print(f"[bold red]UNSANDBOXED: {runner.prepared.security_warning}[/bold red]")
    Console(stderr=True).print(f"[dim]workspace={settings.sandbox.workspace}[/dim]")
    app = CliApplication(runner, config_dir=config_dir)
    # Startup resume shares the controller workflow: a busy session waits
    # inside the TUI (Esc cancels) instead of aborting the launch.
    if resume_id:
        app.sessions.startup_session_id = resume_id
    elif open_picker:
        app.sessions.open_picker_on_start = True
    try:
        app.run()
    finally:
        message = app.continue_session_message()
        if message:
            Console().print(message)
        try:
            runner.close()
        except RuntimeError as exc:
            print(
                f"{exc}\nThe session lock is not released early; the operating "
                "system releases it when this process actually exits.",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
