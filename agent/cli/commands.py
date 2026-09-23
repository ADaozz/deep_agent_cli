from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from agent.cli.app import CliApplication


CommandHandler = Callable[["CliApplication", str], Awaitable[None]]


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    handler: CommandHandler | None = None
    unavailable_reason: str = ""


def command_table() -> tuple[Command, ...]:
    return (
        Command("help", "Show commands and keyboard shortcuts", _help),
        Command("clear", "Clear the visible transcript", _clear),
        Command("status", "Show runtime and sandbox status", _status),
        Command("pause", "Pause at the next model safe point", _pause),
        Command("new", "Start a new persistent session thread", _new),
        Command("session", "Show current session information", _session),
        Command("resume", "Resume another session (/resume or /resume <id-prefix>)", _resume),
        Command("model", "Select a model (/model or /model <id-prefix>)", _model),
        Command("image", "Attach an image (/image <path|clipboard>, /image, /image clear)", _image),
        Command("attachments", "Attachment maintenance (/attachments cleanup)", _attachments),
        Command(
            "permission",
            "Tool approval: ask (require approval) or allow (auto-approve, HIGH RISK)",
            _permission,
        ),
        Command("compact", "Compact context", unavailable_reason="the current runner has no compaction API"),
        Command("quit", "Exit DeepAgent", _quit),
        Command("exit", "Exit DeepAgent", _quit),
    )


async def _help(app: "CliApplication", _arg: str) -> None:
    app.show_help()


async def _clear(app: "CliApplication", _arg: str) -> None:
    app.state.clear()
    app.set_status("Transcript cleared; agent context was preserved")


async def _status(app: "CliApplication", _arg: str) -> None:
    app.show_status()


async def _pause(app: "CliApplication", _arg: str) -> None:
    app.runner.request_pause()
    app.set_status("Pause requested for the next model safe point")


async def _new(app: "CliApplication", _arg: str) -> None:
    app.new_session()


async def _session(app: "CliApplication", _arg: str) -> None:
    app.show_session()


async def _resume(app: "CliApplication", arg: str) -> None:
    await app.resume_session(arg)


async def _model(app: "CliApplication", arg: str) -> None:
    await app.select_model(arg)


async def _image(app: "CliApplication", arg: str) -> None:
    await app.image_command(arg)


async def _attachments(app: "CliApplication", arg: str) -> None:
    await app.attachments_command(arg)


async def _permission(app: "CliApplication", arg: str) -> None:
    await app.select_permission(arg)


async def _quit(app: "CliApplication", _arg: str) -> None:
    app.exit()
