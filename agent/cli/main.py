#!/usr/bin/env python3
"""Installed entry point for the interactive Deep Agent CLI."""
from __future__ import annotations

import argparse
import sys

from rich.console import Console

from agent.cli import CliApplication
from agent.cli.app import default_config_dir
from agent.bootstrap import initialize_user_files
from agent.config import ConfigError, Settings, require_keybindings_outside_workspace
from agent.runner import AgentRunner
from agent.sandbox import SandboxUnavailableError
from agent.session import SessionStore
from agent.version import package_version
from agent.mcp.manager import MCPManager


def create_runner(settings: Settings) -> AgentRunner:
    session_store = SessionStore.for_workspace(
        settings.sandbox.workspace,
        override=settings.state_path,
    )
    config_dir = settings.sandbox.protected_config_dir or settings.config_dir or default_config_dir()
    mcp_manager = MCPManager(config_dir / "mcp.json", trust_env=settings.trust_env)
    mcp_manager.load()
    try:
        return AgentRunner(
            settings=settings,
            sandbox_config=settings.sandbox,
            session_store=session_store,
            enable_sessions=True,
            persist_on_first_message=True,
            workspace=settings.sandbox.workspace,
            mcp_manager=mcp_manager,
        )
    except SandboxUnavailableError as exc:
        mcp_manager.close()
        raise SystemExit(str(exc)) from exc
    except BaseException:
        mcp_manager.close()
        raise


def parse_startup_args(argv: list[str]) -> tuple[str | None, bool]:
    parser = argparse.ArgumentParser(
        prog="deep-agent",
        description="Interactive sandboxed coding agent for Linux and WSL2 (Python 3.12+).",
        epilog=("First launch creates ~/.deep-agent/config.yaml and exits. Configure your model "
                "endpoint and key, then launch from your project in an interactive terminal. "
                "DEEP_AGENT_CONFIG selects an alternate config outside the workspace. "
                "Sandboxing requires working Bubblewrap."),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {package_version()}")
    commands = parser.add_subparsers(dest="command", title="commands")
    resume = commands.add_parser("resume", help="Resume a saved session or open the session picker")
    resume.add_argument("session_id", nargs="?", help="Session ID or unique prefix; omit to select interactively")
    args = parser.parse_args(argv)
    return getattr(args, "session_id", None), args.command == "resume"


def main(argv: list[str] | None = None) -> None:
    startup_args = sys.argv[1:] if argv is None else argv
    resume_id, open_picker = parse_startup_args(startup_args)
    try:
        created = initialize_user_files()
    except (ValueError, OSError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    if created is not None:
        Console(stderr=True).print(
            f"Created {created} and ~/.deep-agent/skills/.\n"
            "Edit the model endpoint and API key in the config, then run deep-agent again."
        )
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("deep-agent requires an interactive terminal (TTY) for stdin and stdout. "
              "Run it directly in a terminal; piped input/output is not supported.", file=sys.stderr)
        raise SystemExit(2)
    try:
        settings = Settings.load()
        config_dir = settings.config_dir or default_config_dir()
        require_keybindings_outside_workspace(config_dir, settings.sandbox.workspace)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        runner = create_runner(settings)
    except ValueError as exc:
        print(f"Startup error: {exc}\nCheck the sandbox and model settings in your config.", file=sys.stderr)
        raise SystemExit(2) from None
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
