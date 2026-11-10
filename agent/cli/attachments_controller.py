"""Image attachments and pasted input workflow."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Awaitable
from pathlib import Path
from typing import Any

from agent.sessions.attachments import image_attachment_from_path
from agent.sessions.images import MAX_IMAGES_PER_MESSAGE
from agent.cli.clipboard import (
    ClipboardError,
    ClipboardFiles,
    ClipboardImage,
    ClipboardText,
    ClipboardUnavailable,
    windows_path_to_wsl,
)
from agent.cli.lifecycle import TaskStop


if TYPE_CHECKING:
    from agent.runtime.runner import AgentRunner
    from agent.cli.lifecycle import AppLifecycle
    from prompt_toolkit.buffer import Buffer
    from agent.cli.state import CliState
    from agent.cli.clipboard import ClipboardAdapter
    from agent.cli.view_state import InputDrafts


class AttachmentsController:
    def __init__(
        self, *,
        _run_blocking: Callable[..., Awaitable[Any]],
        buffer: Buffer,
        clipboard: ClipboardAdapter,
        drafts: InputDrafts,
        lifecycle: Callable[[], AppLifecycle],
        runner: AgentRunner,
        set_status: Callable[..., None],
        state: CliState,
        invalidate: Callable[[], None],
    ) -> None:
        self._run_blocking = _run_blocking
        self.buffer = buffer
        self.clipboard = clipboard
        self.drafts = drafts
        self.lifecycle = lifecycle
        self.runner = runner
        self.set_status = set_status
        self.state = state
        self.invalidate = invalidate


    async def image_command(self, arg: str) -> None:
        value = arg.strip()
        if not value:
            if not self.state.attachments:
                self.state.add_system("No images attached. Use /image <path>.")
                return
            lines = [f"Image #{index}: {ref.filename} ({ref.size} bytes)" for index, ref in enumerate(self.state.attachments, 1)]
            self.state.add_system("Pending images\n" + "\n".join(lines))
            return
        if value.lower() == "clear":
            count = len(self.state.attachments)
            self.state.attachments.clear()
            self.set_status(f"Cleared {count} pending image(s)")
            return
        if value.lower() == "clipboard":
            self.set_status("Reading clipboard…", transient=False)
            await self._paste_clipboard(images_only=True)
            return
        await self._attach_path(value)

    async def attachments_command(self, arg: str) -> None:
        if arg.strip().lower() != "cleanup":
            self.state.add_system("Usage: /attachments cleanup", error=True)
            return
        try:
            result = await self._run_blocking(
                self.runner.cleanup_attachments,
                protected=tuple(self.state.attachments),
            )
        except Exception as exc:  # noqa: BLE001
            self.state.add_system(f"Attachment cleanup failed: {exc}", error=True)
            return
        if result.skipped_busy:
            self.state.add_system("Attachment cleanup skipped: this workspace is in use by another process.", error=True)
            return
        self.state.add_system(
            f"Attachment cleanup: scanned {result.scanned}, kept {result.kept}, "
            f"deleted {result.deleted}, freed {result.freed_bytes} bytes."
        )

    async def _attach_path(self, value: str) -> bool:
        if not self.runner.supports_next_input("image"):
            self.set_status("Current or pending model does not support image input")
            return False
        if len(self.state.attachments) >= MAX_IMAGES_PER_MESSAGE:
            self.set_status(f"A message can contain at most {MAX_IMAGES_PER_MESSAGE} images")
            return False
        try:
            path = windows_path_to_wsl(value)
            attachment = await self._run_blocking(image_attachment_from_path, path)
            ref = await self._run_blocking(self.runner.store_image, attachment)
        except (OSError, ValueError, ClipboardError) as exc:
            self.state.add_system(str(exc), error=True)
            return False
        self.state.attachments.append(ref)
        self.set_status(f"Attached image: {ref.filename}")
        return True

    def _handle_pasted_text(self, payload: str) -> None:
        trimmed = payload.strip()
        if trimmed and "\n" not in trimmed:
            try:
                path = windows_path_to_wsl(trimmed)
                looks_like_image = path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
                if path.is_file() and looks_like_image:
                    if self.runner.supports_next_input("image"):
                        self.lifecycle().spawn(
                            self._attach_pasted_path(trimmed, payload),
                            stop=TaskStop.CANCEL,
                            name="attach-image",
                        )
                        return
                    self.set_status("Image path pasted as text: current or pending model does not support images")
            except (OSError, ClipboardError):
                pass
        self.buffer.insert_text(self.drafts.pasted.display(payload))

    async def _attach_pasted_path(self, value: str, original_payload: str) -> None:
        if not await self._attach_path(value):
            self.buffer.insert_text(self.drafts.pasted.display(original_payload))
            self.invalidate()

    async def _paste_clipboard(self, *, images_only: bool = False) -> None:
        content = await self._run_blocking(self.clipboard.inspect)
        if isinstance(content, ClipboardText):
            if images_only:
                self.set_status("Clipboard does not contain an image")
                return
            self._handle_pasted_text(content.text)
            self.invalidate()
            return
        if isinstance(content, ClipboardUnavailable):
            self.set_status(content.reason)
            return
        if not self.runner.supports_next_input("image"):
            self.set_status("Current or pending model does not support image input")
            return
        if isinstance(content, ClipboardFiles):
            attached = 0
            for value in content.paths:
                if len(self.state.attachments) >= MAX_IMAGES_PER_MESSAGE:
                    break
                if Path(value).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                    continue
                if await self._attach_path(value):
                    attached += 1
            if not attached:
                self.set_status("Clipboard contains no supported image files")
            return
        if isinstance(content, ClipboardImage):
            if len(self.state.attachments) >= MAX_IMAGES_PER_MESSAGE:
                self.set_status(f"A message can contain at most {MAX_IMAGES_PER_MESSAGE} images")
                return
            temp: Path | None = None
            try:
                temp = await self._run_blocking(self.clipboard.export_image)
                await self._attach_path(str(temp))
            except (OSError, ClipboardError, ValueError) as exc:
                self.state.add_system(str(exc), error=True)
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)
