"""Filesystem middleware capability check for the workspace shell wrapper."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware

from agent.sandbox import WorkspaceCompositeBackend

LS_DESCRIPTION = (
    "列出目录中的文件和子目录。path 必须是绝对路径。\n"
    "已知文件路径时直接用 read_file，不必先 ls；按名称查找文件用 execute 运行 rg --files。"
)

READ_FILE_DESCRIPTION = (
    "读取文件。file_path 必须是绝对路径；文件不存在时返回错误。\n"
    "- 默认从开头读 100 行。阅读代码时按需传更大的 limit（如 500）一次读够上下文，"
    "避免逐页翻读；超大文件再用 offset/limit 分段。\n"
    "- 内容上方有一行 `@@ ... @@` 状态头，之后每行都是文件原文；编辑时不要包含状态头。\n"
    "- 需要多个文件时，在同一轮回复中并行调用。\n"
    "- 被转存到文件的大工具结果，按消息中给出的路径用本工具分页读取。\n"
    "- 图片、PDF、音视频返回多模态内容，只传 file_path；视频的 offset/limit 按秒计。\n"
    "- 编辑文件前必须先读取。"
)

TOOL_DESCRIPTIONS = {"ls": LS_DESCRIPTION, "read_file": READ_FILE_DESCRIPTION}


class WorkspaceFilesystemMiddleware(FilesystemMiddleware):
    """Keep the separately registered execute tool visible to the model.

    Deep Agents checks only a CompositeBackend's default route for shell
    support. Our default route denies file access outside the project mount, while
    WorkspaceCompositeBackend.execute delegates to the sandbox executor.
    """

    def __init__(self, *, custom_tool_descriptions: Mapping[str, str] | None = None, **kwargs: Any) -> None:
        super().__init__(custom_tool_descriptions={**TOOL_DESCRIPTIONS, **(custom_tool_descriptions or {})}, **kwargs)

    @property
    def name(self) -> str:
        # Deep Agents replaces its built-in middleware by matching this name.
        return "FilesystemMiddleware"

    def _unsupported_tools_and_execution_state(
        self, tool_names: set[str | None],
    ) -> tuple[set[str | None], bool, BackendProtocol | None]:
        unsupported, execution_active, backend = super()._unsupported_tools_and_execution_state(tool_names)
        if "execute" in tool_names and isinstance(self.backend, WorkspaceCompositeBackend):
            unsupported.discard("execute")
            execution_active = True
            # Upstream builds its "virtual mounts vs. shell paths" prompt from
            # the returned backend, judging reachability by the composite's
            # default route. Our execute shell runs on the executor's
            # filesystem, where the project mount is the shell root, so returning the
            # composite would inject a bogus "workspace is not accessible
            # from the shell" notice. The executor is not a CompositeBackend,
            # which makes that prompt empty.
            backend = self.backend.executor
        return unsupported, execution_active, backend
