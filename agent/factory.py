# Assembly entry: create_deep_agent() only. Do not fork Deep Agents core.
# Middleware augments the Deep Agents defaults; create_deep_agent remains the only assembly entry.
from dataclasses import dataclass, field, replace
import os
from pathlib import Path
from typing import Any, Callable

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends.protocol import BackendProtocol, SandboxBackendProtocol
from deepagents.middleware.subagents import CompiledSubAgent
from deepagents.middleware.summarization import (
    SummarizationToolMiddleware,
    create_summarization_tool_middleware,
)
from deepagents._models import get_model_identifier, get_model_provider
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from agent.config import SandboxConfig, Settings
from agent.control import RunController
from agent.environment import EnvironmentContextMiddleware
from agent.llm import ProtocolChatOpenAI, build_chat_model
from agent.middleware.cancel_tools import ToolCancelMiddleware
from agent.middleware.current_time import CurrentTimeMiddleware
from agent.middleware.attachments import AttachmentMaterializationMiddleware
from agent.middleware.pause import PauseGateMiddleware
from agent.middleware.usage import UsageIdentityMiddleware
from agent.middleware.recovery import RecoveryContextMiddleware
from agent.middleware.steering import SteeringMiddleware
from agent.middleware.tool_arg_hints import ToolArgHintMiddleware
from agent.middleware.write_operation import WriteOperationMiddleware
from agent.middleware.workspace_filesystem import WorkspaceFilesystemMiddleware
from agent.permission import (
    PermissionMode, allow_mode_unavailable_reason, interrupt_on_for_mode,
    permission_mode_from_interrupt_on,
)
from agent.sandbox import (
    BackendSelection, ExecutionMode, SKILLS_ROOT, WorkspaceCompositeBackend, sandbox_pool_of, select_backend,
)
from agent.sandbox_pool import SandboxPool
from agent.tools.execute import build_execute_tool
from agent.tools.human_input import build_human_input_tools
from agent.middleware.runtime_config import RuntimeConfigGateMiddleware
from agent.tools.web_search import build_web_search_tool

DEFAULT_FS_TOOLS = ["ls", "read_file", "write_file", "edit_file", "delete"]

DEFAULT_SYSTEM_PROMPT = (
    "你是运行在 Deep Agent CLI 中、使用 {model_name} 的编程智能体。"
    "你的职责是理解用户需求、检查相关代码、完成任务、验证结果并准确汇报。\n\n"

    "# 任务执行\n"
    "- 优先自主完成用户请求，不要只停留在分析或建议阶段。\n"
    "- 简单任务直接执行；复杂任务先明确实施顺序，再逐步推进。\n"
    "- 充分利用现有上下文和工具，不要反复询问能够自行确定的信息。\n"
    "- 遇到失败时分析原因并调整方法，不要无意义地重复相同操作。\n"
    "- 不猜测文件内容、工具结果或执行状态；无法完成时明确说明原因。\n\n"

    "# 代码修改\n"
    "- 修改前先理解相关实现、依赖关系和项目约定。\n"
    "- 优先修复根本原因，保持改动最小、聚焦且符合现有代码风格。\n"
    "- 不进行无关重构，不修复无关问题，不引入不必要的依赖或抽象。\n"
    "- 遵循适用的 AGENTS.md 项目规则，目录越深的规则在其作用域内越优先。\n"
    "- 遵守沙箱限制、文件访问权限和审批机制。\n"
    "- 未经用户明确要求，不创建 Git 分支、不提交或推送代码。\n"
    "- 必要时同步更新相关测试和文档。\n\n"

    "# 验证\n"
    "- 修改代码后尽可能进行验证，从与改动直接相关的测试开始。\n"
    "- 根据风险和执行成本决定是否扩大测试范围。\n"
    "- 区分本次修改引入的错误与项目原有问题，不擅自修改无关测试。\n"
    "- 无法运行测试时说明限制，不声称未经验证的结果已经通过。\n\n"

    "# 沟通与结果\n"
    "- 长任务在关键阶段简要报告进度，避免逐条复述工具调用。\n"
    "- 任务结束后简洁说明完成内容、验证结果和剩余问题。\n"
    "- 优先提供明确、可操作的信息，避免冗长解释和无意义的总结。"
)


class HiddenToolsMiddleware(AgentMiddleware):
    """Filter middleware-provided tools too, without changing approval rules."""

    def __init__(self, hidden: tuple[str, ...]) -> None:
        self.hidden = frozenset(hidden)

    def _filter(self, request: Any) -> Any:
        return request.override(tools=[
            tool for tool in request.tools
            if (tool.get("name") or tool.get("function", {}).get("name")
                if isinstance(tool, dict) else tool.name) not in self.hidden
        ])

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(self._filter(request))

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return await handler(self._filter(request))


@dataclass(frozen=True)
class AgentSpec:
    instructions: str | None = None
    tools: tuple[BaseTool, ...] = ()
    skills: tuple[str, ...] | None = None
    backend: BackendProtocol | None = None
    sandbox: SandboxConfig | None = None
    web_search_api_key: str | None = field(default=None, repr=False)
    hidden_tools: tuple[str, ...] = ()
    trust_env: bool = False
    role: str = "main"
    settings: Settings | None = field(default=None, repr=False)


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
    # 重建时复用，切换模型或权限模式时保留同一套沙箱。
    backend_selection: BackendSelection | None = None
    sandbox_pool: SandboxPool | None = None
    skill_sources: tuple[str, ...] = ()
    model_tool_names: tuple[str, ...] = ()
    subagent_manager: Any = None
    owns_model_clients: bool = False


def compose_system_prompt(spec: AgentSpec, model: BaseChatModel, workspace: Path) -> str:
    model_name = str(getattr(model, "model_name", None) or get_model_identifier(model) or type(model).__name__)
    sections = [DEFAULT_SYSTEM_PROMPT.format(model_name=model_name)]
    if spec.instructions and spec.instructions.strip():
        sections.append(f"# 用户指令\n{spec.instructions.strip()}")
    project_instructions = workspace.expanduser().resolve() / "AGENTS.md"
    if project_instructions.is_file():
        sections.append(f"# 项目指令\n{project_instructions.read_text(encoding='utf-8')}")
    return "\n\n".join(sections)


def build_agent(
    spec: AgentSpec,
    model: BaseChatModel,
    permission: PermissionMode,
    checkpointer: Any | None,
    run_controller: RunController,
    should_pause: Callable[[], bool],
    interrupt_on_override: dict[str, Any] | None = None,
    backend_selection: BackendSelection | None = None,
    mcp: Any | None = None,
    settings: Settings | None = None,
) -> PreparedAgent:
    sandbox_cfg = replace(spec.sandbox or SandboxConfig(), trust_env=spec.trust_env)
    selected: BackendSelection | None = None
    if spec.backend is None:
        selected = backend_selection or select_backend(sandbox_cfg)
        fs_backend = selected.backend
        execution_mode = selected.mode
        security_warning = selected.warning
    else:
        fs_backend = spec.backend
        selected = backend_selection if backend_selection is not None and backend_selection.backend is fs_backend else None
        execution_mode = selected.mode if selected is not None else ExecutionMode.CUSTOM
        security_warning = selected.warning if selected is not None else ""
    if permission is PermissionMode.ALLOW and execution_mode is not ExecutionMode.SANDBOXED:
        raise ValueError(allow_mode_unavailable_reason(execution_mode))
    sandbox_pool = sandbox_pool_of(fs_backend)
    _disable_general_purpose_task(model)
    prompt = compose_system_prompt(spec, model, sandbox_cfg.workspace)
    tools = [*build_human_input_tools(), *spec.tools] if spec.role == "main" else []
    if spec.web_search_api_key:
        tools.append(build_web_search_tool(spec.web_search_api_key, trust_env=spec.trust_env))
    hitl = dict(interrupt_on_override) if interrupt_on_override is not None else (interrupt_on_for_mode(permission) or {})
    filesystem_tools = list(DEFAULT_FS_TOOLS)
    supports_execute = isinstance(fs_backend, SandboxBackendProtocol)
    if supports_execute:
        tools.append(build_execute_tool(
            fs_backend,
            network_by_default=permission is PermissionMode.ALLOW,
        ))
    skill_sources = list(spec.skills) if spec.skills is not None else (
        _default_skill_sources(fs_backend) if spec.backend is None else None
    )
    tools = [tool for tool in tools if tool.name not in spec.hidden_tools]
    saver = checkpointer if checkpointer is not None else InMemorySaver()
    compact_middleware = create_summarization_tool_middleware(model, fs_backend)
    mcp_revision: int | None = None
    mcp_tools: list[Any] = []
    include_mcp = False
    if mcp is not None and spec.role == "main":
        from agent.mcp.middleware import MCPMiddleware, MCP_HOST_PROMPT
        from agent.mcp.names import host_interrupt_on

        mcp_revision, mcp_tools = mcp.snapshot()
        include_mcp = bool(mcp.snapshot_includes_mcp(mcp_revision))
        if include_mcp:
            if permission is PermissionMode.ASK:
                hitl.update(host_interrupt_on(tool.name for tool in mcp_tools))
            prompt = f"{prompt}\n\n{MCP_HOST_PROMPT}"
    subagents: list[CompiledSubAgent] = []
    manager = None
    cfg = settings or spec.settings or Settings()
    if spec.role == "main" and cfg.subagents.enabled:
        from agent.subagents.manager import SubAgentManager
        from agent.subagents.log_store import SubAgentLogStore
        from agent.subagents.runtime import SubAgentRuntime
        from agent.tools.subagent_logs import build_subagent_log_tool
        # Rebuilds occur at a safe boundary; keep lifetime accounting on the
        # parent controller, which survives model changes and session switches.
        manager = getattr(run_controller, "subagent_manager", None)
        if manager is None:
            manager = SubAgentManager(run_controller, cfg.subagents.max_concurrent)
            run_controller.subagent_manager = manager
        manager.max_concurrent = cfg.subagents.max_concurrent
        root = (cfg.config_dir or Path.home() / ".deep-agent") / "subagents"
        store = SubAgentLogStore(root, tuple(p.api_key for p in cfg.llm_profiles) + (spec.web_search_api_key or "",))
        tools.append(build_subagent_log_tool(store))

        def prepare_child(controller: RunController) -> PreparedAgent:
            if execution_mode is not ExecutionMode.SANDBOXED:
                raise ValueError("Subagents require a verified SANDBOXED backend for allow permission")
            effort = cfg.subagents.reasoning_effort
            effort = None if effort in (None, "default") else effort
            if cfg.subagents.model is not None or isinstance(model, ProtocolChatOpenAI):
                profile = cfg.get_profile(cfg.subagents.model or getattr(model, "_deep_agent_model_id", cfg.llm_default))
                # Each task owns a fresh client. Never inherit the parent's
                # configured effort, including when it uses the same profile.
                child_model = build_chat_model(
                    profile, streaming=True, trust_env=cfg.trust_env, timeout=cfg.request_timeout,
                    reasoning_effort=effort,
                )
            else:
                # Injected custom models (including scripted test doubles) have
                # no general profile-based reconstruction contract. Keep the
                # supplied object instead of silently selecting cfg.llm_default.
                if effort is not None:
                    raise ValueError("A custom BaseChatModel requires subagents.model to override reasoning_effort")
                child_model = model
            child_spec = replace(
                spec, role="subagent", tools=(), backend=fs_backend,
                skills=tuple(skill_sources or ()),
                hidden_tools=tuple(set(spec.hidden_tools) | {"task", "request_human_input", "read_subagent_log"}),
            )
            try:
                child = build_agent(child_spec, child_model, PermissionMode.ALLOW, None,
                                    controller, lambda: False, backend_selection=selected, settings=cfg)
            except BaseException:
                if child_model is not model:
                    from agent.llm_stream import close_model_http_clients
                    close_model_http_clients(child_model)
                raise
            child.owns_model_clients = child_model is not model
            return child

        runtime = SubAgentRuntime(manager, store, prepare_child,
                                  cfg.subagents.model or str(get_model_identifier(model) or type(model).__name__))
        subagents.append({
            "name": "general-purpose",
            "description": (
                "Only delegate when the problem can be decomposed into independent "
                "subproblems that do not interfere with one another. "
                "Complete an isolated coding or research task. "
                "Only the final result is returned. "
                "No MCP, user interaction, or recursive delegation. "
                "Maximum concurrent tasks is configured by the host."
            ),
            "runnable": runtime,
            "mode": "isolated",
        })
    tools = [tool for tool in tools if tool.name not in spec.hidden_tools]
    try:
        graph = create_deep_agent(
            model=model,
            subagents=subagents,
            tools=tools,
            system_prompt=prompt,
            middleware=[
                RuntimeConfigGateMiddleware(run_controller.has_pending_config),
                PauseGateMiddleware(lambda: run_controller.pause_requested or should_pause()),
                RecoveryContextMiddleware(),
                EnvironmentContextMiddleware(),
                CurrentTimeMiddleware(cfg.ui_timezone),
                UsageIdentityMiddleware(),
                SteeringMiddleware(run_controller),
                ToolCancelMiddleware(run_controller),
                ToolArgHintMiddleware(),
                WriteOperationMiddleware(fs_backend),
                AttachmentMaterializationMiddleware(),
                WorkspaceFilesystemMiddleware(backend=fs_backend, tools=filesystem_tools),
                compact_middleware,
                *([MCPMiddleware(mcp, mcp_tools)] if include_mcp else []),
                *([HiddenToolsMiddleware(spec.hidden_tools)] if spec.hidden_tools else []),
            ],
            skills=skill_sources,
            backend=fs_backend,
            interrupt_on=hitl or None,
            checkpointer=saver,
            name="deep-agent-template",
        )
        prepared = PreparedAgent(
            graph=graph,
            backend=fs_backend,
            model=model,
            spec=spec,
            interrupt_on=hitl,
            system_prompt=prompt,
            model_tool_names=tuple(
                name for name in graph.nodes["tools"].bound.tools_by_name if name not in spec.hidden_tools
            ),
            exposed_tool_names=[tool.name for tool in tools if tool.name != "request_human_input"],
            filesystem_tools=[name for name in filesystem_tools if name not in spec.hidden_tools],
            execution_mode=execution_mode,
            security_warning=security_warning,
            checkpointer=saver,
            run_controller=run_controller,
            pause_condition=should_pause,
            compact_middleware=compact_middleware,
            backend_selection=selected,
            sandbox_pool=sandbox_pool,
            skill_sources=tuple(skill_sources or ()),
            subagent_manager=manager,
        )

        if mcp is not None and mcp_revision is not None:
            mcp.commit(mcp_revision)
        return prepared
    except BaseException:
        if mcp is not None and mcp_revision is not None:
            mcp.discard(mcp_revision)
        raise


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
    mcp: Any | None = None,
    role: str = "main",
    backend_selection: BackendSelection | None = None,
) -> PreparedAgent:
    cfg = settings or Settings()
    if role not in {"main", "subagent"}:
        raise ValueError("Unknown agent role")
    spec = AgentSpec(
        role=role, settings=cfg,
        trust_env=cfg.trust_env,
        instructions=instructions if instructions is not None else cfg.agent_instructions,
        tools=tuple(extra_tools or ()),
        hidden_tools=cfg.hidden_tools,
        skills=tuple(skills) if skills is not None else None,
        backend=backend,
        sandbox=sandbox_config or cfg.sandbox,
        web_search_api_key=os.environ.get("TAVILY_API_KEY") or cfg.tavily_api_key,
    )
    if role == "subagent":
        interrupt_on = {}
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
        mcp=mcp if role == "main" else None,
        backend_selection=backend_selection,
    )


def _default_model(cfg: Settings) -> BaseChatModel:
    return build_chat_model(
        cfg.active_profile, streaming=True, trust_env=cfg.trust_env, timeout=cfg.request_timeout,
    )


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
