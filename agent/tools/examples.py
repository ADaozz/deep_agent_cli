# Optional documentation lookup example. It is not part of the default agent.
from langchain_core.tools import BaseTool, StructuredTool

DOCS = {
    "deep-agent": "Deep Agents 以 create_deep_agent() 装配 LangGraph 图，中间件负责文件系统、HITL、Skills 与重试。",
    "middleware": "模板默认栈：PauseGate → ModelRetry → Filesystem。",
    "hitl": "interrupt_on 标记的工具会在执行前暂停，等待 approve / reject。",
}


def lookup_docs(query: str) -> str:
    """Search local documentation. ALLOW — runs immediately without confirmation."""
    key = (query or "").strip().lower()
    for name, text in DOCS.items():
        if name in key or key in name:
            return text
    return "未找到文档。可检索：deep-agent、middleware、hitl。"


def build_example_tools() -> list[BaseTool]:
    return [
        StructuredTool.from_function(
            func=lookup_docs,
            name="lookup_docs",
            description=(
                "检索本地文档。参数 query 为关键词（deep-agent / middleware / hitl）。"
                "该工具可直接执行，不需要人工确认。"
            ),
        ),
    ]
