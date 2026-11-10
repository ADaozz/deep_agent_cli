"""Terminal context compaction use case."""

from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from agent.runtime.runner import AgentRunner
    from agent.cli.session_controller import SessionController
    from agent.cli.view_state import DialogState, InputDrafts, RunPresentation
    from agent.cli.state import CliState
    from prompt_toolkit.buffer import Buffer

from typing import Any


class ContextCompaction:
    def __init__(self, *, runner: AgentRunner, sessions: Callable[[], SessionController], dialogs: DialogState, run_view: RunPresentation, run_blocking: Callable[..., Awaitable[Any]], set_status: Callable[..., None], state: CliState, invalidate: Callable[[], None]) -> None:
        self.runner = runner
        self.sessions = sessions
        self.dialogs = dialogs
        self.run_view = run_view
        self.run_blocking = run_blocking
        self.set_status = set_status
        self.state = state
        self.invalidate = invalidate

    async def compact_command(self, arg: str = "") -> None:
        if arg.strip():
            self.state.add_system("Usage: /compact", error=True)
            return
        if self.state.running or self.dialogs.current is not None:
            self.set_status("Finish the current run or interaction before compacting")
            return
        if self.sessions().waiting:
            self.set_status(self.sessions().wait_status(), transient=False)
            return
        self.run_view.compacting = True
        self.state.running = True
        self.set_status("Compacting context", transient=False)
        try:
            result = await self.run_blocking(self.runner.compact_context)
        except Exception as exc:  # noqa: BLE001
            self.state.add_system(f"Context compaction failed: {exc}", error=True)
            self.set_status("Compaction failed")
            return
        finally:
            self.run_view.compacting = False
            self.state.running = False
            self.invalidate()
        if result.status == "ineligible":
            if result.percent is not None:
                notice = (
                    "Manual compaction is allowed at about 42.5% of the context window; "
                    f"current usage: {result.percent:.1f}%."
                )
                if result.percent >= 42.5:
                    notice += " Deep Agents has not accepted the latest model usage as eligible."
                self.state.add_system(notice)
            elif result.window_tokens > 0:
                self.state.add_system(
                    "Manual compaction is allowed at about 42.5% of the context window; "
                    "current usage is unknown because the model has not reported token usage."
                )
            else:
                self.state.add_system(
                    "Manual compaction requires about 85,000 tokens with the current unknown "
                    "context window; current usage percentage is unavailable. Configure context_window to show it."
                )
            self.set_status("Manual compaction unavailable")
        elif result.status == "nothing_to_compact":
            self.state.add_system("There are no older messages to compact yet.")
            self.set_status("No older context to compact")
        elif result.status == "compacted":
            self.state.add_system(result.message)
            self.set_status("Context compacted")
        else:
            self.state.add_system(result.message or "Context compaction failed", error=True)
            self.set_status("Compaction failed")
