from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends.protocol import BackendProtocol, SandboxBackendProtocol
from deepagents.middleware.skills import SKILLS_SYSTEM_PROMPT, SkillsMiddleware
from deepagents.middleware.summarization import (
    SummarizationMiddleware,
    SummarizationToolMiddleware,
    compute_summarization_defaults,
)
from deepagents._models import get_model_identifier, get_model_provider
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from agent.bootstrap.config import Settings
from agent.prompts import read_prompt
from agent.execution.policy import SandboxConfig
from agent.redaction import known_secret_refs
from agent.runtime.control import RunController
from agent.execution.environment import EnvironmentContextMiddleware
from agent.models.client import ProtocolChatOpenAI, build_chat_model
from agent.middleware.cancel_tools import ToolCancelMiddleware
from agent.middleware.current_time import CurrentTimeMiddleware
from agent.middleware.attachments import AttachmentMaterializationMiddleware
from agent.middleware.payload_recovery import PayloadRecoveryMiddleware
from agent.middleware.pause import PauseGateMiddleware
from agent.middleware.usage import UsageIdentityMiddleware
from agent.middleware.recovery import RecoveryContextMiddleware
from agent.middleware.steering import SteeringMiddleware
from agent.middleware.tool_arg_hints import ToolArgHintMiddleware
from agent.middleware.write_operation import WriteOperationMiddleware
from agent.middleware.workspace_filesystem import WorkspaceFilesystemMiddleware
from agent.runtime.permissions import (
    AUTO_FALLBACK_REASON,
    PermissionMode,
    allow_mode_unavailable_reason,
    interrupt_on_for_mode,
    permission_mode_from_interrupt_on,
    permission_unavailable_reason,
)
from agent.tools.catalog import (
    FILE_MUTATION_TOOLS,
    FILESYSTEM_TOOL_NAMES,
    ROOT_TASK_TOOLS,
    SYNC_TASK_TOOLS,
    NodeKind,
    apply_runtime_limits,
    node_tools,
)
from agent.execution.backend import BackendSelection, sandbox_pool_of, select_backend
from agent.execution.filesystem import WorkspaceCompositeBackend
from agent.execution.ports import supports_interactive_execution
from agent.execution.types import ExecutionMode, SKILLS_ROOT
from agent.execution.pool import SandboxPool
from agent.tools.execute import build_execute_tool, build_write_stdin_tool
from agent.tools.human_input import build_human_input_tools
from agent.middleware.runtime_config import RuntimeConfigGateMiddleware
from agent.tools.web import build_web_fetch_tool, build_web_search_tool

# Assembly entry: create_deep_agent() only. Do not fork Deep Agents core.
# Middleware augments the Deep Agents defaults; create_deep_agent remains the only assembly entry.


DEFAULT_FS_TOOLS = list(FILESYSTEM_TOOL_NAMES)

_PROMPT_DIR = Path(__file__).parent / "prompts"


class WorkspaceSummarizationMiddleware(SummarizationMiddleware):
    def _truncate_args(
        self, messages: list[AnyMessage], total_tokens: int,
    ) -> tuple[list[AnyMessage], bool]:
        """Keep upstream argument truncation for the renamed write and edit tools."""
        if not self._should_truncate_args(messages, total_tokens):
            return messages, False
        cutoff = self._determine_truncate_cutoff_index(messages)
        if cutoff >= len(messages):
            return messages, False
        updated = list(messages)
        modified = False
        for index, message in enumerate(messages[:cutoff]):
            if not isinstance(message, AIMessage):
                continue
            calls = [
                self._truncate_tool_call(call)
                if call["name"] in FILE_MUTATION_TOOLS and call["name"] != "delete"
                else call
                for call in message.tool_calls
            ]
            if calls != message.tool_calls:
                updated[index] = message.model_copy(update={"tool_calls": calls})
                modified = True
        return (updated, True) if modified else (messages, False)


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
    deepseek_api_key: str | None = field(default=None, repr=False)
    hidden_tools: tuple[str, ...] = ()
    trust_env: bool = False
    role: str = "main"
    # Root = 0; each start_task child is parent depth + 1.
    depth: int = 0
    settings: Settings | None = field(default=None, repr=False)


@dataclass
class PreparedAgent:
    graph: Any
    backend: BackendProtocol
    model: BaseChatModel | None = None
    spec: AgentSpec = field(default_factory=AgentSpec)
    interrupt_on: dict[str, Any] = field(default_factory=dict)
    permission_mode: PermissionMode | None = None
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
    # Ask mounted the Auto replay guard for the thread it was built for.
    approval_guard: bool = False


def compose_system_prompt(spec: AgentSpec, workspace: Path) -> str:
    sections = [read_prompt(_PROMPT_DIR / "system.md")]
    if spec.role == "subagent":
        sections.append(read_prompt(_PROMPT_DIR / "subagent.md"))
    if spec.instructions and spec.instructions.strip():
        sections.append(f"## User instructions\n{spec.instructions.strip()}")
    project_instructions = workspace.expanduser().resolve() / "AGENTS.md"
    if project_instructions.is_file():
        sections.append(f"## Project instructions\n{project_instructions.read_text(encoding='utf-8')}")
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
    approval_journal=None,
    thread_id: str | None = None,
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
    auto_block = permission_unavailable_reason(
        PermissionMode.AUTO,
        execution_mode=execution_mode,
        has_deepseek_key=bool(spec.deepseek_api_key),
    )
    if permission is PermissionMode.AUTO and auto_block is not None:
        permission = PermissionMode.ASK
        security_warning = AUTO_FALLBACK_REASON
    if permission is PermissionMode.ALLOW and execution_mode is not ExecutionMode.SANDBOXED:
        raise ValueError(allow_mode_unavailable_reason(execution_mode))
    sandbox_pool = sandbox_pool_of(fs_backend)
    _disable_general_purpose_task(model)
    prompt = compose_system_prompt(spec, sandbox_cfg.workspace)
    cfg = settings or spec.settings or Settings()
    kind = _node_kind(spec, run_controller, cfg)
    allowed = apply_runtime_limits(
        node_tools(kind),
        subagents_enabled=cfg.subagents.enabled,
        can_launch=permission is not PermissionMode.ASK,
        hidden=spec.hidden_tools,
    )
    tools: list[Any] = []
    if "request_human_input" in allowed:
        tools.extend(build_human_input_tools())
    tools.extend(tool for tool in spec.tools if tool.name not in spec.hidden_tools)
    if spec.deepseek_api_key:
        if "web_search" in allowed:
            tools.append(build_web_search_tool(spec.deepseek_api_key, trust_env=spec.trust_env))
        if "web_fetch" in allowed:
            tools.append(build_web_fetch_tool(spec.deepseek_api_key, trust_env=spec.trust_env))
    hitl = dict(interrupt_on_override) if interrupt_on_override is not None else (interrupt_on_for_mode(permission) or {})
    # File tools are common to every node. The middleware requires the full list;
    # hidden_tools removes names from the model after that.
    filesystem_tools = list(DEFAULT_FS_TOOLS)
    supports_execute = isinstance(fs_backend, SandboxBackendProtocol)
    if supports_execute and "execute" in allowed:
        tools.append(build_execute_tool(fs_backend, permission=permission))
        if supports_interactive_execution(fs_backend) and "write_stdin" in allowed:
            tools.append(build_write_stdin_tool(fs_backend))
    skill_sources = list(spec.skills) if spec.skills is not None else (
        _default_skill_sources(fs_backend) if spec.backend is None else None
    )
    saver = checkpointer if checkpointer is not None else InMemorySaver()
    summary_defaults = compute_summarization_defaults(model)
    trigger = summary_defaults["trigger"]
    keep = summary_defaults["keep"]
    if trigger[0] == "fraction":
        trigger = ("fraction", cfg.effective_compaction_fraction(trigger[1]))
        if keep[0] == "fraction" and keep[1] >= trigger[1]:
            keep = ("fraction", trigger[1] / 2)
    summarization = WorkspaceSummarizationMiddleware(
        model=model, backend=fs_backend, trigger=trigger,
        keep=keep, trim_tokens_to_summarize=None,
        truncate_args_settings=summary_defaults["truncate_args_settings"],
    )
    compact_middleware = SummarizationToolMiddleware(summarization)
    mcp_revision: int | None = None
    mcp_tools: list[Any] = []
    include_mcp = False
    if mcp is not None and kind == "root":
        from agent.mcp.middleware import MCPMiddleware, MCP_HOST_PROMPT
        from agent.mcp.names import host_interrupt_on

        mcp_revision, mcp_tools = mcp.snapshot()
        include_mcp = bool(mcp.snapshot_includes_mcp(mcp_revision))
        if include_mcp:
            if permission is not PermissionMode.ALLOW:
                hitl.update(host_interrupt_on(tool.name for tool in mcp_tools))
            prompt = f"{prompt}\n\n{MCP_HOST_PROMPT}"
    auto_middleware = []
    from agent.runtime.auto_approval import ApprovalJournal, AutoApprovalMiddleware
    # An interrupted Auto batch can be reopened after its key or sandbox became
    # unavailable. Preserve the original human decision indices and block its
    # pending automatic grants; subsequent turns follow the regular Ask rules.
    # Only the thread that recorded Auto decisions needs this guard under Ask.
    approval_guard = permission is PermissionMode.ASK and bool(thread_id) and ApprovalJournal.has_thread(saver, thread_id)
    if permission is PermissionMode.AUTO or approval_guard:
        if approval_journal is None:
            approval_journal = getattr(saver, "_auto_approval_journal", None)
        if approval_journal is None:
            approval_journal = ApprovalJournal(saver)
            saver._auto_approval_journal = approval_journal
        ask_only = permission is PermissionMode.ASK
        auto_middleware.append(AutoApprovalMiddleware(
            spec=spec, backend=fs_backend, controller=run_controller, journal=approval_journal,
            trusted=mcp.snapshot_trusted_tools(mcp_revision) if include_mcp and not ask_only else (),
            # Auto never gates less than Ask does, whatever override the caller passed.
            gated=hitl if ask_only else {**(interrupt_on_for_mode(PermissionMode.ASK) or {}), **hitl},
            ask_only=ask_only,
        ))
    manager = None
    task_names = allowed & (ROOT_TASK_TOOLS | SYNC_TASK_TOOLS)
    # Only a node whose set includes task tools gets a runtime. Async and
    # max-depth nodes stay on the common tools.
    if task_names:
        from agent.subagents.manager import SubAgentManager
        from agent.subagents.log_store import SubAgentLogStore
        from agent.subagents.runtime import SubAgentRuntime
        from agent.tools.subagent_tasks import build_subagent_tools
        # Rebuilds occur at a safe boundary; keep lifetime accounting on the
        # parent controller, which survives model changes and session switches.
        # Nested agents reuse the root manager (set on the child controller in
        # prepare_child) so concurrency stays global for the whole tree.
        manager = getattr(run_controller, "subagent_manager", None)
        if manager is None:
            manager = SubAgentManager(
                run_controller,
                max_concurrent=cfg.subagents.max_concurrent,
                max_total=cfg.subagents.max_total,
            )
            run_controller.subagent_manager = manager
        manager.set_limits(
            max_concurrent=cfg.subagents.max_concurrent,
            max_total=cfg.subagents.max_total,
        )
        if manager.log_store is None:
            root = (cfg.config_dir or Path.home() / ".deep-agent") / "subagents"
            base_refs = known_secret_refs(cfg, spec.deepseek_api_key)
            provider = getattr(run_controller, "secret_refs_provider", None)
            if callable(provider):
                def _subagent_secret_refs(_base=base_refs, _provider=provider):
                    return (*_base, *_provider())
                manager.log_store = SubAgentLogStore(root, _subagent_secret_refs)
            else:
                manager.log_store = SubAgentLogStore(root, base_refs)
        store = manager.log_store

        def prepare_child(controller: RunController) -> PreparedAgent:
            if execution_mode is not ExecutionMode.SANDBOXED or permission is PermissionMode.ASK:
                raise ValueError("Subagents require a verified SANDBOXED backend and auto or allow permission")
            effort = cfg.subagents.reasoning_effort
            effort = None if effort in (None, "default") else effort
            if cfg.subagents.model is not None or isinstance(model, ProtocolChatOpenAI):
                profile = cfg.get_profile(cfg.subagents.model or getattr(model, "_deep_agent_model_id", cfg.llm_default))
                # Each task owns a fresh client and its own run_id session, so
                # concurrent SubAgents never share a session header value.
                child_model = build_chat_model(
                    profile, streaming=True, trust_env=cfg.trust_env, timeout=cfg.request_timeout,
                    reasoning_effort=effort, session_id=controller.session_id or None,
                )
            else:
                # Injected custom models (including scripted test doubles) have
                # no general profile-based reconstruction contract. Keep the
                # supplied object instead of silently selecting cfg.llm_default.
                if effort is not None:
                    raise ValueError("A custom BaseChatModel requires subagents.model to override reasoning_effort")
                child_model = model
            child_depth = spec.depth + 1
            child_spec = replace(
                spec, role="subagent", depth=child_depth, tools=(), backend=fs_backend,
                skills=tuple(skill_sources or ()),
                hidden_tools=spec.hidden_tools,
            )
            # Share the tree's concurrency manager; cancel chains use owner controllers.
            controller.subagent_manager = manager
            provider = getattr(run_controller, "secret_refs_provider", None)
            if callable(provider):
                controller.secret_refs_provider = provider
            try:
                # The child runs under the parent's permission mode; it never gains more than the parent has.
                child = build_agent(child_spec, child_model, permission, None, controller, lambda: False,
                                    backend_selection=selected, settings=cfg, approval_journal=approval_journal)
            except BaseException:
                if child_model is not model:
                    from agent.models.stream_lifecycle import close_model_http_clients
                    close_model_http_clients(child_model)
                raise
            child.owns_model_clients = child_model is not model
            return child

        runtime = SubAgentRuntime(
            manager, store, prepare_child,
            cfg.subagents.model or str(get_model_identifier(model) or type(model).__name__),
            cfg.subagents.reasoning_effort or "default",
            child_depth=spec.depth + 1,
            owner_controller=run_controller,
        )
        tools.extend(build_subagent_tools(
            runtime, manager, store,
            names=task_names,
            allow_background=kind == "root" and bool(cfg.subagents.background),
        ))
    tools = [tool for tool in tools if tool.name not in spec.hidden_tools]
    try:
        graph = create_deep_agent(
            model=model,
            tools=tools,
            system_prompt=prompt,
            middleware=[
                *auto_middleware,
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
                PayloadRecoveryMiddleware(),
                WorkspaceFilesystemMiddleware(backend=fs_backend, tools=filesystem_tools),
                *([SkillsMiddleware(
                    backend=fs_backend, sources=skill_sources,
                    system_prompt=SKILLS_SYSTEM_PROMPT.replace("read_file", "read"),
                )] if skill_sources else []),
                summarization,
                compact_middleware,
                *([MCPMiddleware(mcp, mcp_tools)] if include_mcp else []),
                *([HiddenToolsMiddleware(spec.hidden_tools)] if spec.hidden_tools else []),
            ],
            skills=skill_sources,
            backend=fs_backend,
            interrupt_on=None if auto_middleware else hitl or None,
            checkpointer=saver,
            name="deep-agent-template",
        )
        prepared = PreparedAgent(
            graph=graph,
            backend=fs_backend,
            model=model,
            spec=spec,
            permission_mode=permission,
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
            approval_guard=approval_guard,
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
    permission_mode: PermissionMode | str | None = None,
    backend_selection: BackendSelection | None = None,
    thread_id: str | None = None,
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
        deepseek_api_key=cfg.resolved_deepseek_api_key,
    )
    if role == "subagent":
        interrupt_on = {}
    permission = permission_mode_from_interrupt_on(interrupt_on) if interrupt_on is not None else PermissionMode.ASK
    if permission_mode is not None:
        permission = PermissionMode(permission_mode)
        if interrupt_on is not None and role != "subagent":
            raise ValueError("Specify permission_mode or interrupt_on")
    effective_interrupt_on = interrupt_on
    if interrupt_on:
        defaults = interrupt_on_for_mode(PermissionMode.ASK) or {}
        for name, rule in defaults.items():
            if name in interrupt_on and interrupt_on[name] != rule:
                raise ValueError(f"Cannot override the default approval rule for {name}")
        effective_interrupt_on = {**defaults, **interrupt_on}
    return build_agent(
        spec, model or _default_model(cfg, session_id=thread_id), permission, checkpointer,
        run_controller or RunController(), should_pause or (lambda: False),
        interrupt_on_override=effective_interrupt_on,
        mcp=mcp if role == "main" else None,
        backend_selection=backend_selection,
        thread_id=thread_id,
    )


def _default_model(cfg: Settings, *, session_id: str | None = None) -> BaseChatModel:
    return build_chat_model(
        cfg.active_profile, streaming=True, trust_env=cfg.trust_env, timeout=cfg.request_timeout,
        session_id=session_id,
    )


def _default_skill_sources(backend: BackendProtocol) -> list[str] | None:
    if isinstance(backend, WorkspaceCompositeBackend) and backend.skills_dir is not None:
        return [f"{SKILLS_ROOT}/"]
    return None


_NO_GP = HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False))


def _node_kind(spec: AgentSpec, controller: RunController, cfg: Settings) -> NodeKind:
    """Which catalog set this agent is allowed to draw from."""
    if getattr(controller, "background_leaf", False):
        return "async"
    if spec.role != "main" and not cfg.subagents.may_create_subagent(spec.depth):
        return "max_depth"
    if spec.role == "main":
        return "root"
    return "sync"


def _disable_general_purpose_task(model: BaseChatModel) -> None:
    provider = get_model_provider(model)
    identifier = get_model_identifier(model)
    if identifier:
        key = identifier if ":" in identifier or not provider else f"{provider}:{identifier}"
        register_harness_profile(key, _NO_GP)
    elif provider:
        # Models without an identifier cannot be targeted more narrowly upstream.
        register_harness_profile(provider, _NO_GP)
