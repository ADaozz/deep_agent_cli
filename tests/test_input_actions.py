"""InputActionRouter is the only place that orders interrupt and exit keys."""
from agent.cli.input_actions import EXIT_WINDOW_SECONDS, InputAction, InputActionRouter, InputView


def test_interrupt_checks_follow_one_priority() -> None:
    router = InputActionRouter()
    busy = dict(transcript_away=True, session_waiting=True, reviewing=True, running=True, mcp_busy=True)
    assert router.decide_interrupt(InputView(completion_open=True, slash_line=True, **busy)) is InputAction.DISMISS_COMPLETION
    assert router.decide_interrupt(InputView(slash_line=True, **busy)) is InputAction.CLEAR_SLASH_LINE
    assert router.decide_interrupt(InputView(**busy)) is InputAction.FOLLOW_TRANSCRIPT
    assert router.decide_interrupt(InputView(has_interaction=True, completion_open=True, transcript_away=True)) is InputAction.FOLLOW_TRANSCRIPT
    assert router.decide_interrupt(InputView(has_interaction=True, completion_open=True)) is InputAction.CANCEL_COMPLETION
    assert router.decide_interrupt(InputView(session_waiting=True, reviewing=True, running=True)) is InputAction.CANCEL_SESSION_WAIT
    assert router.decide_interrupt(InputView(reviewing=True, has_interaction=True, running=True)) is InputAction.CLOSE_REVIEW
    assert router.decide_interrupt(InputView(has_interaction=True, custom_entry=True)) is InputAction.EXIT_CUSTOM_ENTRY
    assert router.decide_interrupt(InputView(has_interaction=True)) is InputAction.CANCEL_INTERACTION
    assert router.decide_interrupt(InputView(compacting=True, running=True)) is InputAction.WAIT_FOR_COMPACTION
    assert router.decide_interrupt(InputView(running=True, cancel_sent=True, mcp_busy=True)) is InputAction.CANCEL_ALREADY_SENT
    assert router.decide_interrupt(InputView(running=True, mcp_busy=True)) is InputAction.CANCEL_RUN
    assert router.decide_interrupt(InputView(mcp_busy=True)) is InputAction.MCP_STILL_RUNNING
    assert router.decide_interrupt(InputView()) is InputAction.NONE


def test_escape_during_exit_still_reaches_a_running_cancel() -> None:
    """exit() does not mark cancel_sent, so Esc can request cancel again."""
    router = InputActionRouter()
    assert router.decide_interrupt(InputView(exiting=True, running=True)) is InputAction.CANCEL_RUN


def test_ctrl_c_window_is_armed_only_by_the_router() -> None:
    router = InputActionRouter()
    assert router.decide_clear_or_exit(InputView(has_interaction=True), 0.0) is InputAction.CANCEL_INTERACTION
    assert router.last_ctrl_c is None
    assert router.decide_clear_or_exit(InputView(), 0.0) is InputAction.CLEAR_INPUT
    router.arm_clear(0.0)
    assert router.decide_clear_or_exit(InputView(), EXIT_WINDOW_SECONDS - 0.001) is InputAction.REQUEST_EXIT
    assert router.decide_clear_or_exit(InputView(has_interaction=True), 0.1) is InputAction.CANCEL_INTERACTION
    assert router.last_ctrl_c == 0.0
    assert router.decide_clear_or_exit(InputView(), EXIT_WINDOW_SECONDS) is InputAction.CLEAR_INPUT


def test_ctrl_d_and_exit_command_share_the_exit_action() -> None:
    router = InputActionRouter()
    assert router.decide_ctrl_d(InputView(buffer_empty=True, has_interaction=True)) is InputAction.REQUEST_EXIT
    assert router.decide_ctrl_d(InputView(buffer_empty=False)) is InputAction.DELETE_AFTER_CURSOR
    assert router.decide_exit_command() is InputAction.REQUEST_EXIT
