from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

from agent.sessions.images import ImageAttachmentRef
from agent.runtime.contracts import RunEvent, TurnTiming
from agent.runtime.metrics import normalize_usage
from agent.sessions.transcript import subagent_card_status
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
                    output=item.content,
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
            tool = self._tool(event.tool_call_id) or self._running_tool()
            if tool is None:
                tool = ToolBlock(event.tool_call_id, event.name or "tool", {})
                self.blocks.append(tool)
            touch(tool)
            if event.stream == "tail_snapshot" and tool.name == "execute":
                tool.output = event.content
                artifact = tool.artifact if isinstance(tool.artifact, dict) else {}
                tool.artifact = {**artifact, "truncated": True}
            else:
                tool.output += event.content
        elif event.type == "assistant_completed":
            self.active_block = None
        elif event.type == "tool_completed":
            tool = self._tool(event.tool_call_id)
            if tool is None:
                tool = ToolBlock(event.tool_call_id, event.name, {})
                self.blocks.append(tool)
            touch(tool)
            if is_background_launch(tool.name, tool.arguments):
                # The launch returned; the delegation itself is still running.
                # Only subagent_finished ends the card, and it may already have.
                refused = subagent_card_status(
                    event.content, is_error=event.is_error, artifact=event.artifact,
                )
                if tool.status == "running" and refused != "completed":
                    tool.output, tool.status, tool.is_error = event.content, refused, True
                    self._finish_task_card(tool)
                return
            completed_artifact = event.artifact if isinstance(event.artifact, dict) else None
            had_tail_snapshot = (
                event.name == "execute" and isinstance(tool.artifact, dict)
                and tool.artifact.get("truncated") is True
            )
            if completed_artifact is not None:
                tool.artifact = completed_artifact
            if event.name == "execute" and completed_artifact is not None:
                if completed_artifact.get("truncated") and not had_tail_snapshot and event.content:
                    # Without streamed output, the final result contains the tail.
                    tool.output = event.content
                elif not tool.output:
                    tool.output = event.content
            elif event.content and (not tool.output or event.content.startswith(tool.output)):
                tool.output = event.content
            tool.is_error = event.is_error
            tool.status = "error" if event.is_error else "completed"
            if is_task_tool(tool.name):
                tool.output = event.content
                tool.status = subagent_card_status(
                    event.content, is_error=event.is_error, artifact=event.artifact,
                )
                tool.is_error = tool.status in {"error", "cancelled"}
                self._finish_task_card(tool)
            if event.name == "execute" and completed_artifact is not None:
                code = completed_artifact.get("exit_code")
                tool.exit_code = code if isinstance(code, int) and not isinstance(code, bool) else None
        elif event.type == "subagent_finished" and isinstance(event.result, dict):
            tool = self._tool_by_id(str(event.result.get("tool_call_id") or ""))
            if tool is not None and is_background_launch(tool.name, tool.arguments):
                touch(tool)
                tool.output = event.content
                recorded = event.result.get("status")
                tool.status = subagent_card_status(
                    event.content, status=recorded if isinstance(recorded, str) else None,
                )
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
                if isinstance(block, ToolBlock) and block.status == "running" and not is_background_launch(block.name, block.arguments):
                    touch(block).status = settle(block)
        elif event.type == "run_completed":
            self.running = False
            self.status = "Ready"
            for block in self.blocks:
                # A background subagent outlives the run that started it.
                if (isinstance(block, ToolBlock) and block.status in {"running", "waiting"}
                        and not is_background_launch(block.name, block.arguments)):
                    touch(block).status = "interrupted"
                    if is_task_tool(block.name):
                        self._finish_task_card(block, fallback="cancelled")
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
                if isinstance(block, ToolBlock) and block.status == "running":
                    touch(block)
                    if is_task_tool(block.name):
                        block.status = "cancelled"
                        block.is_error = True
                        self._finish_task_card(block, fallback="cancelled")
                        continue
                    block.status = "error"
                    block.is_error = True
                    block.exit_code = 1
                    if block.name == "execute":
                        artifact = block.artifact if isinstance(block.artifact, dict) else {}
                        block.artifact = {**artifact, "termination_reason": "cancelled"}
        elif event.type == "run_failed":
            self.running = False
            self.status = "Failed"
            for block in self.blocks:
                if (isinstance(block, ToolBlock) and block.status in {"running", "waiting"}
                        and not is_background_launch(block.name, block.arguments)):
                    touch(block).status = "error"
                    block.is_error = True
                    block.exit_code = 1
                    if block.name == "start_task":
                        self._finish_task_card(block, fallback="failed")
            self.add_system(event.content or "Unknown error", error=True)

    def _apply_subagent_update(self, payload: dict[str, Any]) -> None:
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            return
        status = str(payload.get("status") or "running")
        if status not in {"running", "completed", "failed", "cancelled"}:
            status = "running"
        node = self.subagent_nodes.get(run_id)
        # A child can emit a late progress tick while its finish event is applied.
        # That tick must not reopen a task that has already ended.
        if node is not None and node.status != "running" and status == "running":
            return
        description = " ".join(str(payload.get("description") or "").split())
        depth = payload.get("depth")
        depth = depth if type(depth) is int and not isinstance(depth, bool) and depth > 0 else 1
        elapsed = payload.get("elapsed_seconds")
        elapsed_seconds = (
            float(elapsed) if type(elapsed) in {int, float} and not isinstance(elapsed, bool) and elapsed >= 0
            else None
        )
        if node is None:
            node = SubagentNode(
                run_id=run_id,
                parent_run_id=str(payload.get("parent_run_id") or ""),
                depth=depth,
                description=description or "Delegated task",
            )
            self.subagent_nodes[run_id] = node
        node.parent_run_id = str(payload.get("parent_run_id") or node.parent_run_id)
        node.depth = depth
        if description:
            node.description = description
        node.status = status
        tool_call_id = str(payload.get("tool_call_id") or "")
        if tool_call_id:
            node.tool_call_id = tool_call_id
        node.elapsed_seconds = elapsed_seconds
        node.reason = str(payload.get("reason") or "") if status in {"failed", "cancelled"} else ""
        result_text = payload.get("result")
        if isinstance(result_text, str) and status in {"completed", "failed", "cancelled"}:
            node.result = result_text
        self._refresh_owner(node)

    def _finish_task_card(self, tool: ToolBlock, *, fallback: str = "") -> None:
        """Stop counting this card's tree once the card itself has left `running`.

        Child rows that already finished keep their own status. Only tasks still
        marked running adopt the card's terminal status.
        """
        node = self._node_for_tool(tool.tool_call_id)
        if node is None:
            return
        terminal = {"error": "failed", "interrupted": "cancelled", "cancelled": "cancelled", "completed": "completed"}.get(
            tool.status, fallback or "failed",
        )
        if tool.status == "running":
            terminal = fallback or "cancelled"
        self._close_running(node.run_id, terminal)
        if node.status == "running":
            node.status = terminal
        self._paint_task_card(tool)

    def _close_running(self, run_id: str, status: str) -> None:
        for node in self.subagent_nodes.values():
            if node.parent_run_id == run_id and node.status == "running":
                node.status = status
                self._close_running(node.run_id, status)

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
        touch(tool)

    def _assistant_block(self) -> MessageBlock:
        block = self.active_block
        if block is not None and any(item is block for item in self.blocks):
            return block
        block = MessageBlock(kind="assistant")
        self.blocks.append(block)
        self.active_block = block
        return block

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
