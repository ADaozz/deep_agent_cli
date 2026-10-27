"""为第三方内置工具提供中文说明，不修改执行节点的工具或参数校验。"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool

TOOL_DESCRIPTIONS = {
    "ls": "列出指定目录中的文件，用于查看目录结构并定位需要阅读或修改的文件。",
    "read_file": (
        "读取文件；路径不存在时返回错误。默认从开头读取最多 100 行，可通过 offset 和 limit 分页。"
        "状态头 @@ field | field | ... @@ 之后是原始文件内容；编辑时不要包含状态头。"
        "可批量读取多个相关文件。空文件返回提示；大结果可能保存到文件，可按返回路径分页读取。"
        "图片、音频、视频和 PDF 返回多模态内容；读取图片或 PDF 时只传 file_path。编辑前先读取文件。"
    ),
    "write_file": (
        "写入文件全文；文件不存在时创建，已存在时完全覆盖，无需事先读取。"
        "局部修改已有文件时优先使用 edit_file。"
    ),
    "edit_file": (
        "按精确字符串替换文件内容。编辑前必须先读取文件，否则返回错误。"
        "保留原始缩进，old_string 和 new_string 不得包含读取结果的状态头。"
        "优先修改已有文件；仅在用户明确要求时添加表情符号。"
    ),
    "delete": "永久删除指定文件或目录；删除目录会递归删除全部内容。操作不可撤销，只删除确认不再需要的路径。",
    "compact_conversation": (
        "将较早的对话压缩为简洁摘要，释放上下文空间。"
        "适用于对话过长、切换到无关任务，或完成总结与提取后不再需要之前工作上下文的情况。无需参数。"
    ),
}

_PARAMETER_DESCRIPTIONS = {
    "path": "目录的绝对路径，不接受相对路径。",
    "file_path": "文件或目录的绝对路径，不接受相对路径。",
    "offset": "从第几行开始读取，从 0 计数，用于大文件分页。",
    "limit": "本次最多读取的行数，用于大文件分页。",
    "content": "要写入文件的完整文本，必须提供。",
    "old_string": "待替换的精确文本；replace_all 为 false 时必须在文件中唯一。",
    "new_string": "替换后的文本，必须与 old_string 不同。",
    "replace_all": "true 表示替换全部匹配；false 表示只替换唯一匹配。",
}


class ChineseToolDescriptionsMiddleware(AgentMiddleware):
    """仅替换模型请求中的内置工具说明，保留字段名和原始工具实现。"""

    def _localize(self, request: Any) -> Any:
        tools = []
        for tool in request.tools:
            name = tool.get("name") or tool.get("function", {}).get("name") if isinstance(tool, dict) else tool.name
            if name not in TOOL_DESCRIPTIONS:
                tools.append(tool)
                continue
            # Conversion copies the tool schema; execution keeps the original
            # tool, including injected runtime arguments and validation.
            localized = deepcopy(convert_to_openai_tool(tool))
            function = localized["function"]
            function["description"] = TOOL_DESCRIPTIONS[name]
            parameters = function.get("parameters", {})
            if "description" in parameters:
                parameters["description"] = TOOL_DESCRIPTIONS[name]
            for field, schema in parameters.get("properties", {}).items():
                if field in _PARAMETER_DESCRIPTIONS:
                    schema["description"] = _PARAMETER_DESCRIPTIONS[field]
            tools.append(
                tool.model_copy(update={"description": function["description"], "args_schema": parameters})
                if isinstance(tool, BaseTool) else localized
            )
        return request.override(tools=tools)

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(self._localize(request))

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return await handler(self._localize(request))
