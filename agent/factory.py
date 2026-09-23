# Assembly entry: create_deep_agent() only. Do not fork Deep Agents core.
# Middleware order matches the platform Native Agent:
# PauseGate → ModelRetry → ToolRetry → TodoList → Filesystem.
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import StateBackend
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
from agent.middleware.network_gate import NetworkGateMiddleware
from agent.middleware.pause import PauseGateMiddleware
from agent.middleware.retry import retry_on_transient
from agent.middleware.steering import SteeringMiddleware
from agent.sandbox import ExecutionMode, SANDBOX_ROOT, UNSANDBOXED_WARNING, select_backend
from agent.tools.examples import CONFIRM_INTERRUPT_ON, build_example_tools
from agent.tools.execute import build_execute_tool
from agent.tools.human_input import build_human_input_tools

DEFAULT_FS_TOOLS = ["ls", "read_file", "glob", "grep", "write_file", "edit_file", "delete"]
DEFAULT_SYSTEM_PROMPT = """你是一个可本地运行的 Deep Agent。

使用文件系统工具读写 /workspace 下的文件。
lookup_docs 可直接执行；send_email 需要人工确认后才会发送。
缺少业务判断时调用 request_human_input 或 handoff_to_human，不要猜测。
"""


@dataclass
class PreparedAgent:
    graph: Any
    backend: BackendProtocol
    files: dict[str, str] = field(default_factory=dict)
    interrupt_on: dict[str, Any] = field(default_factory=dict)
    system_prompt: str = ""
    exposed_tool_names: list[str] = field(default_factory=list)
    filesystem_tools: list[str] = field(default_factory=list)
    execution_mode: ExecutionMode = ExecutionMode.CUSTOM
    security_warning: str = ""

    def files_for_state(self) -> dict[str, str]:
        return self.files if isinstance(self.backend, StateBackend) else {}


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
    files: dict[str, str] | None = None,
    sandbox_config: SandboxConfig | None = None,
    run_controller: RunController | None = None,
) -> PreparedAgent:
    cfg = settings or default_settings
    sandbox_cfg = sandbox_config or cfg.sandbox
    if backend is None:
        selected = select_backend(sandbox_cfg)
        fs_backend = selected.backend
        execution_mode = selected.mode
        security_warning = selected.warning
    else:
        fs_backend = backend
        execution_mode = ExecutionMode.CUSTOM
        security_warning = ""
    prompt = system_prompt if system_prompt is not None else DEFAULT_SYSTEM_PROMPT
    chat_model = model or _default_model(cfg)
    _disable_general_purpose_task(chat_model)

    tools = [*build_example_tools(), *build_human_input_tools()]
    if extra_tools:
        tools.extend(extra_tools)

    hitl: dict[str, Any]
    if interrupt_on is not None:
        # Explicit mapping fully replaces the default CONFIRM set (including {}).
        hitl = dict(interrupt_on)
    else:
        hitl = dict(CONFIRM_INTERRUPT_ON)

    filesystem_tools = list(DEFAULT_FS_TOOLS)
    supports_execute = isinstance(fs_backend, SandboxBackendProtocol)
    permissions = [] if supports_execute else _filesystem_permissions(sandbox_cfg, managed=backend is None)
    if supports_execute:
        tools.append(build_execute_tool(fs_backend))
        if system_prompt is None:
            prompt = (
                f"{prompt}\n使用 execute 在 /workspace 中执行命令；默认无网络。"
                "仅当需要联网时设置 network=true（permission ask 下会先审批）。"
                "文件系统和 shell 使用相同的路径。"
            )
    if execution_mode is ExecutionMode.UNSANDBOXED:
        prompt = f"{prompt}\n\n安全状态：{UNSANDBOXED_WARNING}"
    pause_check = should_pause or (lambda: False)
    controller = run_controller or RunController()
    skill_sources = skills if skills is not None else _default_skill_sources(sandbox_cfg.workspace)
    seeded = files if files is not None else {
        "/workspace/README.md": "# Workspace\n\nLocal Deep Agent template workspace.\n",
    }

    graph = create_deep_agent(
        model=chat_model,
        tools=tools,
        system_prompt=prompt,
        middleware=[
            PauseGateMiddleware(pause_check),
            SteeringMiddleware(controller),
            ToolCancelMiddleware(controller),
            NetworkGateMiddleware(),
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
        files=seeded,
        interrupt_on=hitl,
        system_prompt=prompt,
        exposed_tool_names=[
            tool.name for tool in tools
            if tool.name not in {"handoff_to_human", "request_human_input"}
        ],
        filesystem_tools=filesystem_tools,
        execution_mode=execution_mode,
        security_warning=security_warning,
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


def state_file(content: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {"content": content, "encoding": "utf-8", "created_at": now, "modified_at": now}


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
