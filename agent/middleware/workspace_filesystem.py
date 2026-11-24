"""Filesystem middleware capability check for the workspace shell wrapper."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool

from agent.execution.filesystem import WorkspaceCompositeBackend
from agent.tools.catalog import FILE_MUTATION_TOOLS


LS_DESCRIPTION = (
    "列出目录中的文件和子目录。path 必须是绝对路径。\n"
    "已知文件路径时直接用 read，不必先 ls；按名称查找文件用 execute 运行 rg --files。"
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

_SAME_PATH_SERIAL = (
    "同一路径不允许并发变更："
    "同一轮回复中不要对同一 file_path 发出多次 write / edit / delete；"
    "先完成一次并看到结果，再改同一文件。"
)
_MULTI_PATH_PARALLEL = (
    "可以对多个不同路径并发：同一轮回复中可并行调用多个 write / edit / delete，"
    "只要各自的 file_path 互不相同。"
)

WRITE_FILE_DESCRIPTION = (
    "写入文件。file_path 必须是绝对路径；文件不存在则创建，已存在则整文件替换。\n"
    "- 打算新建或整文件覆盖时用本工具；不必先 read。\n"
    "- 修改已有文件时优先用 edit，而不是 write 整文件重写。\n"
    f"- {_SAME_PATH_SERIAL}\n"
    f"- {_MULTI_PATH_PARALLEL}"
)

EDIT_FILE_DESCRIPTION = (
    "对文件做精确字符串替换。file_path 必须是绝对路径。\n"
    "- 编辑前必须先 read。\n"
    "- old_string / new_string 必须保留 read 结果中的原始缩进，不要包含 read 的 `@@ ... @@` 状态头。\n"
    "- 优先编辑已有文件，而不是新建。\n"
    "- 除非用户明确要求，不要使用 emoji。\n"
    "- 同一文件多处修改：合并为一次 edit，或等本轮结果返回后再发下一次；"
    "同一轮不要对同一 file_path 并发多次 edit。\n"
    f"- {_MULTI_PATH_PARALLEL}"
)

DELETE_FILE_DESCRIPTION = (
    "删除文件或目录。file_path 必须是绝对路径。\n"
    "- 删除目录会递归移除其下全部内容；优先一次删除整个目录，而不是逐个删文件。\n"
    "- 不可撤销，只删除确认不再需要的路径。\n"
    f"- {_SAME_PATH_SERIAL}\n"
    f"- {_MULTI_PATH_PARALLEL}"
)

TOOL_DESCRIPTIONS = {
    "ls": LS_DESCRIPTION,
    "read": READ_FILE_DESCRIPTION,
    "write": WRITE_FILE_DESCRIPTION,
    "edit": EDIT_FILE_DESCRIPTION,
    "delete": DELETE_FILE_DESCRIPTION,
}

_NATIVE_FILE_NAMES = {"read": "read_file", "write": "write_file", "edit": "edit_file"}
_FILE_TOOL_NAMES = {native: name for name, native in _NATIVE_FILE_NAMES.items()}


class WorkspaceFilesystemMiddleware(FilesystemMiddleware):
    """Keep the separately registered execute tool visible to the model.

    Deep Agents checks only a CompositeBackend's default route for shell
    support. Our default route denies file access outside the project mount, while
    WorkspaceCompositeBackend.execute delegates to the sandbox executor.
    """

    def __init__(self, *, custom_tool_descriptions: Mapping[str, str] | None = None, **kwargs: Any) -> None:
        if (tools := kwargs.get("tools")) is not None:
            kwargs["tools"] = [_NATIVE_FILE_NAMES.get(name, name) for name in tools]
        descriptions = {**TOOL_DESCRIPTIONS, **(custom_tool_descriptions or {})}
        super().__init__(
            custom_tool_descriptions={_NATIVE_FILE_NAMES.get(name, name): text for name, text in descriptions.items()},
            **kwargs,
        )

    def _create_tools(self) -> list[BaseTool]:
        tools = super()._create_tools()
        for tool in tools:
            tool.name = _FILE_TOOL_NAMES.get(tool.name, tool.name)
            for native, name in _FILE_TOOL_NAMES.items():
                tool.description = tool.description.replace(native, name)
        return tools

    @staticmethod
    def _filesystem_request(request: Any) -> Any:
        """Adapt only the upstream filesystem hook's view of the current call."""
        name = request.tool_call["name"]
        if name not in _NATIVE_FILE_NAMES and name != "delete":
            return request
        call = {**request.tool_call, "name": _NATIVE_FILE_NAMES.get(name, name)}
        state = request.state
        if name in FILE_MUTATION_TOOLS and isinstance(state, Mapping):
            messages = list(state.get("messages", ()))
            for index in range(len(messages) - 1, -1, -1):
                message = messages[index]
                if isinstance(message, AIMessage):
                    calls = [
                        {**item, "name": _NATIVE_FILE_NAMES.get(item["name"], item["name"])}
                        for item in message.tool_calls
                    ]
                    messages[index] = message.model_copy(update={"tool_calls": calls})
                    state = {**state, "messages": messages}
                    break
        return request.override(tool_call=call, state=state)

    @staticmethod
    def _name_result(result: Any, name: str) -> Any:
        if isinstance(result, ToolMessage) and result.name in _FILE_TOOL_NAMES:
            return result.model_copy(update={"name": name})
        return result

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        result = super().wrap_tool_call(self._filesystem_request(request), lambda _: handler(request))
        return self._name_result(result, request.tool_call["name"])

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        result = await super().awrap_tool_call(self._filesystem_request(request), lambda _: handler(request))
        return self._name_result(result, request.tool_call["name"])

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
