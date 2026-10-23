"""MCP operation identity stays on MCPController, including forced restarts."""
from agent.cli.mcp_controller import MCPController


def test_only_the_current_operation_clears_busy() -> None:
    controller = MCPController()
    first = controller.try_begin()
    assert first == 1 and controller.busy
    assert controller.try_begin() is None
    second = controller.try_begin(force=True)
    assert second == 2 and controller.busy
    controller.finish(1)
    assert controller.busy
    controller.finish(2)
    assert not controller.busy
    assert controller.owns(2)
    assert not controller.owns(1)
