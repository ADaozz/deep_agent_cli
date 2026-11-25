"""Keyboard bindings for the CLI input workflow."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from prompt_toolkit.filters import Condition, Filter
from prompt_toolkit.key_binding import ConditionalKeyBindings, KeyBindings, merge_key_bindings
from prompt_toolkit.key_binding.key_bindings import KeyBindingsBase
from prompt_toolkit.key_binding.bindings.named_commands import get_by_name
from prompt_toolkit.keys import Keys

from agent.cli.input import Keymap
from agent.cli.input_actions import InputAction
from agent.cli.lifecycle import TaskStop


if TYPE_CHECKING:
    from agent.cli.app import CliApplication


def create_bindings(
    self: CliApplication, clock: Callable[[], float], main_only: Filter
) -> KeyBindingsBase:
    kb = KeyBindings()
    shared = KeyBindings()
    interaction_active = Condition(lambda: self.dialogs.current is not None)
    secret_input_active = Condition(
        lambda: self.dialogs.current is not None and self.dialogs.current.accepts_secret
    )

    def bind(action: str, bindings: KeyBindings = kb):  # type: ignore[no-untyped-def]
        def register(handler):  # type: ignore[no-untyped-def]
            registered = False
            for sequence in self.keymap.sequences(action):
                # Ctrl+A is also beginning-of-line in the editor. Eager wins so
                # the SubAgent result toggle is the key the user asked for.
                eager = action == "tasks_expand"
                try:
                    bindings.add(*sequence, eager=eager)(handler)
                    registered = True
                except (KeyError, ValueError) as exc:
                    self.keymap.warning = f"Invalid keybinding for {action}: {' '.join(sequence)} ({exc})"
            if not registered:
                for sequence in Keymap().sequences(action):
                    try:
                        bindings.add(*sequence, eager=action == "tasks_expand")(handler)
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

    turn_navigation = Condition(lambda: not self.buffer.text and not self.state.attachments)

    @kb.add("left", filter=turn_navigation & ~interaction_active & ~completion_active)
    def previous_turn(event) -> None:  # type: ignore[no-untyped-def]
        self.transcript.jump_turn(-1)

    @kb.add("right", filter=turn_navigation & ~interaction_active & ~completion_active)
    def next_turn(event) -> None:  # type: ignore[no-untyped-def]
        self.transcript.jump_turn(1)

    editing = Condition(lambda: not secret_input_active() and bool(self.buffer.text) and (
        self.dialogs.current is None or self.dialogs.current.accepts_text
    ))

    @kb.add("up", filter=editing)
    def editor_up(event) -> None:  # type: ignore[no-untyped-def]
        self.editor_control.move_vertical(-1, self._editor_width())

    @kb.add("down", filter=editing)
    def editor_down(event) -> None:  # type: ignore[no-untyped-def]
        self.editor_control.move_vertical(1, self._editor_width())

    @kb.add("tab", filter=completion_active)
    def completion_tab(event) -> None:  # type: ignore[no-untyped-def]
        self.run_dispatch._accept_command_completion(event.current_buffer)

    @bind("newline")
    def newline(event) -> None:  # type: ignore[no-untyped-def]
        if secret_input_active():
            return
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
        if secret_input_active():
            sanitized = event.data.replace("\r\n", "").replace("\n", "").replace("\r", "")
            self.secret_buffer.insert_text(sanitized)
            return
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

    @kb.add("up", filter=interaction_active & ~editing)
    def interaction_up(event) -> None:  # type: ignore[no-untyped-def]
        assert self.dialogs.current
        self.dialogs.current.move(-1)
        self.application.invalidate()

    @kb.add("down", filter=interaction_active & ~editing)
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
        self._clear_secret_buffer()
        if self.dialogs.current.custom_entry:
            self.dialogs.current.custom_entry = False
            self.dialogs.current.error = ""
            self.buffer.reset()
            self.drafts.pasted.clear()
            self.drafts.skill.clear()
            return
        if self.dialogs.current.current.get("id") == "__confirm_overwrite__":
            self.dialogs.current.fields = [
                item for item in self.dialogs.current.fields if item.get("id") != "__confirm_overwrite__"
            ]
            self.dialogs.current.index = max(0, len(self.dialogs.current.fields) - 1)
            self.dialogs.current.option_index = 0
            if self.dialogs.current.accepts_secret:
                fid = str(self.dialogs.current.current.get("id") or "")
                self.dialogs.current._secret_values.pop(fid, None)
            self.application.invalidate()
            return
        if self.dialogs.current.index > 0:
            self.dialogs.current.index -= 1
            self.dialogs.current.option_index = 0
            field_id = str(self.dialogs.current.current.get("id") or "")
            if self.dialogs.current.accepts_secret:
                # Never copy a previously entered secret back into any editable buffer.
                self.dialogs.current._secret_values.pop(field_id, None)
                self.buffer.reset()
                self.drafts.pasted.clear()
                self.drafts.skill.clear()
                return
            previous = self.dialogs.current.values.get(field_id, "")
            if self.dialogs.current.accepts_text and isinstance(previous, str):
                self.buffer.text = previous
                self.buffer.cursor_position = len(previous)
            else:
                self.buffer.reset()
                self.drafts.pasted.clear()
                self.drafts.skill.clear()

    @bind("interrupt")
    def escape(event) -> None:  # type: ignore[no-untyped-def]
        if self.dialogs.current is None and self._attached_execution is not None:
            self.detach_process_input()
            return
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

    @bind("clear_or_exit", shared)
    def ctrl_c(event) -> None:  # type: ignore[no-untyped-def]
        buffer = event.current_buffer
        if self.dialogs.current is None and self._attached_execution is not None and buffer is self.buffer:
            if buffer.text:
                buffer.reset(append_to_history=False)
                self.drafts.pasted.clear()
                self.drafts.skill.clear()
                self.application.invalidate()
                return
            _exec_id, mode = self._attached_execution
            if mode == "pty":
                self.send_attached_input(chars="\x03", action="write")
            else:
                self.send_attached_input(action="cancel")
            return
        now = clock()
        action = self._input_actions.decide_clear_or_exit(self._input_view(buffer), now)
        self.apply_input_action(action, buffer)
        if action is InputAction.CLEAR_INPUT:
            self._input_actions.arm_clear(now)

    @bind("exit")
    def ctrl_d(event) -> None:  # type: ignore[no-untyped-def]
        buffer = event.current_buffer
        if self.dialogs.current is None and self._attached_execution is not None and not buffer.text:
            _exec_id, mode = self._attached_execution
            if mode == "pty":
                self.send_attached_input(chars="\x04", action="write")
            else:
                self.send_attached_input(action="close")
            return
        action = self._input_actions.decide_ctrl_d(self._input_view(buffer))
        self.apply_input_action(action, buffer)

    @bind("tools_expand")
    def tools_expand(event) -> None:  # type: ignore[no-untyped-def]
        self.state.tools_expanded = not self.state.tools_expanded
        self.set_status(f"Tool output: {'expanded' if self.state.tools_expanded else 'collapsed'}")

    @bind("tasks_expand")
    def tasks_expand(event) -> None:  # type: ignore[no-untyped-def]
        self.state.tasks_expanded = not self.state.tasks_expanded
        self.set_status(f"SubAgent results: {'expanded' if self.state.tasks_expanded else 'collapsed'}")

    @bind("review_diff")
    def review_diff(event) -> None:  # type: ignore[no-untyped-def]
        self._toggle_file_changes()

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

    return merge_key_bindings([ConditionalKeyBindings(kb, main_only), shared])
