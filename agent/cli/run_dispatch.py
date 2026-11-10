"""CLI run dispatch workflow."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Awaitable
from typing import Any
from functools import partial

from prompt_toolkit.buffer import Buffer

from agent.sessions.images import ImageAttachmentRef
from agent.cli.lifecycle import TaskStop
from agent.runtime.contracts import RunEvent


if TYPE_CHECKING:
    from agent.runtime.runner import AgentRunner
    from agent.cli.lifecycle import AppLifecycle
    from prompt_toolkit.buffer import Buffer
    from agent.cli.state import CliState
    from agent.cli.commands import Command
    from agent.cli.view_state import DialogState
    from agent.cli.view_state import InputDrafts
    from agent.cli.interaction_flow import InteractionFlow
    from agent.cli.run_events_controller import RunEventsController
    from agent.cli.view_state import RunPresentation
    from agent.cli.session_controller import SessionController
    from agent.cli.completion import SlashCompleter
    from agent.cli.working_messages import WorkingMessageRotation


class RunDispatch:
    def __init__(
        self, *,
        execute_command: Callable[[Command, str], Awaitable[None]],
        _on_event_thread: Callable[..., Any],
        _run_blocking: Callable[..., Awaitable[Any]],
        _working_messages: Callable[[], WorkingMessageRotation],
        buffer: Buffer,
        command_by_name: dict[str, Command],
        dialogs: DialogState,
        drafts: InputDrafts,
        interaction_flow: Callable[[], InteractionFlow],
        lifecycle: Callable[[], AppLifecycle],
        run_events: Callable[[], RunEventsController],
        run_view: RunPresentation,
        runner: AgentRunner,
        sessions: Callable[[], SessionController],
        set_status: Callable[..., None],
        slash_completer: SlashCompleter,
        state: CliState,
        invalidate: Callable[[], None],
        _call_in_run_epoch: Callable[..., Any],
    ) -> None:
        self.execute_command = execute_command
        self._on_event_thread = _on_event_thread
        self._run_blocking = _run_blocking
        self._working_messages = _working_messages
        self.buffer = buffer
        self.command_by_name = command_by_name
        self.dialogs = dialogs
        self.drafts = drafts
        self.interaction_flow = interaction_flow
        self.lifecycle = lifecycle
        self.run_events = run_events
        self.run_view = run_view
        self.runner = runner
        self.sessions = sessions
        self.set_status = set_status
        self.slash_completer = slash_completer
        self.state = state
        self.invalidate = invalidate
        self._call_in_run_epoch = _call_in_run_epoch


    def _accept_command_completion(self, buffer: Buffer) -> bool:
        state = buffer.complete_state
        if self.dialogs.current is not None or state is None or not state.completions:
            return False
        completion = state.current_completion or state.completions[0]
        buffer.apply_completion(completion)
        self.slash_completer.accepted_text = buffer.document.text_before_cursor
        self.invalidate()
        return True

    def _submit_buffer(self, queue_mode: str) -> None:
        self.slash_completer.accepted_text = None
        if self.sessions().waiting:
            self.set_status(self.sessions().wait_status(), transient=False)
            return
        displayed = self.buffer.text.strip()
        if self.drafts.skill.has_invalid_marker(displayed):
            self.state.add_system("A skill marker was edited. Restore it or remove it before submitting.", error=True)
            self.set_status("Edited skill marker")
            return
        if self.dialogs.current is None and self.drafts.skill.is_present(displayed):
            try:
                responses = self.runner.prepared.backend.download_files([self.drafts.skill.path])
            except (OSError, ValueError, RuntimeError) as exc:
                self.state.add_system(f"Cannot read selected skill: {exc}", error=True)
                return
            if not responses or responses[0].error or responses[0].content is None:
                self.state.add_system("Selected skill is no longer readable. Remove it or select another skill.", error=True)
                return
        text = self.drafts.pasted.expand(displayed)
        text = self.drafts.skill.expand(text)
        if self.drafts.pasted.has_invalid_marker(displayed):
            self.state.add_system(
                "A pasted-content marker was edited or truncated. "
                "Restore the marker or re-paste the content before submitting.",
                error=True,
            )
            self.set_status("Edited pasted-content marker")
            return
        if self.dialogs.current is not None:
            was_text = self.dialogs.current.accepts_text
            complete = self.dialogs.current.accept(text)
            if self.dialogs.current.kind == "skill":
                self.buffer.reset()
            elif was_text or complete:
                self._reset_submitted_buffer(text)
            if complete:
                self.interaction_flow()._finish_interaction()
            self.invalidate()
            return
        if not text and not self.state.attachments:
            return
        if self.run_view.compacting:
            self.set_status("Wait for compaction to finish")
            return
        if (not self.drafts.pasted.has_blocks and not self.drafts.skill.is_present(displayed)
                and text.startswith("/") and "\n" not in text):
            self.buffer.reset(append_to_history=True)
            self.lifecycle().spawn(self._dispatch_command(text), stop=TaskStop.CANCEL, name="slash-command")
            return
        pending_interrupt = self.runner.current_interrupt() if not self.state.running else None
        if self.state.running or self.runner.control.pending_message_texts() or pending_interrupt is not None:
            if self.state.attachments:
                self.set_status("Wait for the active run before sending images")
                return
            self._reset_submitted_buffer(text)
            if queue_mode == "followUp":
                self.runner.follow_up(text)
                self.set_status("Follow-up queued")
            else:
                self.runner.steer(text)
                self.set_status("Steering queued")
            if pending_interrupt is not None:
                self.set_status(f"{self.state.status} · F2 pending", transient=False)
            if not self.state.running and (self.run_view.task is None or self.run_view.task.done()):
                self._start_queued_run()
            self.invalidate()
            return
        self._reset_submitted_buffer(text)
        self._start_run(text, image_refs=tuple(self.state.attachments))

    def _reset_submitted_buffer(self, expanded_text: str) -> None:
        if self.drafts.pasted.has_blocks or self.drafts.skill.has_blocks:
            if expanded_text:
                self.buffer.history.append_string(expanded_text)
            self.buffer.reset(append_to_history=False)
        else:
            self.buffer.reset(append_to_history=bool(expanded_text))
        self.drafts.pasted.clear()
        self.drafts.skill.clear()

    async def _dispatch_command(self, text: str) -> None:
        name, _, arg = text[1:].partition(" ")
        command = self.command_by_name.get(name)
        if command is None:
            self.state.add_system(f"Unknown command: /{name}", error=True)
        else:
            await self.execute_command(command, arg.strip())
        self.invalidate()

    def _start_run(
        self,
        text: str,
        *,
        resume_call: Any | None = None,
        image_refs: tuple[ImageAttachmentRef, ...] = (),
    ) -> None:
        # Exit may win the race with a completion callback. Refuse before any
        # visible state change or a new run reservation.
        if self.lifecycle().exiting:
            return
        if resume_call is None:
            self.state.add_user(text, attachments=image_refs)
            self.state.attachments.clear()
        self.state.running = True
        self._working_messages().reset()
        self.set_status("Working…  Esc to cancel", transient=False)
        self.run_view.epoch += 1
        epoch = self.run_view.epoch
        self.run_view.cancel_sent = False
        # Esc may arrive before the worker reaches begin_run; the reservation
        # keeps that cancel instead of letting begin_run clear it.
        reservation = self.runner.reserve_run()

        async def work() -> None:
            try:
                if resume_call is None:
                    try:
                        result = await self._run_blocking(
                            self._call_in_run_epoch, epoch,
                            self.runner.invoke_with_attachment_refs,
                            text,
                            image_refs=image_refs,
                            on_event=self._on_event_thread,
                        )
                    except Exception as exc:  # noqa: BLE001
                        self.state.attachments[:] = image_refs
                        self.run_events()._apply_event(RunEvent(type="run_failed", content=str(exc)))
                        return
                    if result.status == "failed" and image_refs:
                        self.state.attachments[:] = image_refs
                else:
                    try:
                        result = await self._run_blocking(self._call_in_run_epoch, epoch, resume_call)
                    except Exception as exc:  # noqa: BLE001
                        self.run_events()._apply_event(RunEvent(type="run_failed", content=str(exc)))
                        return
                self.run_events()._handle_result(result)
            finally:
                self.runner.release_run_reservation(reservation)
                if epoch == self.run_view.epoch:
                    self.run_view.cancel_sent = False

        self.run_view.task = self.lifecycle().spawn(work(), stop=TaskStop.OBSERVE, name="agent-run")

    def _start_queued_run(self) -> None:
        # Do not pop steering or follow-up after exit: those messages are not
        # consumed, and they are not marked applied.
        if self.lifecycle().exiting:
            return
        if self.dialogs.current is not None or self.runner.current_interrupt() is not None:
            return
        text = self.runner.control.pop_steering()
        if text is None:
            text = self.runner.control.pop_follow_up()
        if text is not None:
            # The applied event records the user message in the transcript.
            self._start_run("", resume_call=partial(
                self.runner.invoke, text, on_event=self._on_event_thread,
            ))
