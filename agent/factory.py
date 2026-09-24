# Assembly entry: create_deep_agent() only. Do not fork Deep Agents core.
# Middleware augments the Deep Agents defaults; create_deep_agent remains the only assembly entry.
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends.protocol import BackendProtocol, SandboxBackendProtocol
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.summarization import (
    SummarizationToolMiddleware,
    create_summarization_tool_middleware,
)
from deepagents._models import get_model_identifier, get_model_provider
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from agent.config import SandboxConfig, Settings
from agent.control import RunController
from agent.llm import build_chat_model
from agent.middleware.cancel_tools import ToolCancelMiddleware
from agent.middleware.attachments import AttachmentMaterializationMiddleware
from agent.middleware.pause import PauseGateMiddleware
from agent.middleware.recovery import RecoveryContextMiddleware
from agent.middleware.steering import SteeringMiddleware
from agent.middleware.tool_arg_hints import ToolArgHintMiddleware
from agent.permission import (
    PermissionMode, allow_mode_unavailable_reason, interrupt_on_for_mode,
    permission_mode_from_interrupt_on,
)
from agent.sandbox import ExecutionMode, SKILLS_ROOT, WorkspaceCompositeBackend, select_backend
from agent.tools.execute import build_execute_tool
from agent.tools.human_input import build_human_input_tools

DEFAULT_FS_TOOLS = ["ls", "read_file", "glob", "grep", "write_file", "edit_file", "delete"]
DEFAULT_SYSTEM_PROMPT = (
    "你是使用 {model_name} 的 Coding Agent CLI。"
    "完成用户请求后直接报告结果。"
    "如果继续执行需要用户提供信息或从多个选项中做决定，调用 request_human_input 暂停等待回答。"
    "如果你主动给用户列出多个后续操作供其选择，也必须调用 request_human_input，"
    "用 fields 的 single_select 或 multi_select 表达选项；不要只在普通回复中写编号菜单。"
)


@dataclass(frozen=True)
class AgentSpec:
    instructions: str | None = None
    tools: tuple[BaseTool, ...] = ()
    skills: tuple[str, ...] | None = None
    backend: BackendProtocol | None = None
    sandbox: SandboxConfig | None = None


@dataclass
class PreparedAgent:
    graph: Any
    backend: BackendProtocol
    model: BaseChatModel | None = None
    spec: AgentSpec = field(default_factory=AgentSpec)
    interrupt_on: dict[str, Any] = field(default_factory=dict)
    system_prompt: str = ""
    exposed_tool_names: list[str] = field(default_factory=list)
    filesystem_tools: list[str] = field(default_factory=list)
    execution_mode: ExecutionMode = ExecutionMode.CUSTOM
    security_warning: str = ""
    checkpointer: Any = None
    run_controller: RunController | None = None
    pause_condition: Callable[[], bool] = field(default=lambda: False)
    compact_middleware: SummarizationToolMiddleware | None = None


def compose_system_prompt(spec: AgentSpec, model: BaseChatModel, workspace: Path) -> str:
    model_name = str(getattr(model, "model_name", None) or get_model_identifier(model) or type(model).__name__)
    sections = [DEFAULT_SYSTEM_PROMPT.format(model_name=model_name)]
    if spec.instructions and spec.instructions.strip():
        sections.append(f"# User Instructions\n{spec.instructions.strip()}")
    project_instructions = workspace.expanduser().resolve() / "AGENTS.md"
    if project_instructions.is_file():
        sections.append(f"# Project Instructions\n{project_instructions.read_text(encoding='utf-8')}")
    return "\n\n".join(sections)


def build_agent(
    spec: AgentSpec,
    model: BaseChatModel,
    permission: PermissionMode,
    checkpointer: Any | None,
    run_controller: RunController,
    should_pause: Callable[[], bool],
    interrupt_on_override: dict[str, Any] | None = None,
) -> PreparedAgent:
    sandbox_cfg = spec.sandbox or SandboxConfig()
    if spec.backend is None:
        selected = select_backend(sandbox_cfg)
        fs_backend = selected.backend
        execution_mode = selected.mode
        security_warning = selected.warning
    else:
        fs_backend = spec.backend
        execution_mode = ExecutionMode.CUSTOM
        security_warning = ""
    if permission is PermissionMode.ALLOW and execution_mode is not ExecutionMode.SANDBOXED:
        raise ValueError(allow_mode_unavailable_reason(execution_mode))
    _disable_general_purpose_task(model)
    prompt = compose_system_prompt(spec, model, sandbox_cfg.workspace)
    tools = [*build_human_input_tools(), *spec.tools]
    hitl = dict(interrupt_on_override) if interrupt_on_override is not None else (interrupt_on_for_mode(permission) or {})
    filesystem_tools = list(DEFAULT_FS_TOOLS)
    supports_execute = isinstance(fs_backend, SandboxBackendProtocol)
    if supports_execute:
        tools.append(build_execute_tool(fs_backend))
    skill_sources = list(spec.skills) if spec.skills is not None else (
        _default_skill_sources(fs_backend) if spec.backend is None else None
    )
    saver = checkpointer if checkpointer is not None else InMemorySaver()
    compact_middleware = create_summarization_tool_middleware(model, fs_backend)
    graph = create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=prompt,
        middleware=[
            PauseGateMiddleware(lambda: run_controller.pause_requested or should_pause()),
            RecoveryContextMiddleware(),
            SteeringMiddleware(run_controller),
            ToolCancelMiddleware(run_controller),
            ToolArgHintMiddleware(),
            AttachmentMaterializationMiddleware(),
            TodoListMiddleware(),
            FilesystemMiddleware(backend=fs_backend, tools=filesystem_tools),
            compact_middleware,
        ],
        skills=skill_sources,
        backend=fs_backend,
        interrupt_on=hitl or None,
        checkpointer=saver,
        name="deep-agent-template",
    )
    return PreparedAgent(
        graph=graph,
        backend=fs_backend,
        model=model,
        spec=spec,
        interrupt_on=hitl,
        system_prompt=prompt,
        exposed_tool_names=[tool.name for tool in tools if tool.name != "request_human_input"],
        filesystem_tools=filesystem_tools,
        execution_mode=execution_mode,
        security_warning=security_warning,
        checkpointer=saver,
        run_controller=run_controller,
        pause_condition=should_pause,
        compact_middleware=compact_middleware,
    )


def create_agent(
    *,
    model: BaseChatModel | None = None,
    checkpointer: Any | None = None,
    should_pause: Callable[[], bool] | None = None,
    instructions: str | None = None,
    extra_tools: list[BaseTool] | None = None,
    interrupt_on: dict[str, Any] | None = None,
    skills: list[str] | None = None,
    settings: Settings | None = None,
    backend: BackendProtocol | None = None,
    sandbox_config: SandboxConfig | None = None,
    run_controller: RunController | None = None,
) -> PreparedAgent:
    cfg = settings or Settings()
    spec = AgentSpec(
        instructions=instructions if instructions is not None else cfg.agent_instructions,
        tools=tuple(extra_tools or ()),
        skills=tuple(skills) if skills is not None else None,
        backend=backend,
        sandbox=sandbox_config or cfg.sandbox,
    )
    permission = permission_mode_from_interrupt_on(interrupt_on) if interrupt_on is not None else PermissionMode.ASK
    effective_interrupt_on = interrupt_on
    if interrupt_on:
        defaults = interrupt_on_for_mode(PermissionMode.ASK) or {}
        for name, rule in defaults.items():
            if name in interrupt_on and interrupt_on[name] != rule:
                raise ValueError(f"Cannot override the default approval rule for {name}")
        effective_interrupt_on = {**defaults, **interrupt_on}
    return build_agent(
        spec, model or _default_model(cfg), permission, checkpointer,
        run_controller or RunController(), should_pause or (lambda: False),
        interrupt_on_override=effective_interrupt_on,
    )


def _default_model(cfg: Settings) -> BaseChatModel:
    return build_chat_model(cfg.active_profile, streaming=True)


def _default_skill_sources(backend: BackendProtocol) -> list[str] | None:
    if isinstance(backend, WorkspaceCompositeBackend) and backend.skills_dir is not None:
        return [f"{SKILLS_ROOT}/"]
    return None


_NO_GP = HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False))


def _disable_general_purpose_task(model: BaseChatModel) -> None:
    provider = get_model_provider(model)
    identifier = get_model_identifier(model)
    if identifier:
        key = identifier if ":" in identifier or not provider else f"{provider}:{identifier}"
        register_harness_profile(key, _NO_GP)
    elif provider:
        # Models without an identifier cannot be targeted more narrowly upstream.
        register_harness_profile(provider, _NO_GP)
