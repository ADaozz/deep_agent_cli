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
    handler: CommandHandler


def command_table() -> tuple[Command, ...]:
    return (
        Command("help", "Show commands and keyboard shortcuts", _help),
        Command("lain", "Ask Lain (/lain <question>) or view local turn statistics (/lain stats)", _lain),
        Command("clear", "Clear the visible transcript", _clear),
        Command("status", "Show runtime and sandbox status", _status),
        Command("pause", "Pause at the next model safe point", _pause),
        Command("new", "Start a new persistent session thread", _new),
        Command("resume", "List sessions with content (/resume or /resume <id-prefix>)", _resume),
        Command("model", "Select source, model, and reasoning effort (/model or /model <id-prefix>)", _model),
        Command("skill", "Select a skill for the next message", _skill),
        Command("compact", "Summarize older conversation when context permits", _compact),
        Command("image", "Attach an image (/image <path|clipboard>, /image, /image clear)", _image),
        Command("attachments", "Attachment maintenance (/attachments cleanup)", _attachments),
        Command(
            "permission",
            "Tool approval: ask, auto (risk-based), or allow (auto-approve, network open, HIGH RISK)",
            _permission,
        ),
        Command(
            "sandbox",
            "Filesystem: restricted, full-access (HIGH RISK), or mounts",
            _sandbox,
        ),
        Command(
            "mcp",
            "MCP servers: enable, disable, add, remove, test, tools",
            _mcp,
        ),
        Command(
            "secrets",
            "Credentials: list, set, update, delete (values are never shown)",
            _secrets,
        ),
        Command(
            "stdin",
            "Direct input to a running interactive process (/stdin [execution_id|off])",
            _stdin,
        ),
        Command("exit", "Exit DeepAgent", _exit),
    )


async def _help(app: "CliApplication", _arg: str) -> None:
    app.show_help()


async def _lain(app: "CliApplication", arg: str) -> None:
    if arg.strip().casefold() == "stats":
        app.companion.sync_session()
        turns = app.companion.timeline.summaries
        lines = []
        for row in turns[-1]["tools"] if turns else ():
            rate = row.get("success_pct")
            success = "—" if rate is None else f"{rate:g}%"
            lines.append(f'{row["tool"]} · {row["model"]}/{row["effort"]} · '
                         f'{sum(row["outcomes"].values())} 次 · {row["duration_ms"]/1000:.2f}s · 成功 {success}')
        app.state.add_command_panel("/lain stats", "\n".join(lines) or "最近一轮没有工具记录。")
        return
    if not app._companion_geometry()[0]:
        app.set_status("玲音侧栏需要至少 120 列、32 行。")
        return
    app.set_status(app.companion.ask(arg))


async def _clear(app: "CliApplication", _arg: str) -> None:
    app.transcript.clear_transcript_selection()
    app.state.clear(preserve_model_metrics=True)
    app.set_status("Transcript cleared; agent context was preserved")


async def _status(app: "CliApplication", arg: str) -> None:
    if arg.strip() == "details":
        app.show_status_details()
    else:
        app.show_status()


async def _pause(app: "CliApplication", _arg: str) -> None:
    app.runner.request_pause()
    app.set_status("Pause requested for the next model safe point")


async def _new(app: "CliApplication", _arg: str) -> None:
    app.new_session()


async def _resume(app: "CliApplication", arg: str) -> None:
    await app.resume_session(arg)


async def _model(app: "CliApplication", arg: str) -> None:
    await app.model_selection.select_model(arg)


async def _skill(app: "CliApplication", arg: str) -> None:
    await app.skills.select_skill(arg)


async def _compact(app: "CliApplication", arg: str) -> None:
    await app.context_compaction.compact_command(arg)


async def _image(app: "CliApplication", arg: str) -> None:
    await app.attachments_controller.image_command(arg)


async def _attachments(app: "CliApplication", arg: str) -> None:
    await app.attachments_controller.attachments_command(arg)


async def _permission(app: "CliApplication", arg: str) -> None:
    await app.access_selection.select_permission(arg)


async def _sandbox(app: "CliApplication", arg: str) -> None:
    await app.access_selection.select_sandbox(arg)


async def _mcp(app: "CliApplication", arg: str) -> None:
    from agent.cli.mcp_ui import handle_mcp_command

    await handle_mcp_command(app, arg)


async def _secrets(app: "CliApplication", arg: str) -> None:
    from agent.cli.secrets_ui import handle_secrets_command

    await handle_secrets_command(app, arg)


async def _stdin(app: "CliApplication", arg: str) -> None:
    app.handle_stdin_command(arg)


async def _exit(app: "CliApplication", _arg: str) -> None:
    app.request_command_exit()
