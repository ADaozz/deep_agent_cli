"""Keyboard bindings for the CLI input workflow."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.named_commands import get_by_name
from prompt_toolkit.keys import Keys

from agent.cli.input import Keymap
from agent.cli.input_actions import InputAction
from agent.cli.lifecycle import TaskStop


if TYPE_CHECKING:
    from agent.cli.app import CliApplication


def create_bindings(self: CliApplication, clock: Callable[[], float]) -> KeyBindings:
    kb = KeyBindings()
    interaction_active = Condition(lambda: self.dialogs.current is not None)

    def bind(action: str):  # type: ignore[no-untyped-def]
        def register(handler):  # type: ignore[no-untyped-def]
            registered = False
            for sequence in self.keymap.sequences(action):
                try:
                    kb.add(*sequence)(handler)
                    registered = True
                except (KeyError, ValueError) as exc:
                    self.keymap.warning = f"Invalid keybinding for {action}: {' '.join(sequence)} ({exc})"
            if not registered:
                for sequence in Keymap().sequences(action):
                    try:
                        kb.add(*sequence)(handler)
                    except (KeyError, ValueError):
                        continue
            return handler
        return register

    @bind("submit")
    def submit(event) -> None:  # type: ignore[no-untyped-def]
        if self.dialogs.current is None and event.current_buffer.text.strip() == "/skill":
            event.current_buffer.cancel_completion()
            self.run_dispatch._submit_buffer("steer")
            return
        if self.run_dispatch._accept_command_completion(event.current_buffer):
            return
        self.run_dispatch.submit_enter(clock())

    completion_active = Condition(lambda: self.dialogs.current is None and self.buffer.complete_state is not None)

    @kb.add("up", filter=completion_active)
    def completion_up(event) -> None:  # type: ignore[no-untyped-def]
        event.current_buffer.complete_previous()

    @kb.add("down", filter=completion_active)
    def completion_down(event) -> None:  # type: ignore[no-untyped-def]
        event.current_buffer.complete_next()

    @kb.add("tab", filter=completion_active)
    def completion_tab(event) -> None:  # type: ignore[no-untyped-def]
        self.run_dispatch._accept_command_completion(event.current_buffer)

    @bind("newline")
    def newline(event) -> None:  # type: ignore[no-untyped-def]
        event.current_buffer.insert_text("\n")

    @bind("follow_up")
    def follow_up(event) -> None:  # type: ignore[no-untyped-def]
        self.run_dispatch._submit_buffer("followUp")

    @bind("image_paste")
    def clipboard_paste(event) -> None:  # type: ignore[no-untyped-def]
        if self.dialogs.current is not None:
            return
        self.set_status("Reading clipboard…", transient=False)
        self.lifecycle.spawn(self.attachments_controller._paste_clipboard(), stop=TaskStop.CANCEL, name="clipboard-paste")

    @kb.add(Keys.BracketedPaste, eager=True)
    def bracketed_paste(event) -> None:  # type: ignore[no-untyped-def]
        self.attachments_controller._handle_pasted_text(event.data.replace("\r\n", "\n").replace("\r", "\n"))

    @kb.add("backspace", filter=Condition(
        lambda: self.dialogs.current is None and self.buffer.text.startswith("/")
        and "\n" not in self.buffer.text
    ))
    def command_backspace(event) -> None:  # type: ignore[no-untyped-def]
        """退格修改命令后重新补全，同时解除已确认候选的抑制状态。"""
        get_by_name("backward-delete-char").handler(event)
        self.slash_completer.accepted_text = None
        if event.current_buffer.text.startswith("/"):
            event.current_buffer.start_completion()

    @kb.add("backspace", filter=Condition(
        lambda: self.dialogs.current is None and not self.buffer.text and bool(self.state.attachments)
    ))
    def remove_last_attachment(event) -> None:  # type: ignore[no-untyped-def]
        removed = self.state.attachments.pop()
        self.set_status(f"Removed image: {removed.filename}")

    @kb.add("up", filter=interaction_active)
    def interaction_up(event) -> None:  # type: ignore[no-untyped-def]
        assert self.dialogs.current
        self.dialogs.current.move(-1)
        self.application.invalidate()

    @kb.add("down", filter=interaction_active)
    def interaction_down(event) -> None:  # type: ignore[no-untyped-def]
        assert self.dialogs.current
        self.dialogs.current.move(1)
        self.application.invalidate()

    @kb.add("pageup")
    def page_up(event) -> None:  # type: ignore[no-untyped-def]
        self._scroll_queue(-10) if self._queue_expanded() else self.transcript.scroll_transcript(-10)

    @kb.add("pagedown")
    def page_down(event) -> None:  # type: ignore[no-untyped-def]
        self._scroll_queue(10) if self._queue_expanded() else self.transcript.scroll_transcript(10)

    @kb.add("c-home")
    def scroll_top(event) -> None:  # type: ignore[no-untyped-def]
        self.transcript.scroll_transcript_to(0)

    @kb.add("c-end")
    def scroll_bottom(event) -> None:  # type: ignore[no-untyped-def]
        self.transcript.scroll_transcript_to(self.transcript.transcript_rows())

    @kb.add(" ", filter=interaction_active)
    def interaction_toggle(event) -> None:  # type: ignore[no-untyped-def]
        assert self.dialogs.current
        if self.dialogs.current.current.get("type") == "multi_select" and not self.dialogs.current.accepts_text:
            self.dialogs.current.toggle()
        else:
            event.current_buffer.insert_text(" ")

    @kb.add("tab", filter=interaction_active)
    def interaction_next(event) -> None:  # type: ignore[no-untyped-def]
        self.run_dispatch._submit_buffer("steer")

    @kb.add("s-tab", filter=interaction_active)
    def interaction_previous(event) -> None:  # type: ignore[no-untyped-def]
        assert self.dialogs.current
        if self.dialogs.current.custom_entry:
            self.dialogs.current.custom_entry = False
            self.dialogs.current.error = ""
            event.current_buffer.reset()
            self.drafts.pasted.clear()
            self.drafts.skill.clear()
            return
        if self.dialogs.current.index > 0:
            self.dialogs.current.index -= 1
            self.dialogs.current.option_index = 0
            previous = self.dialogs.current.values.get(str(self.dialogs.current.current.get("id") or ""), "")
            if self.dialogs.current.accepts_text and isinstance(previous, str):
                event.current_buffer.text = previous
                event.current_buffer.cursor_position = len(previous)
            else:
                event.current_buffer.reset()
                self.drafts.pasted.clear()
                self.drafts.skill.clear()

    @bind("interrupt")
    def escape(event) -> None:  # type: ignore[no-untyped-def]
        buffer = event.current_buffer
        action = self._input_actions.decide_interrupt(self._input_view(buffer))
        self.run_view.last_queue_enter = None
        if (action in {InputAction.CANCEL_RUN, InputAction.CANCEL_ALREADY_SENT, InputAction.NONE}
                and self.runner.control.pending_message_texts()):
            self._restore_queued_to_editor(buffer)
            self.run_view.send_queue_after_cancel = False
            self.set_status("Queued messages restored to input")
            self.application.invalidate()
            return
        self.apply_input_action(action, buffer)

    @bind("clear_or_exit")
    def ctrl_c(event) -> None:  # type: ignore[no-untyped-def]
        buffer = event.current_buffer
        now = clock()
        action = self._input_actions.decide_clear_or_exit(self._input_view(buffer), now)
        self.apply_input_action(action, buffer)
        if action is InputAction.CLEAR_INPUT:
            self._input_actions.arm_clear(now)

    @bind("exit")
    def ctrl_d(event) -> None:  # type: ignore[no-untyped-def]
        buffer = event.current_buffer
        action = self._input_actions.decide_ctrl_d(self._input_view(buffer))
        self.apply_input_action(action, buffer)

    @bind("tools_expand")
    def tools_expand(event) -> None:  # type: ignore[no-untyped-def]
        self.state.tools_expanded = not self.state.tools_expanded
        self.set_status(f"Tool output: {'expanded' if self.state.tools_expanded else 'collapsed'}")

    @bind("review_diff")
    def review_diff(event) -> None:  # type: ignore[no-untyped-def]
        self._toggle_review()

    @bind("reopen_interaction")
    def reopen_interaction(event) -> None:  # type: ignore[no-untyped-def]
        self.interaction_flow._reopen_pending_interaction_preserving_allow()

    @bind("thinking_toggle")
    def thinking_toggle(event) -> None:  # type: ignore[no-untyped-def]
        self.state.thinking_collapsed = not self.state.thinking_collapsed
        self.set_status(f"Thinking: {'collapsed' if self.state.thinking_collapsed else 'expanded'}")

    @bind("messages_toggle")
    def messages_toggle(event) -> None:  # type: ignore[no-untyped-def]
        self.state.messages_expanded = not self.state.messages_expanded
        self._queue_scroll = 0
        self.transcript.clear_transcript_selection()
        self.application.invalidate()

    @bind("dequeue")
    def dequeue(event) -> None:  # type: ignore[no-untyped-def]
        restored = self._restore_queued_to_editor(event.current_buffer)
        if not restored:
            self.set_status("No queued messages")
            return
        self.set_status("Queued messages restored")
        self.application.invalidate()

    @bind("model_cycle_forward")
    def model_forward(event) -> None:  # type: ignore[no-untyped-def]
        self.model_selection.cycle_model(delta=1)

    @bind("model_cycle_backward")
    def model_backward(event) -> None:  # type: ignore[no-untyped-def]
        self.model_selection.cycle_model(delta=-1)

    return kb
