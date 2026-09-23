# Local tools that stand in for the platform ToolGateway two-layer policy:
# ALLOW tools run immediately; CONFIRM tools are listed in interrupt_on so
# HumanInTheLoopMiddleware pauses before the function body executes.
from langchain_core.tools import BaseTool, StructuredTool

DOCS = {
    "deep-agent": "Deep Agents 以 create_deep_agent() 装配 LangGraph 图，中间件负责文件系统、HITL、Skills 与重试。",
    "middleware": "模板默认栈：PauseGate → ModelRetry → ToolRetry → TodoList → Filesystem。",
    "hitl": "interrupt_on 标记的工具会在执行前暂停，等待 approve / reject。",
}


def lookup_docs(query: str) -> str:
    """Search local documentation. ALLOW — runs immediately without confirmation."""
    key = (query or "").strip().lower()
    for name, text in DOCS.items():
        if name in key or key in name:
            return text
    return "未找到文档。可检索：deep-agent、middleware、hitl。"


def send_email(to: str, subject: str, body: str) -> str:
    """Send an email. CONFIRM — HumanInTheLoopMiddleware interrupts before this runs."""
    return f"已发送邮件给 {to}，主题：{subject}。正文预览：{body[:80]}"


CONFIRM_INTERRUPT_ON = {"send_email": {"allowed_decisions": ["approve", "reject"]}}


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
        StructuredTool.from_function(
            func=send_email,
            name="send_email",
            description=(
                "发送邮件。参数 to 为收件人，subject 为主题，body 为正文。"
                "该动作需要人工确认后才会真正执行。"
            ),
        ),
    ]
