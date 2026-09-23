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
from deepagents.middleware.filesystem import FilesystemMiddleware, FilesystemPermission
from deepagents._models import get_model_identifier, get_model_provider
from langchain.agents.middleware import ModelRetryMiddleware, TodoListMiddleware, ToolRetryMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from agent.config import SandboxConfig, Settings, settings as default_settings
from agent.control import RunController
from agent.llm import build_chat_model
from agent.middleware.cancel_tools import ToolCancelMiddleware
from agent.middleware.attachments import AttachmentMaterializationMiddleware
from agent.middleware.network_gate import NetworkGateMiddleware
from agent.middleware.pause import PauseGateMiddleware
from agent.middleware.retry import retry_on_transient
from agent.middleware.steering import SteeringMiddleware
from agent.permission import PermissionMode, interrupt_on_for_mode, permission_mode_from_interrupt_on
from agent.sandbox import ExecutionMode, SANDBOX_ROOT, select_backend
from agent.tools.examples import build_example_tools
from agent.tools.execute import build_execute_tool
from agent.tools.human_input import build_human_input_tools

DEFAULT_FS_TOOLS = ["ls", "read_file", "glob", "grep", "write_file", "edit_file", "delete"]
DEFAULT_SYSTEM_PROMPT = "你是使用 {model_name} 的 Coding Agent CLI。"


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
) -> PreparedAgent:
    sandbox_cfg = spec.sandbox or default_settings.sandbox
    if spec.backend is None:
        selected = select_backend(sandbox_cfg)
        fs_backend = selected.backend
        execution_mode = selected.mode
        security_warning = selected.warning
    else:
        fs_backend = spec.backend
        execution_mode = ExecutionMode.CUSTOM
        security_warning = ""
    _disable_general_purpose_task(model)
    prompt = compose_system_prompt(spec, model, sandbox_cfg.workspace)
    tools = [*build_example_tools(), *build_human_input_tools(), *spec.tools]
    hitl = interrupt_on_for_mode(permission) or {}
    filesystem_tools = list(DEFAULT_FS_TOOLS)
    supports_execute = isinstance(fs_backend, SandboxBackendProtocol)
    permissions = [] if supports_execute else _filesystem_permissions(sandbox_cfg, managed=spec.backend is None)
    if supports_execute:
        tools.append(build_execute_tool(fs_backend))
    skill_sources = list(spec.skills) if spec.skills is not None else _default_skill_sources(sandbox_cfg.workspace)
    graph = create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=prompt,
        middleware=[
            PauseGateMiddleware(should_pause),
            SteeringMiddleware(run_controller),
            ToolCancelMiddleware(run_controller),
            NetworkGateMiddleware(),
            AttachmentMaterializationMiddleware(),
            ModelRetryMiddleware(max_retries=2, retry_on=retry_on_transient, on_failure="error"),
            ToolRetryMiddleware(max_retries=2, retry_on=retry_on_transient, on_failure="continue"),
            TodoListMiddleware(),
            FilesystemMiddleware(backend=fs_backend, tools=filesystem_tools, _permissions=permissions),  # type: ignore[arg-type]
        ],
        skills=skill_sources,
        permissions=permissions or None,
        backend=fs_backend,
        interrupt_on=hitl or None,
        checkpointer=checkpointer if checkpointer is not None else InMemorySaver(),
        name="deep-agent-template",
    )
    return PreparedAgent(
        graph=graph,
        backend=fs_backend,
        model=model,
        spec=spec,
        interrupt_on=hitl,
        system_prompt=prompt,
        exposed_tool_names=[tool.name for tool in tools if tool.name not in {"handoff_to_human", "request_human_input"}],
        filesystem_tools=filesystem_tools,
        execution_mode=execution_mode,
        security_warning=security_warning,
    )


def create_agent(
    *,
    model: BaseChatModel | None = None,
    checkpointer: Any | None = None,
    should_pause: Callable[[], bool] | None = None,
    system_prompt: str | None = None,
    extra_tools: list[BaseTool] | None = None,
    interrupt_on: dict[str, Any] | None = None,
    skills: list[str] | None = None,
    settings: Settings | None = None,
    backend: BackendProtocol | None = None,
    sandbox_config: SandboxConfig | None = None,
    run_controller: RunController | None = None,
) -> PreparedAgent:
    cfg = settings or default_settings
    spec = AgentSpec(
        instructions=system_prompt,
        tools=tuple(extra_tools or ()),
        skills=tuple(skills) if skills is not None else None,
        backend=backend,
        sandbox=sandbox_config or cfg.sandbox,
    )
    permission = permission_mode_from_interrupt_on(interrupt_on) if interrupt_on is not None else PermissionMode.ASK
    return build_agent(
        spec, model or _default_model(cfg), permission, checkpointer,
        run_controller or RunController(), should_pause or (lambda: False),
    )


def _default_model(cfg: Settings) -> BaseChatModel:
    return build_chat_model(cfg.active_profile, streaming=True)


def _default_skill_sources(workspace: Path) -> list[str] | None:
    workspace_skills = workspace.expanduser().resolve() / "skills"
    if not workspace_skills.is_dir():
        return None
    return [f"{SANDBOX_ROOT}/skills/"]


def _filesystem_permissions(config: SandboxConfig, *, managed: bool) -> list[FilesystemPermission]:
    permissions: list[FilesystemPermission] = []
    for relative in config.protected_workspace_paths:
        clean = relative.strip("/")
        permissions.append(FilesystemPermission(
            operations=["write"],
            paths=[f"{SANDBOX_ROOT}/{clean}", f"{SANDBOX_ROOT}/{clean}/**"],
            mode="deny",
        ))
    if managed:
        permissions.extend([
            FilesystemPermission(
                operations=["read", "write"],
                paths=[SANDBOX_ROOT, f"{SANDBOX_ROOT}/**"],
                mode="allow",
            ),
            FilesystemPermission(operations=["read", "write"], paths=["/**"], mode="deny"),
        ])
    return permissions


_NO_GP = HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False))


def _disable_general_purpose_task(model: BaseChatModel) -> None:
    register_harness_profile("openai", _NO_GP)
    provider = get_model_provider(model)
    identifier = get_model_identifier(model)
    if provider:
        register_harness_profile(provider, _NO_GP)
        if identifier and ":" not in identifier:
            register_harness_profile(f"{provider}:{identifier}", _NO_GP)
    if identifier and ":" in identifier:
        register_harness_profile(identifier, _NO_GP)
