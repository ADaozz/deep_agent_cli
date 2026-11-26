"""Model-visible subagent schemas."""

import pytest
from pydantic import ValidationError

from agent.tools.catalog import (
    ASYNC_TOOLS,
    COMMON_TOOLS,
    ROOT_ONLY_TOOLS,
    ROOT_TASK_TOOLS,
    ROOT_TOOLS,
    SYNC_TASK_TOOLS,
    SYNC_TOOLS,
    apply_runtime_limits,
    node_tools,
)
from agent.tools.subagent_tasks import StartTaskSchema, StartTaskSyncSchema


def test_start_task_description_points_at_read_task_for_capacity():
    from agent.tools.subagent_tasks import start_task_description

    for background in (True, False):
        text = start_task_description(allow_background=background)
        assert "Call `read_task()` to read the current subagent capacity." in text
        assert "wait_task" not in text
        assert "Maximum concurrent" not in text
        assert "Remaining delegation depth" not in text
    background = start_task_description(allow_background=True)
    assert "start a new turn if the parent is idle" in background
    assert "Do not poll or wait for background tasks." in background
    assert "Both modes can run concurrently" in background


def test_node_sets_are_common_tools_plus_the_node_specific_ones():
    assert ROOT_TOOLS == COMMON_TOOLS | ROOT_ONLY_TOOLS | ROOT_TASK_TOOLS
    assert SYNC_TOOLS == COMMON_TOOLS | SYNC_TASK_TOOLS
    assert ASYNC_TOOLS == COMMON_TOOLS
    assert node_tools("max_depth") == COMMON_TOOLS
    assert "request_human_input" not in SYNC_TOOLS | ASYNC_TOOLS
    assert SYNC_TASK_TOOLS < ROOT_TASK_TOOLS
    ask = apply_runtime_limits(ROOT_TOOLS, subagents_enabled=True, can_launch=False)
    assert "start_task" not in ask
    assert {"read_task", "cancel_task", "request_human_input"} <= ask
    disabled = apply_runtime_limits(ROOT_TOOLS, subagents_enabled=False, can_launch=True)
    assert not ROOT_TASK_TOOLS & disabled
    assert "ls" in disabled and "request_human_input" in disabled
    hidden = apply_runtime_limits(SYNC_TOOLS, subagents_enabled=True, can_launch=True, hidden={"read_task", "ls"})
    assert {"read_task", "ls"}.isdisjoint(hidden)
    assert "start_task" in hidden


def test_start_schemas_reject_undeclared_arguments():
    StartTaskSchema.model_validate({"description": "do the work", "runtime": object()})
    with pytest.raises(ValidationError, match="subagent_type"):
        StartTaskSchema.model_validate({
            "description": "do the work", "subagent_type": "general-purpose",
        })
    StartTaskSyncSchema.model_validate({"description": "do the work", "runtime": object()})
    with pytest.raises(ValidationError, match="run_in_background"):
        StartTaskSyncSchema.model_validate({"description": "do the work", "run_in_background": True})
