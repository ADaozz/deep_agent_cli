from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from agent.attachments import ImageAttachmentRef
from agent.runner import RunEvent
from agent.session import TranscriptBlock


@dataclass
class MessageBlock:
    kind: str
    content: str = ""
    thinking: str = ""
    id: str = field(default_factory=lambda: str(uuid4()))
    is_error: bool = False
    pending: bool = False
    attachments: tuple[ImageAttachmentRef, ...] = ()


@dataclass
class ToolBlock:
    tool_call_id: str
    name: str
    arguments: dict[str, Any]
    output: str = ""
    status: str = "running"
    is_error: bool = False


Block = MessageBlock | ToolBlock


@dataclass
class CliState:
    blocks: list[Block] = field(default_factory=list)
    running: bool = False
    status: str = "Ready"
    thinking_collapsed: bool = False
    tools_expanded: bool = False
    queued: list[tuple[str, str]] = field(default_factory=list)
    active_assistant: int | None = None
    pending_user_ids: dict[str, str] = field(default_factory=dict)
    attachments: list[ImageAttachmentRef] = field(default_factory=list)

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
        self.active_assistant = None
        if pending and message_id:
            self.pending_user_ids[message_id] = block.id

    def add_system(self, text: str, *, error: bool = False) -> None:
        self.blocks.append(MessageBlock(kind="error" if error else "system", content=text, is_error=error))

    def clear(self) -> None:
        self.blocks.clear()
        self.active_assistant = None
        self.pending_user_ids.clear()

    def load_transcript(self, blocks: list[TranscriptBlock]) -> None:
        self.clear()
        for item in blocks:
            if item.kind == "user":
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
                    status=item.status or ("error" if item.is_error else "completed"),
                    is_error=item.is_error,
                ))

    def apply(self, event: RunEvent) -> None:
        if event.type == "run_started":
            self.running = True
            self.status = "Working…  Esc to cancel"
            self.active_assistant = None
        elif event.type == "assistant_started":
            self.active_assistant = None
        elif event.type in {"thinking_delta", "assistant_delta"}:
            block = self._assistant_block()
            if event.type == "thinking_delta":
                block.thinking = event.content
            else:
                block.content = event.content
        elif event.type == "tool_started":
            self.blocks.append(ToolBlock(
                tool_call_id=event.tool_call_id,
                name=event.name,
                arguments=event.arguments,
            ))
            self.active_assistant = None
        elif event.type == "tool_output_delta":
            tool = self._tool(event.tool_call_id) or self._running_tool()
            if tool is None:
                tool = ToolBlock(event.tool_call_id, event.name or "tool", {})
                self.blocks.append(tool)
            tool.output += event.content
        elif event.type == "assistant_completed":
            self.active_assistant = None
        elif event.type == "tool_completed":
            tool = self._tool(event.tool_call_id)
            if tool is None:
                tool = ToolBlock(event.tool_call_id, event.name, {})
                self.blocks.append(tool)
            if event.content:
                if not tool.output or event.content.startswith(tool.output):
                    tool.output = event.content
                elif tool.output not in event.content:
                    # Keep the live stream; append only a trailing status line when needed.
                    for marker in ("Exit code:", "Cancelled by user."):
                        if marker in event.content and marker not in tool.output:
                            tool.output = f"{tool.output.rstrip()}\n\n{event.content[event.content.rfind(marker):]}"
                            break
            tool.is_error = event.is_error
            tool.status = "error" if event.is_error else "completed"
        elif event.type == "steering_queued":
            mode = ""
            message_id = ""
            if isinstance(event.result, dict):
                mode = str(event.result.get("mode") or "")
                message_id = str(event.result.get("id") or "")
            if mode == "steer" and event.content:
                self.add_user(event.content, pending=True, message_id=message_id)
                self.status = "Steering queued"
            elif mode == "followUp":
                self.status = f"Follow-up queued ({event.content[:40]})"
        elif event.type == "steering_applied":
            message_id = ""
            if isinstance(event.result, dict):
                message_id = str(event.result.get("id") or "")
            block_id = self.pending_user_ids.pop(message_id, None)
            if block_id:
                for block in self.blocks:
                    if isinstance(block, MessageBlock) and block.id == block_id:
                        block.pending = False
                        break
        elif event.type == "run_cancelling":
            self.status = "Cancelling…"
        elif event.type == "interaction_requested":
            self.running = False
            self.status = "Waiting for input"
            self.active_assistant = None
            for block in reversed(self.blocks):
                if isinstance(block, ToolBlock) and block.status == "running":
                    block.status = "waiting"
                    break
        elif event.type == "run_completed":
            self.running = False
            self.status = "Ready"
            if event.content and not self._has_assistant_text(event.content):
                self.blocks.append(MessageBlock(kind="assistant", content=event.content))
            self.active_assistant = None
        elif event.type == "run_cancelled":
            self.running = False
            self.status = "Cancelled"
            self.add_system("Operation cancelled.", error=True)
        elif event.type == "run_failed":
            self.running = False
            self.status = "Failed"
            self.add_system(event.content or "Unknown error", error=True)

    def _assistant_block(self) -> MessageBlock:
        if self.active_assistant is not None:
            block = self.blocks[self.active_assistant]
            if isinstance(block, MessageBlock):
                return block
        block = MessageBlock(kind="assistant")
        self.blocks.append(block)
        self.active_assistant = len(self.blocks) - 1
        return block

    def _tool(self, tool_call_id: str) -> ToolBlock | None:
        if not tool_call_id:
            return self._running_tool()
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
