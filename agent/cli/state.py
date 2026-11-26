from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

from agent.sessions.images import ImageAttachmentRef
from agent.runtime.contracts import RunEvent, TurnTiming
from agent.runtime.metrics import normalize_usage
from agent.sessions.transcript import (
    _execution_output,
    is_execution_terminal,
    project_execution_update,
    subagent_card_status,
    task_result_text,
)
from agent.sessions.contracts import TranscriptBlock
from agent.subagents.notifications import is_background_launch, is_task_tool


@dataclass
class MessageBlock:
    kind: str
    content: str = ""
    thinking: str = ""
    id: str = field(default_factory=lambda: str(uuid4()))
    is_error: bool = False
    pending: bool = False
    attachments: tuple[ImageAttachmentRef, ...] = ()
    # Bumped on every mutation so the transcript renderer can tell a frozen
    # block from the one still being streamed into.
    revision: int = 0
    styled_spans: tuple[tuple[int, int, str], ...] = ()
    left_padding: int = 1
    status_marker: str | None = None
    command_title: str | None = None


@dataclass(frozen=True)
class SubtaskView:
    """One nested task row under the card that launched its ancestor."""

    run_id: str
    description: str
    status: str
    elapsed_seconds: float | None = None
    reason: str = ""
    result: str = ""
    children: tuple[SubtaskView, ...] = ()


@dataclass
class SubagentNode:
    """Live task-tree entry. Depth-1 nodes bind a root card by tool_call_id."""

    run_id: str
    parent_run_id: str
    depth: int
    description: str
    status: str = "running"
    tool_call_id: str = ""
    elapsed_seconds: float | None = None
    reason: str = ""
    result: str = ""


@dataclass
class ToolBlock:
    tool_call_id: str
    name: str
    arguments: dict[str, Any]
    output: str = ""
    # Raw ToolMessage.artifact: the structured tool result, preserved for
    # preview builders. Not persisted as a derived preview.
    artifact: Any = None
    status: str = "running"
    is_error: bool = False
    revision: int = 0
    exit_code: int | None = None
    # Nested tasks under this delegation. Empty when the task has no children.
    subtasks: tuple[SubtaskView, ...] = ()


@dataclass(frozen=True)
class TurnSummaryBlock:
    elapsed_seconds: float
    finished_at: datetime


Block = MessageBlock | ToolBlock | TurnSummaryBlock


def touch(block: MessageBlock | ToolBlock) -> MessageBlock | ToolBlock:
    """Mark a block as changed so its cached rendering is rebuilt."""
    block.revision += 1
    return block


def _is_background_execution(block: ToolBlock) -> bool:
    """True once execute has yielded into a live background process."""
    return (
        block.name == "execute"
        and block.status == "running"
        and isinstance(block.artifact, dict)
        and block.artifact.get("running") is True
        and bool(block.artifact.get("execution_id"))
    )


def _apply_execution_projection(
    tool: ToolBlock,
    projected: tuple[str, dict[str, Any], str, bool, int | None] | None,
) -> bool:
    if projected is None:
        return False
    tool.output, tool.artifact, tool.status, tool.is_error, tool.exit_code = projected
    return True


@dataclass
class CliState:
    blocks: list[Block] = field(default_factory=list)
    running: bool = False
    compacting: bool = False
    status: str = "Ready"
    thinking_collapsed: bool = True
    tools_expanded: bool = False
    # SubAgent results fold on their own. Ctrl+O still expands other tools.
    tasks_expanded: bool = False
    # File diffs expand in place. Ctrl+R toggles this; Ctrl+O also shows the full body.
    diffs_expanded: bool = False
    messages_expanded: bool = False
    # Held by reference, not index: app-side filtering of `blocks` would
    # otherwise silently redirect stream deltas into an unrelated block.
    active_block: MessageBlock | None = None
    pending_user_ids: dict[str, str] = field(default_factory=dict)
    attachments: list[ImageAttachmentRef] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    model_metrics_history: list[dict[str, Any]] = field(default_factory=list)
    # Separate a never-started conversation from missing model usage.
    # Clearing the visible transcript must not reset this session property.
    context_pristine: bool = True
    # Live task tree. Root cards stay in `blocks`; deeper tasks hang off them.
    subagent_nodes: dict[str, SubagentNode] = field(default_factory=dict)
    # Active session thread_id; guards against background execution deltas from other sessions.
    session_id: str = ""

    @property
    def subagent_count(self) -> int:
        """Running task cards plus nested tasks that are actually executing.

        Scheduler admission counts also include HTTP streams that are only
        closing. Those slots are not running work, so they do not add to this
        number. A finished child drops out immediately; its parent card stays
        running until that parent’s own tool call ends.
        """
        cards = sum(
            isinstance(block, ToolBlock) and is_task_tool(block.name) and block.status == "running"
            for block in self.blocks
        )
        nested = sum(node.depth >= 2 and node.status == "running" for node in self.subagent_nodes.values())
        return cards + nested

    def add_user(
        self,
        text: str,
        *,
        pending: bool = False,
        message_id: str = "",
        attachments: tuple[ImageAttachmentRef, ...] = (),
    ) -> None:
        block = MessageBlock(
            kind="user", content=text, pending=pending,
            id=message_id or str(uuid4()), attachments=attachments,
        )
        self.blocks.append(block)
        self.active_block = None
        if pending and message_id:
            self.pending_user_ids[message_id] = block.id

    def add_system(
        self, text: str, *, error: bool = False,
        styled_spans: tuple[tuple[int, int, str], ...] = (),
        left_padding: int = 1,
        status_marker: str | None = None,
    ) -> None:
        self.blocks.append(MessageBlock(
            kind="error" if error else "system", content=text,
            is_error=error, styled_spans=styled_spans, left_padding=left_padding,
            status_marker=status_marker,
        ))

    def add_command_panel(
        self, title: str, body: str, *,
        styled_spans: tuple[tuple[int, int, str], ...] = (),
    ) -> None:
        self.blocks.append(MessageBlock(
            kind="system", content=body, styled_spans=styled_spans, command_title=title,
        ))

    def clear(self, *, preserve_model_metrics: bool = False) -> None:
        self.blocks.clear()
        self.active_block = None
        self.pending_user_ids.clear()
        self.usage.clear()
        self.subagent_nodes.clear()
        if not preserve_model_metrics:
            self.model_metrics_history.clear()

    def load_model_metrics(self, history: list[dict[str, Any]]) -> None:
        self.model_metrics_history = [dict(metrics) for metrics in history]

    def load_transcript(self, blocks: list[TranscriptBlock]) -> None:
        self.clear()
        self.context_pristine = not bool(blocks)
        for item in blocks:
            if item.kind == "turn_summary" and item.finished_at is not None:
                self.blocks.append(TurnSummaryBlock(
                    elapsed_seconds=item.elapsed_seconds, finished_at=item.finished_at,
                ))
            elif item.kind == "user":
                self.blocks.append(MessageBlock(
                    kind="user", content=item.content, attachments=item.attachments,
                ))
            elif item.kind == "assistant":
                self.blocks.append(MessageBlock(
                    kind="assistant", content=item.content, thinking=item.thinking,
                ))
            elif item.kind == "tool":
                self.blocks.append(ToolBlock(
                    tool_call_id=item.tool_call_id,
                    name=item.name or "tool",
                    arguments=item.arguments or {},
                    output=(
                        task_result_text(artifact=item.artifact, content=item.content, status=item.status or "")
                        if item.name == "start_task" else item.content
                    ),
                    artifact=item.artifact,
                    status=item.status or ("error" if item.is_error else "completed"),
                    is_error=item.is_error,
                    exit_code=item.exit_code,
                ))

    def apply(self, event: RunEvent) -> None:
        if event.type == "subagents_changed":
            # Runtime accounting remains available to other event consumers.
            # Presentation counts follow task card transitions instead.
            return
        if event.type == "subagent_update" and isinstance(event.result, dict):
            self._apply_subagent_update(event.result)
            return
        if event.type in {"run_started", "run_completed", "run_failed", "run_cancelled", "interaction_requested"}:
            self.compacting = False
        if event.type == "run_started":
            self.context_pristine = False
            self.running = True
            self.status = "Working…  Esc to cancel"
            self.active_block = None
        elif event.type == "compaction_started":
            self.compacting = True
        elif event.type == "compaction_finished":
            self.compacting = False
        elif event.type == "assistant_started":
            self.active_block = None
        elif event.type == "model_metrics" and isinstance(event.result, dict):
            self.model_metrics_history.append(dict(event.result))
        elif event.type == "usage" and isinstance(event.result, dict):
            usage = normalize_usage(event.result)
            # Keep the last reported context size until a new input count arrives.
            # Empty/start events and output-only usage cannot replace that reading.
            if "input_tokens" in usage:
                self.usage = usage
        elif event.type in {"thinking_delta", "assistant_delta"}:
            block = touch(self._assistant_block())
            if event.type == "thinking_delta":
                block.thinking = event.content
            else:
                block.content = event.content
        elif event.type == "tool_started":
            # write_stdin updates the owning execute card; it is not shown as its own card.
            if event.name == "write_stdin":
                self.active_block = None
                return
            tool = self._tool_by_id(event.tool_call_id)
            if tool is None:
                self.blocks.append(ToolBlock(
                    tool_call_id=event.tool_call_id,
                    name=event.name,
                    arguments=event.arguments,
                ))
            elif tool.status not in {"completed", "error"}:
                touch(tool)
                tool.name = event.name or tool.name
                tool.arguments = event.arguments or tool.arguments
                tool.status = "running"
            if tool is not None and is_task_tool(tool.name):
                self._paint_task_card(tool)
            elif is_task_tool(event.name):
                created = self._tool_by_id(event.tool_call_id)
                if created is not None:
                    self._paint_task_card(created)
            self.active_block = None
        elif event.type == "tool_output_delta":
            if event.name == "write_stdin":
                return
            payload = event.result if isinstance(event.result, dict) else {}
            event_thread = str(payload.get("thread_id") or "")
            if self.session_id and event_thread and event_thread != self.session_id:
                return
            execution_id = str(payload.get("execution_id") or "")
            tool = self._find_execution_tool(event.tool_call_id, execution_id)
            if tool is None:
                return
            touch(tool)
            if event.stream == "execution_finished" and tool.name == "execute":
                _apply_execution_projection(
                    tool,
                    project_execution_update(
                        current_output=tool.output,
                        current_artifact=tool.artifact,
                        current_status=tool.status,
                        update_artifact={**payload, "running": False},
                        output_delta=event.content,
                        output_mode="append",
                    ),
                )
            elif event.stream == "tail_snapshot" and tool.name == "execute":
                tool.output = event.content
                artifact = tool.artifact if isinstance(tool.artifact, dict) else {}
                tool.artifact = {**artifact, "truncated": True}
            else:
                tool.output += event.content
        elif event.type == "assistant_completed":
            self.active_block = None
        elif event.type == "tool_completed":
            if event.name == "write_stdin":
                # Keep the underlying ToolMessage, but never surface a separate card.
                return
            tool = self._tool(event.tool_call_id)
            if tool is None:
                tool = ToolBlock(event.tool_call_id, event.name, {})
                self.blocks.append(tool)
            touch(tool)
            if is_background_launch(tool.name, tool.arguments):
                # A running launch leaves the card open. The task's own event ends it.
                # A finish that already arrived is not reopened by the launch result.
                if tool.status not in {"running", "waiting"}:
                    return
                card = subagent_card_status(is_error=event.is_error, artifact=event.artifact)
                if event.artifact is not None:
                    tool.artifact = event.artifact
                if card == "running" and not event.is_error:
                    tool.status = "running"
                    tool.is_error = False
                    return
                tool.output = task_result_text(artifact=event.artifact, content=event.content)
                tool.status = card
                tool.is_error = card in {"error", "cancelled"}
                self._finish_task_card(tool)
                return
            completed_artifact = event.artifact if isinstance(event.artifact, dict) else None
            if event.name == "execute" and completed_artifact is not None:
                had_tail_snapshot = (
                    isinstance(tool.artifact, dict)
                    and tool.artifact.get("truncated") is True
                )
                output_mode = (
                    "replace"
                    if (completed_artifact.get("running") is not True and completed_artifact.get("truncated") and not had_tail_snapshot)
                    else "replace_if_empty"
                )
                _apply_execution_projection(
                    tool,
                    project_execution_update(
                        current_output=tool.output,
                        current_artifact=tool.artifact,
                        current_status=tool.status,
                        update_artifact=completed_artifact,
                        output_delta=_execution_output(completed_artifact),
                        fallback_output=event.content,
                        output_mode=output_mode,
                        event_is_error=event.is_error,
                    ),
                )
                return
            if completed_artifact is not None:
                tool.artifact = completed_artifact
            if event.content and (not tool.output or event.content.startswith(tool.output)):
                tool.output = event.content
            tool.is_error = event.is_error
            tool.status = "error" if event.is_error else "completed"
            if is_task_tool(tool.name):
                tool.output = task_result_text(
                    artifact=event.artifact, content=event.content, status="",
                )
                tool.status = subagent_card_status(is_error=event.is_error, artifact=event.artifact)
                tool.is_error = tool.status in {"error", "cancelled"}
                self._finish_task_card(tool)
        elif event.type == "subagent_finished" and isinstance(event.result, dict):
            tool = self._tool_by_id(str(event.result.get("tool_call_id") or ""))
            if tool is not None and is_background_launch(tool.name, tool.arguments):
                touch(tool)
                recorded = event.result.get("status")
                tool.output = task_result_text(content=event.content, status=recorded if isinstance(recorded, str) else "")
                tool.status = subagent_card_status(status=recorded if isinstance(recorded, str) else None)
                tool.is_error = tool.status in {"error", "cancelled"}
                self._finish_task_card(tool)
        elif event.type == "steering_queued":
            pass  # Pending input is rendered separately until it is applied.
        elif event.type == "steering_applied":
            message_id = ""
            if isinstance(event.result, dict):
                message_id = str(event.result.get("id") or "")
            block_id = self.pending_user_ids.pop(message_id, None)
            if block_id:
                for block in self.blocks:
                    if isinstance(block, MessageBlock) and block.id == block_id:
                        touch(block).pending = False
                        break
            elif event.content:
                self.add_user(event.content, message_id=message_id)
        elif event.type == "run_cancelling":
            self.status = "Cancelling…"
        elif event.type == "interaction_requested":
            self.running = False
            self.status = "Waiting for input"
            self.active_block = None
            settle = _interaction_settlement(event.result)
            for block in self.blocks:
                if (
                    isinstance(block, ToolBlock)
                    and block.status == "running"
                    and not is_background_launch(block.name, block.arguments)
                    and not _is_background_execution(block)
                ):
                    touch(block).status = settle(block)
        elif event.type == "run_completed":
            self.running = False
            self.status = "Ready"
            for block in self.blocks:
                # Background subagents and yielded executions outlive the turn that started them.
                if (
                    isinstance(block, ToolBlock)
                    and block.status in {"running", "waiting"}
                    and not is_background_launch(block.name, block.arguments)
                    and not _is_background_execution(block)
                ):
                    touch(block).status = "interrupted"
                    if is_task_tool(block.name):
                        self._finish_task_card(block)
            if event.content and not self._has_assistant_text(event.content):
                self.blocks.append(MessageBlock(kind="assistant", content=event.content))
            self.active_block = None
        elif event.type == "turn_completed" and isinstance(event.result, TurnTiming):
            if event.content and not self._has_assistant_text(event.content):
                self.blocks.append(MessageBlock(kind="assistant", content=event.content))
            self.blocks.append(TurnSummaryBlock(
                elapsed_seconds=event.result.elapsed_seconds,
                finished_at=event.result.finished_at,
            ))
            self.active_block = None
        elif event.type == "run_cancelled":
            self.running = False
            self.status = "Cancelled"
            for block in self.blocks:
                if (
                    isinstance(block, ToolBlock)
                    and block.status == "running"
                    and not _is_background_execution(block)
                ):
                    touch(block)
                    if is_task_tool(block.name):
                        block.status = "cancelled"
                        block.is_error = True
                        self._finish_task_card(block)
                        continue
                    block.status = "error"
                    block.is_error = True
                    if block.name == "execute":
                        artifact = block.artifact if isinstance(block.artifact, dict) else {}
                        block.artifact = {**artifact, "termination_reason": "cancelled"}
        elif event.type == "run_failed":
            self.running = False
            self.status = "Failed"
            for block in self.blocks:
                if (
                    isinstance(block, ToolBlock)
                    and block.status in {"running", "waiting"}
                    and not is_background_launch(block.name, block.arguments)
                    and not _is_background_execution(block)
                ):
                    touch(block).status = "error"
                    block.is_error = True
                    if block.name == "start_task":
                        self._finish_task_card(block)
            self.add_system(event.content or "Unknown error", error=True)

    def _apply_subagent_update(self, payload: dict[str, Any]) -> None:
        node = self._upsert_task_node(payload)
        if node is not None:
            self._refresh_owner(node)

    def _upsert_task_node(self, fields: dict[str, Any]) -> SubagentNode | None:
        """Insert or update one task node. Live events and restored logs share this."""
        run_id = str(fields.get("run_id") or "")
        if not run_id:
            return None
        status = str(fields.get("status") or "")
        if status not in {"running", "completed", "failed", "cancelled", "unconfirmed"}:
            return None
        node = self.subagent_nodes.get(run_id)
        if node is not None and node.status in {"completed", "failed", "cancelled"} and status in {"running", "unconfirmed"}:
            return None
        depth = fields.get("depth")
        depth = depth if type(depth) is int and not isinstance(depth, bool) and depth > 0 else 1
        elapsed = fields.get("elapsed_seconds")
        elapsed_seconds = (
            float(elapsed) if type(elapsed) in {int, float} and not isinstance(elapsed, bool) and elapsed >= 0
            else None
        )
        description = str(fields.get("description") or "")
        if node is None:
            node = SubagentNode(
                run_id=run_id,
                parent_run_id=str(fields.get("parent_run_id") or ""),
                depth=depth,
                description=description,
            )
            self.subagent_nodes[run_id] = node
        node.parent_run_id = str(fields.get("parent_run_id") or node.parent_run_id)
        node.depth = depth
        if description:
            node.description = description
        node.status = status
        tool_call_id = str(fields.get("tool_call_id") or "")
        if tool_call_id:
            node.tool_call_id = tool_call_id
        node.elapsed_seconds = elapsed_seconds
        node.reason = str(fields.get("reason") or "") if status in {"failed", "cancelled"} else ""
        if fields.get("summary_truncated") and isinstance(fields.get("result_summary"), str):
            if not node.result:
                node.result = fields["result_summary"]
        else:
            result_text = fields.get("result")
            if isinstance(result_text, str) and status in {"completed", "failed", "cancelled", "unconfirmed"}:
                node.result = result_text
        return node

    def _finish_task_card(self, tool: ToolBlock) -> None:
        """Refresh this card from the nodes. A parent status does not rewrite children."""
        self._paint_task_card(tool)

    def restore_task_tree(self, store: Any, root_id: str) -> None:
        """Rebuild the task tree from persisted start and finish records."""
        self.subagent_nodes.clear()
        summaries: set[str] = set()

        def absorb(parent_id: str) -> None:
            records = []
            for run_id in store.list_runs(parent_id):
                if run_id in self.subagent_nodes:
                    continue
                meta = store.task_metadata(parent_id, run_id)
                if meta is not None:
                    records.append(meta)
            records.sort(key=lambda item: (str(item.get("started_at") or ""), str(item.get("run_id") or "")))
            for meta in records:
                status = str(meta.get("status") or "unconfirmed")
                if status not in {"completed", "failed", "cancelled", "unconfirmed"}:
                    status = "unconfirmed"
                if meta.get("summary_truncated"):
                    summaries.add(str(meta.get("run_id") or ""))
                self._upsert_task_node({**meta, "status": status})
                absorb(str(meta.get("run_id") or ""))

        absorb(root_id)
        confirmed = {"completed": "completed", "error": "failed", "cancelled": "cancelled"}
        for block in self.blocks:
            if not isinstance(block, ToolBlock) or not is_task_tool(block.name):
                continue
            node = self._node_for_tool(block.tool_call_id)
            if node is not None:
                # A log that never finished must not replace a checkpoint terminal
                # state, and a truncated log summary must not replace its result.
                if node.status == "unconfirmed" and block.status in confirmed:
                    node.status = confirmed[block.status]
                else:
                    mapped = subagent_card_status(status=node.status)
                    block.status = mapped
                    block.is_error = mapped in {"error", "cancelled"}
                if node.run_id in summaries and block.output:
                    node.result = block.output
                elif node.result:
                    block.output = node.result
            self._paint_task_card(block)

    def _refresh_owner(self, node: SubagentNode) -> None:
        tool = self._tool_by_id(self._owner_tool_id(node))
        if tool is not None and is_task_tool(tool.name):
            self._paint_task_card(tool)

    def _owner_tool_id(self, node: SubagentNode) -> str:
        seen = {node.run_id}
        current = node
        while current.parent_run_id and current.parent_run_id not in seen:
            parent = self.subagent_nodes.get(current.parent_run_id)
            if parent is None:
                break
            seen.add(parent.run_id)
            current = parent
        return current.tool_call_id

    def _node_for_tool(self, tool_call_id: str) -> SubagentNode | None:
        if not tool_call_id:
            return None
        for node in self.subagent_nodes.values():
            if node.depth <= 1 and node.tool_call_id == tool_call_id:
                return node
        return None

    def _views_under(self, parent_run_id: str) -> tuple[SubtaskView, ...]:
        children = [item for item in self.subagent_nodes.values() if item.parent_run_id == parent_run_id]
        return tuple(
            SubtaskView(
                run_id=item.run_id,
                description=item.description or "Delegated task",
                status=item.status,
                elapsed_seconds=item.elapsed_seconds,
                reason=item.reason,
                result=item.result,
                children=self._views_under(item.run_id),
            )
            for item in children
        )

    def _paint_task_card(self, tool: ToolBlock) -> None:
        node = self._node_for_tool(tool.tool_call_id)
        if node is None:
            if tool.subtasks:
                tool.subtasks = ()
                touch(tool)
            return
        tool.subtasks = self._views_under(node.run_id)
        if node.result and node.status in {"completed", "failed", "cancelled", "unconfirmed"}:
            tool.output = node.result
        touch(tool)

    def _assistant_block(self) -> MessageBlock:
        block = self.active_block
        if block is not None and any(item is block for item in self.blocks):
            return block
        block = MessageBlock(kind="assistant")
        self.blocks.append(block)
        self.active_block = block
        return block

    def restore_executions(
        self,
        snapshots: dict[str, dict[str, Any]] | None,
        thread_id: str,
    ) -> None:
        """Reconcile restored execute cards against owned execution snapshots."""
        self.session_id = thread_id
        for block in self.blocks:
            if not isinstance(block, ToolBlock) or block.name != "execute":
                continue
            artifact = block.artifact if isinstance(block.artifact, dict) else None
            if artifact is None:
                continue
            execution_id = str(artifact.get("execution_id") or "")
            if not execution_id:
                continue
            snap = snapshots.get(execution_id) if isinstance(snapshots, dict) else None
            if snap is not None:
                _apply_execution_projection(
                    block,
                    project_execution_update(
                        current_output=block.output,
                        current_artifact=block.artifact,
                        current_status=block.status,
                        update_artifact={**snap["artifact"], "running": bool(snap["running"])},
                        output_delta=snap["output"],
                        output_mode="replace",
                    ),
                )
                touch(block)
                continue
            if is_execution_terminal(artifact, block.status):
                continue
            if block.status == "running" or artifact.get("running") is True:
                # Execution no longer exists (e.g. after CLI restart): never remain Running.
                block.artifact = {**artifact, "running": False}
                block.status = "interrupted"
                block.is_error = False
                touch(block)

    def _find_execution_tool(self, tool_call_id: str, execution_id: str = "") -> ToolBlock | None:
        """Locate an execute/tool card by exact tool_call_id or execution_id."""
        if tool_call_id:
            found = self._tool_by_id(tool_call_id)
            if found is not None:
                existing_id = (
                    str(found.artifact.get("execution_id") or "")
                    if isinstance(found.artifact, dict) else ""
                )
                if not execution_id or not existing_id or existing_id == execution_id:
                    return found
        if execution_id:
            for block in reversed(self.blocks):
                if (
                    isinstance(block, ToolBlock)
                    and block.name == "execute"
                    and isinstance(block.artifact, dict)
                    and str(block.artifact.get("execution_id") or "") == execution_id
                ):
                    return block
        if not tool_call_id and not execution_id:
            return self._running_tool()
        return None

    def _tool(self, tool_call_id: str) -> ToolBlock | None:
        if not tool_call_id:
            return self._running_tool()
        for block in reversed(self.blocks):
            if isinstance(block, ToolBlock) and block.tool_call_id == tool_call_id:
                return block
        return None

    def _tool_by_id(self, tool_call_id: str) -> ToolBlock | None:
        """Exact identity lookup; never falls back to an unrelated running tool."""
        if not tool_call_id:
            return None
        for block in reversed(self.blocks):
            if isinstance(block, ToolBlock) and block.tool_call_id == tool_call_id:
                return block
        return None

    def _running_tool(self) -> ToolBlock | None:
        for block in reversed(self.blocks):
            if isinstance(block, ToolBlock) and block.status == "running":
                return block
        return None

    def _has_assistant_text(self, text: str) -> bool:
        return any(
            isinstance(block, MessageBlock) and block.kind == "assistant" and block.content == text
            for block in self.blocks
        )


def _interaction_settlement(result: Any) -> Callable[[ToolBlock], str]:
    """Classify running tools at an interrupt as waiting or interrupted.

    Only the calls the interrupt actually blocks keep the ``waiting`` label;
    every other unfinished call merely lost its completion confirmation.
    """
    status = str(getattr(result, "status", "") or "")
    if status == "waiting_confirmation":
        pending = {
            str(call.get("toolCallId") or "")
            for call in getattr(result, "pending_tool_calls", None) or []
            if isinstance(call, dict)
        }
        return lambda block: "waiting" if block.tool_call_id in pending else "interrupted"
    if status == "waiting_human":
        return lambda block: "waiting" if block.name == "request_human_input" else "interrupted"
    return lambda _block: "interrupted"
