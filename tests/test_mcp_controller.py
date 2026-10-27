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


def test_publish_revision_is_not_repeated_until_it_changes() -> None:
    controller = MCPController()
    assert controller.cached_publish(1, dirty=True) is None
    controller.remember_publish(1, "failed", current_revision=1)
    assert controller.cached_publish(1, dirty=True) == "failed"
    controller.remember_publish(1, "applied", current_revision=2)
    assert controller.publish_outcome == "failed"
    controller.remember_publish(2, "applied", current_revision=2)
    assert controller.cached_publish(2, dirty=False) == "applied"
    assert controller.cached_publish(2, dirty=True) is None
    assert controller.cached_publish(3, dirty=False) is None
