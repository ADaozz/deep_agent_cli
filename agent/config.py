from __future__ import annotations

from dataclasses import dataclass, field, replace
import os
import re
from pathlib import Path
from typing import Any, Literal, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

SAFE_INHERITED_ENV = (
    "LANG",
    "TERM",
    "COLORTERM",
    "NO_COLOR",
    "TZ",
)

_DEFAULT_CONFIG_NAME = "config.yaml"
DEFAULT_UI_TIMEZONE = "Asia/Shanghai"
DEFAULT_STREAM_CHUNK_TIMEOUT = 120.0
DEFAULT_REQUEST_TIMEOUT = 60.0


class ConfigError(ValueError):
    """A user configuration error safe to display without a traceback."""


def default_skills_dir() -> Path:
    return Path.home() / ".deep-agent" / "skills"


@dataclass(frozen=True)
class BindMount:
    """An explicit host-to-sandbox mount.

    ``modes`` is ``None`` when the mount applies in both filesystem modes.
    That is the historical behavior for entries that omit the field.
    """

    source: Path
    destination: str
    optional: bool = False
    dev_bind: bool = False
    modes: tuple["SandboxMode", ...] | None = None


@dataclass(frozen=True)
class ModelProfile:
    """Named OpenAI-compatible chat model profile."""

    id: str
    model: str
    api_key: str = "sk-local"
    base_url: str = "http://localhost:8000/v1"
    input: tuple["InputKind", ...] = ("text",)
    api: Literal["responses", "chat_completions"] = "responses"
    # 0 = unknown, so the UI can hide the context meter instead of guessing.
    context_window: int = 0
    source: str = ""
    reasoning_efforts: tuple[str, ...] = ()
    max_retries: int = 5
    stream_chunk_timeout: float = DEFAULT_STREAM_CHUNK_TIMEOUT

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_retries", _model_max_retries(self.max_retries, field_name="max_retries"))
        object.__setattr__(
            self, "stream_chunk_timeout",
            _model_stream_chunk_timeout(self.stream_chunk_timeout, field_name="stream_chunk_timeout"),
        )

    def supports_input(self, kind: "InputKind") -> bool:
        return kind in self.input


InputKind = Literal["text", "image"]
_INPUT_KINDS = frozenset({"text", "image"})


SandboxMode = Literal["restricted", "full-access"]
_SANDBOX_MODES = frozenset({"restricted", "full-access"})


def parse_sandbox_mode(value: str) -> SandboxMode | None:
    key = value.strip().lower()
    if key == "restricted":
        return "restricted"
    if key == "full-access":
        return "full-access"
    return None


@dataclass(frozen=True)
class SandboxConfig:
    """Bubblewrap policy. ``restricted`` is the default filesystem boundary."""

    workspace: Path = field(default_factory=Path.cwd)
    bwrap_path: str = "bwrap"
    mode: SandboxMode = "restricted"
    timeout_seconds: int | None = None
    max_output_bytes: int = 100_000
    env_allowlist: tuple[str, ...] = ()
    env_set: dict[str, str] = field(default_factory=dict)
    extra_read_only_mounts: tuple[BindMount, ...] = ()
    extra_read_write_mounts: tuple[BindMount, ...] = ()
    mount_skills: bool = True
    skills_dir: Path | None = None
    protected_config_dir: Path | None = None
    protected_files: tuple[Path, ...] = ()
    trust_env: bool = False

    def __post_init__(self) -> None:
        if type(self.mount_skills) is not bool:
            raise ValueError("sandbox.mount_skills must be a boolean")
        if self.mode not in _SANDBOX_MODES:
            raise ValueError(
                f"sandbox.mode must be restricted or full-access, got {self.mode!r}"
            )


def _default_profiles() -> tuple[ModelProfile, ...]:
    return (
        ModelProfile(
            id="default",
            model="qwen3.5-plus",
            api_key="sk-local",
            base_url="http://localhost:8000/v1",
        ),
    )


@dataclass(frozen=True)
class UiDisplayLimits:
    """Content folding limits, loaded from the main configuration's ui section."""

    head_lines: int = 4
    tail_lines: int = 4
    explore_preview_items: int = 5
    explore_failure_items: int = 3
    create_preview_items: int = 5
    editor_max_lines: int = 10
    completion_menu_lines: int = 8

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"ui.{name} must be a positive integer")

    @classmethod
    def from_mapping(cls, ui: Mapping[str, Any]) -> "UiDisplayLimits":
        removed = (
            "thinking_tail_lines", "execute_tail_lines", "tool_tail_lines", "expanded_tool_lines",
            "edit_preview_changed_lines", "write_preview_lines", "failure_preview_lines",
            "command_failure_tail_lines",
        )
        obsolete = [name for name in removed if name in ui]
        if obsolete:
            raise ValueError(f"Removed ui settings: {', '.join(obsolete)}; use ui.head_lines and ui.tail_lines")
        return cls(**{name: ui[name] for name in cls.__dataclass_fields__ if name in ui})


DEFAULT_UI_DISPLAY_LIMITS = UiDisplayLimits()


@dataclass(frozen=True)
class SubAgentConfig:
    enabled: bool = True
    max_concurrent: int = 2
    model: str | None = None
    reasoning_effort: str | None = "default"
    permission: str = "allow"

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "SubAgentConfig":
        unknown = set(values) - cls.__dataclass_fields__.keys()
        if unknown:
            raise ValueError(f"Unknown subagents settings: {', '.join(sorted(unknown))}")
        return cls(**values)

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("subagents.enabled must be a boolean")
        if type(self.max_concurrent) is not int or self.max_concurrent < 1:
            raise ValueError("subagents.max_concurrent must be a positive integer")
        if self.permission != "allow":
            raise ValueError("subagents.permission must be allow")
        for name in ("model", "reasoning_effort"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"subagents.{name} must be a non-empty string or null")
        if self.reasoning_effort is None:
            object.__setattr__(self, "reasoning_effort", "default")
        if self.reasoning_effort == "auto":
            raise ValueError("subagents.reasoning_effort auto is not supported yet")


@dataclass(frozen=True)
class Settings:
    llm_profiles: tuple[ModelProfile, ...] = field(default_factory=_default_profiles)
    llm_default: str = "default"
    subagents: SubAgentConfig = field(default_factory=SubAgentConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    state_path: Path | None = None
    config_dir: Path | None = None
    source_path: Path | None = None
    agent_instructions: str | None = None
    ui_timezone: str = DEFAULT_UI_TIMEZONE
    ui_display_limits: UiDisplayLimits = field(default_factory=UiDisplayLimits)
    hidden_tools: tuple[str, ...] = ()
    tavily_api_key: str | None = field(default=None, repr=False)
    trust_env: bool = False
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT

    def __post_init__(self) -> None:
        if not isinstance(self.trust_env, bool):
            raise ValueError("trust_env must be a boolean")
        profile = self.active_profile
        if self.subagents.model is not None:
            profile = next((p for p in self.llm_profiles if p.id == self.subagents.model), None)
            if profile is None:
                raise ValueError(f"Unknown subagents.model: {self.subagents.model}")
        effort = self.subagents.reasoning_effort
        if effort != "default" and effort not in profile.reasoning_efforts:
            raise ValueError(f"Unsupported subagents.reasoning_effort for {profile.id}: {effort}")
        object.__setattr__(self, "sandbox", replace(self.sandbox, trust_env=self.trust_env))
        object.__setattr__(
            self, "request_timeout",
            _model_request_timeout(self.request_timeout, field_name="llm.timeout"),
        )

    @property
    def llm_model(self) -> str:
        return self.active_profile.model

    @property
    def llm_api_key(self) -> str:
        return self.active_profile.api_key

    @property
    def llm_base_url(self) -> str:
        return self.active_profile.base_url

    @property
    def active_profile(self) -> ModelProfile:
        return self.get_profile(self.llm_default)

    def get_profile(self, id_or_prefix: str) -> ModelProfile:
        key = id_or_prefix.strip()
        if not key:
            raise KeyError("model id is empty")
        exact = next((item for item in self.llm_profiles if item.id == key), None)
        if exact is not None:
            return exact
        matches = [item for item in self.llm_profiles if item.id.startswith(key)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise KeyError(f"Unknown model: {id_or_prefix}")
        ids = ", ".join(item.id for item in matches)
        raise KeyError(f"Ambiguous model prefix {id_or_prefix!r}: {ids}")

    def list_profiles(self) -> tuple[ModelProfile, ...]:
        return self.llm_profiles

    @classmethod
    def load(
        cls, path: str | Path | None = None, *, base_dir: Path | None = None,
        workspace: Path | None = None,
    ) -> "Settings":
        """Load settings; an explicit workspace is applied before path safety checks."""
        resolved = resolve_config_path(path, base_dir=base_dir)
        if resolved is None:
            return cls.from_mapping({}, workspace=workspace) if workspace is not None else cls()
        try:
            data = _read_yaml(resolved)
            if "llm" not in data:
                raise ValueError("llm.models must define grouped model sources")
            return cls.from_mapping(data, base_dir=resolved.parent, source_path=resolved, workspace=workspace)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            raise ConfigError(f"{resolved}: invalid YAML syntax{location}") from exc
        except OSError as exc:
            raise ConfigError(f"{resolved}: cannot read configuration ({exc.strerror or type(exc).__name__})") from exc
        except ValueError as exc:
            raise ConfigError(f"{resolved}: {exc}") from exc

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any] | None,
        *,
        base_dir: Path | None = None,
        source_path: Path | None = None,
        workspace: Path | None = None,
    ) -> "Settings":
        raw = dict(data or {})
        trust_env = raw.get("trust_env", False)
        if not isinstance(trust_env, bool):
            raise ValueError("trust_env must be a boolean")
        root = (base_dir or Path.cwd()).expanduser().resolve()
        llm = _section(raw, "llm")
        agent = _section(raw, "agent")
        tools = _section(raw, "tools")
        hidden_tools = tools.get("hidden", [])
        if not isinstance(hidden_tools, list) or any(
            not isinstance(name, str) or not name.strip() for name in hidden_tools
        ):
            raise ValueError("tools.hidden must be a list of non-empty tool names")
        ui = _section(raw, "ui")
        paths = _section(raw, "paths")
        sandbox_raw = dict(_section(raw, "sandbox"))
        if workspace is not None:
            sandbox_raw["workspace"] = workspace.expanduser().resolve()
        web_search = _section(raw, "web_search")
        if "protected_workspace_paths" in sandbox_raw:
            raise ValueError(
                "sandbox.protected_workspace_paths was removed; move skills to "
                "~/.deep-agent/skills and use explicit read-only mounts for other resources"
            )
        if "allow_unsandboxed" in sandbox_raw:
            raise ValueError(
                "sandbox.allow_unsandboxed was removed; Bubblewrap is required"
            )
        if "dev" in sandbox_raw:
            raise ValueError(
                "sandbox.dev was removed; use extra_read_write_mounts with dev_bind: true for host device nodes"
            )
        profiles, default_id = _llm_profiles_from_mapping(llm) if "llm" in raw else (_default_profiles(), "default")
        request_timeout = _model_request_timeout(llm.get("timeout"), field_name="llm.timeout")
        instructions = agent.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise ValueError("agent.instructions must be a string or null")
        timezone_name = ui.get("timezone", DEFAULT_UI_TIMEZONE)
        display_limits = UiDisplayLimits.from_mapping(ui)
        if not isinstance(timezone_name, str) or not timezone_name.strip():
            raise ValueError("ui.timezone must be a valid IANA time zone name")
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"ui.timezone is invalid: {timezone_name}") from exc
        result = cls(
            trust_env=trust_env,
            hidden_tools=tuple(dict.fromkeys(name.strip() for name in hidden_tools)),
            subagents=SubAgentConfig.from_mapping(_section(raw, "subagents")),
            llm_profiles=profiles,
            llm_default=default_id,
            sandbox=_sandbox_from_mapping(sandbox_raw, base_dir=root),
            state_path=_optional_path(paths.get("state_path"), base_dir=root),
            config_dir=_optional_path(paths.get("config_dir"), base_dir=root),
            source_path=source_path.resolve() if source_path is not None else None,
            agent_instructions=instructions,
            ui_timezone=timezone_name,
            ui_display_limits=display_limits,
            tavily_api_key=os.environ.get("TAVILY_API_KEY") or _optional_secret(
                web_search.get("tavily_api_key"), field_name="web_search.tavily_api_key"
            ),
            request_timeout=request_timeout,
        )
        if result.source_path is not None:
            require_outside_workspace(result.source_path, result.sandbox.workspace, label="config file")
        if result.config_dir is not None:
            require_keybindings_outside_workspace(result.config_dir, result.sandbox.workspace)
        config_dir = (result.config_dir or (Path.home() / ".deep-agent")).expanduser().resolve()
        protected: list[Path] = []
        for candidate in (result.source_path, result.state_path):
            if candidate is None:
                continue
            resolved = candidate.expanduser().resolve()
            if resolved != config_dir and not resolved.is_relative_to(config_dir):
                protected.append(resolved)
        return replace(
            result,
            sandbox=replace(result.sandbox, protected_config_dir=config_dir, protected_files=tuple(protected)),
        )


def resolve_config_path(
    path: str | Path | None = None,
    *,
    base_dir: Path | None = None,
) -> Path | None:
    if path is not None:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            raise FileNotFoundError(f"config file not found: {candidate}")
        return candidate.resolve()
    env = os.environ.get("DEEP_AGENT_CONFIG")
    if env:
        candidate = Path(env).expanduser()
        if not candidate.is_file():
            raise FileNotFoundError(f"DEEP_AGENT_CONFIG not found: {candidate}")
        return candidate.resolve()
    root = base_dir.expanduser().resolve() if base_dir is not None else Path.home() / ".deep-agent"
    candidate = root / _DEFAULT_CONFIG_NAME
    return candidate.resolve() if candidate.is_file() else None


def require_outside_workspace(path: Path, workspace: Path, *, label: str) -> None:
    """Keep runtime configuration outside the agent's writable workspace."""
    if path.expanduser().resolve().is_relative_to(workspace.expanduser().resolve()):
        raise ConfigError(
            f"{label} must be outside workspace: {path}\n"
            "Launch deep-agent from a project directory that does not contain this path "
            "(for example, cd ~/projects/my-app)."
        )


def require_keybindings_outside_workspace(config_dir: Path, workspace: Path) -> None:
    require_outside_workspace(config_dir, workspace, label="keybindings directory")
    require_outside_workspace(config_dir / "keybindings.json", workspace, label="keybindings file")


def _read_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"config root must be a mapping: {path}")
    return loaded


def _section(data: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name) or {}
    if not isinstance(value, dict):
        raise ValueError(f"config.{name} must be a mapping")
    return value


def _optional_path(value: Any, *, base_dir: Path) -> Path | None:
    if value is None or value == "":
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _required_path(value: Any, *, base_dir: Path, default: Path) -> Path:
    if value is None or value == "":
        return default.expanduser().resolve()
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _workspace_path(value: Any, *, base_dir: Path) -> Path:
    """Resolve sandbox workspace.

    ``.`` / ``./`` / omitted → process cwd (start directory).
    Other relative paths resolve against the config file directory.
    Absolute paths are used as-is.
    """
    if value is None or value == "":
        return Path.cwd().resolve()
    text = str(value).strip()
    if text in {".", "./"}:
        return Path.cwd().resolve()
    return _required_path(value, base_dir=base_dir, default=Path.cwd())


def _as_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{field_name} must be a boolean, got {value!r}")


def _as_positive_int(value: Any, *, field_name: str, default: int) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer, got {value!r}") from exc
    if number <= 0:
        raise ValueError(f"{field_name} must be positive, got {number}")
    return number


def _as_str_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise ValueError(f"{field_name} must be a list of strings, got {value!r}")


def _as_str_dict(value: Any, *, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping, got {value!r}")
    return {str(key): str(item) for key, item in value.items()}


def _mount_modes(value: Any, *, field_name: str) -> tuple[SandboxMode, ...] | None:
    """``None`` means both modes. An empty or unknown list is a config error."""
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field_name} must list restricted and/or full-access")
    modes: list[SandboxMode] = []
    for item in value:
        parsed = parse_sandbox_mode(item) if isinstance(item, str) else None
        if parsed is None or parsed in modes:
            raise ValueError(f"{field_name} has an invalid or duplicate mode: {item!r}")
        modes.append(parsed)
    return tuple(modes)


def _as_mounts(value: Any, *, field_name: str, base_dir: Path) -> tuple[BindMount, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of mounts, got {value!r}")
    mounts: list[BindMount] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"{field_name}[{index}] must be a mapping")
        source = item.get("source")
        destination = item.get("destination")
        if source is None or destination is None:
            raise ValueError(f"{field_name}[{index}] requires source and destination")
        source_path = Path(str(source)).expanduser()
        if not source_path.is_absolute():
            source_path = (base_dir / source_path).resolve()
        else:
            source_path = source_path.resolve()
        optional = _as_bool(item.get("optional", False), field_name=f"{field_name}[{index}].optional")
        dev_bind = _as_bool(item.get("dev_bind", False), field_name=f"{field_name}[{index}].dev_bind")
        modes = _mount_modes(item.get("modes"), field_name=f"{field_name}[{index}].modes")
        if dev_bind and field_name == "sandbox.extra_read_only_mounts":
            raise ValueError(
                f"{field_name}[{index}].dev_bind cannot be true; device binds must be extra_read_write_mounts"
            )
        if optional and not source_path.exists():
            continue
        mounts.append(BindMount(
            source=source_path, destination=str(destination), optional=optional, dev_bind=dev_bind,
            modes=modes,
        ))
    return tuple(mounts)


def _llm_profiles_from_mapping(llm: Mapping[str, Any]) -> tuple[tuple[ModelProfile, ...], str]:
    _reject_removed_protocol_fields(llm, field_name="llm")
    source_fields = {
        "model", "api_key", "base_url", "input", "api", "context_window", "source",
        "reasoning_efforts", "max_retries", "stream_chunk_timeout",
    }
    unsupported = source_fields.intersection(llm)
    if unsupported:
        field = sorted(unsupported)[0]
        raise ValueError(f"llm.{field} is unsupported; configure it under llm.models.<source>")
    models_raw = llm.get("models")
    if not isinstance(models_raw, Mapping) or not models_raw:
        raise ValueError("llm.models must be a non-empty mapping of grouped model sources")
    return _grouped_llm_profiles(llm, models_raw)


def _grouped_llm_profiles(
    llm: Mapping[str, Any], groups: Mapping[str, Any],
) -> tuple[tuple[ModelProfile, ...], str]:
    profiles: list[ModelProfile] = []
    for source_key, group in groups.items():
        if not isinstance(source_key, str) or not source_key.strip() or "/" in source_key:
            raise ValueError("llm.models source keys must be non-empty strings containing no slash")
        source = source_key.strip()
        if not isinstance(group, Mapping) or not isinstance(group.get("models"), Mapping) or not group["models"]:
            raise ValueError(f"llm.models.{source}.models must be a non-empty mapping")
        group_api = _model_api(group, field_name=f"llm.models.{source}")
        _model_max_retries(group.get("max_retries"), field_name=f"llm.models.{source}.max_retries")
        _model_stream_chunk_timeout(
            group.get("stream_chunk_timeout"), field_name=f"llm.models.{source}.stream_chunk_timeout",
        )
        if "timeout" in group:
            raise ValueError(
                f"llm.models.{source}.timeout is not per model; set llm.timeout once for every model"
            )
        _model_reasoning_efforts(group.get("reasoning_efforts"), field_name=f"llm.models.{source}.reasoning_efforts")
        for model_key, item in group["models"].items():
            if not isinstance(model_key, str) or not model_key.strip() or "/" in model_key:
                raise ValueError(f"llm.models.{source}.models keys must be non-empty strings containing no slash")
            name = model_key.strip()
            field = f"llm.models.{source}.models.{name}"
            if not isinstance(item, Mapping):
                raise ValueError(f"{field} must be a mapping")
            if "timeout" in item:
                raise ValueError(f"{field}.timeout is not per model; set llm.timeout once for every model")
            def inherited(
                key: str,
                item: Mapping[Any, Any] = item,
                group: Mapping[Any, Any] = group,
            ) -> Any:
                return item.get(key, group.get(key))
            model_name = item.get("model", name)
            if not isinstance(model_name, str) or not model_name.strip():
                raise ValueError(f"{field}.model must be a non-empty string")
            profiles.append(ModelProfile(
                id=f"{source}/{name}",
                model=model_name.strip(),
                api_key=_optional_secret(
                    _model_text(inherited("api_key"), field_name=f"{field}.api_key", default="sk-local"),
                    field_name=f"{field}.api_key",
                ) or "sk-local",
                base_url=_model_text(inherited("base_url"), field_name=f"{field}.base_url", default="http://localhost:8000/v1"),
                input=_model_inputs(inherited("input"), field_name=f"{field}.input"),
                api=_model_api(item, field_name=field, default=group_api),
                context_window=_model_context_window(inherited("context_window"), field_name=f"{field}.context_window"),
                reasoning_efforts=_model_reasoning_efforts(inherited("reasoning_efforts"), field_name=f"{field}.reasoning_efforts"),
                max_retries=_model_max_retries(inherited("max_retries"), field_name=f"{field}.max_retries"),
                stream_chunk_timeout=_model_stream_chunk_timeout(
                    inherited("stream_chunk_timeout"), field_name=f"{field}.stream_chunk_timeout",
                ),
                source=source,
            ))
    default = llm.get("default")
    if not isinstance(default, str) or not default.strip():
        raise ValueError("llm.default is required for grouped models (source/model)")
    default_id = default.strip()
    if default_id not in {item.id for item in profiles}:
        raise ValueError(f"llm.default must name a configured source/model; unknown: {default_id}")
    return tuple(profiles), default_id


def _model_text(value: Any, *, field_name: str, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


_ENV_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _optional_secret(value: Any, *, field_name: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string or null")
    secret = value.strip()
    match = _ENV_REFERENCE.fullmatch(secret)
    if match:
        variable = match.group(1)
        secret = os.environ.get(variable, "").strip()
        if not secret:
            raise ValueError(f"{field_name} references missing environment variable {variable}")
    return secret


def _model_inputs(value: Any, *, field_name: str) -> tuple[InputKind, ...]:
    if value is None:
        return ("text",)
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{field_name} must be a non-empty list")
    result = tuple(str(item).strip().lower() for item in value)
    if any(not item for item in result):
        raise ValueError(f"{field_name} contains an empty input kind")
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} contains duplicate input kinds")
    unknown = sorted(set(result) - _INPUT_KINDS)
    if unknown:
        raise ValueError(f"{field_name} contains unsupported input kinds: {', '.join(unknown)}")
    if "text" not in result:
        raise ValueError(f"{field_name} must include text")
    kinds: list[InputKind] = []
    for item in result:
        if item == "text":
            kinds.append("text")
        elif item == "image":
            kinds.append("image")
        else:
            raise ValueError(f"{field_name} contains unsupported input kinds: {item}")
    return tuple(kinds)


def _reject_removed_protocol_fields(mapping: Mapping[str, Any], *, field_name: str) -> None:
    for removed in ("provider", "stream_usage"):
        if removed in mapping:
            raise ValueError(f"{field_name}.{removed} is unsupported; use api and remove obsolete fields")


def _model_api(mapping: Mapping[str, Any], *, field_name: str, default: str = "responses") -> Any:
    """Validate the explicit protocol; model-level values override the source."""
    _reject_removed_protocol_fields(mapping, field_name=field_name)
    api = mapping.get("api", default)
    if not isinstance(api, str) or api not in {"responses", "chat_completions"}:
        raise ValueError(f"{field_name}.api must be responses or chat_completions")
    return api


def _model_reasoning_efforts(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() or item != item.strip() or item == "default"
        for item in value
    ):
        raise ValueError(f"{field_name} must be a list of non-empty strings excluding default")
    if len(set(value)) != len(value):
        raise ValueError(f"{field_name} contains duplicate reasoning efforts")
    return tuple(value)


def _model_context_window(value: Any, *, field_name: str) -> int:
    """Accepts a plain token count, or a `128k` / `1.5m` shorthand. 0 means unknown."""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        tokens = value
    elif isinstance(value, str):
        text = value.strip().lower().replace("_", "")
        if not text:
            return 0
        multiplier = 1
        if text.endswith("k"):
            multiplier, text = 1_000, text[:-1]
        elif text.endswith("m"):
            multiplier, text = 1_000_000, text[:-1]
        try:
            tokens = int(float(text.strip()) * multiplier)
        except ValueError:
            raise ValueError(f"{field_name} must be a token count like 128000, 128k or 1m") from None
    else:
        raise ValueError(f"{field_name} must be a token count like 128000, 128k or 1m")
    if tokens < 0:
        raise ValueError(f"{field_name} must not be negative")
    return tokens


def _sandbox_mode(value: Any) -> SandboxMode:
    if value is None:
        return "restricted"
    if not isinstance(value, str):
        raise ValueError("sandbox.mode must be restricted or full-access")
    parsed = parse_sandbox_mode(value)
    if parsed is None:
        raise ValueError("sandbox.mode must be restricted or full-access")
    return parsed


def _sandbox_from_mapping(data: Mapping[str, Any], *, base_dir: Path) -> SandboxConfig:
    workspace = _workspace_path(data.get("workspace"), base_dir=base_dir)
    return SandboxConfig(
        workspace=workspace,
        mount_skills=_as_bool(data.get("mount_skills", True), field_name="sandbox.mount_skills"),
        skills_dir=_workspace_path(data["skills_dir"], base_dir=base_dir) if data.get("skills_dir") is not None else None,
        bwrap_path=str(data.get("bwrap_path") or "bwrap"),
        mode=_sandbox_mode(data.get("mode")),
        timeout_seconds=(
            _as_positive_int(data["timeout_seconds"], field_name="sandbox.timeout_seconds", default=0)
            if data.get("timeout_seconds") is not None else None
        ),
        max_output_bytes=_as_positive_int(
            data.get("max_output_bytes"), field_name="sandbox.max_output_bytes", default=100_000,
        ),
        env_allowlist=_as_str_tuple(data.get("env_allowlist"), field_name="sandbox.env_allowlist"),
        env_set=_as_str_dict(data.get("env_set"), field_name="sandbox.env_set"),
        extra_read_only_mounts=_as_mounts(
            data.get("extra_read_only_mounts"),
            field_name="sandbox.extra_read_only_mounts",
            base_dir=base_dir,
        ),
        extra_read_write_mounts=_as_mounts(
            data.get("extra_read_write_mounts"),
            field_name="sandbox.extra_read_write_mounts",
            base_dir=base_dir,
        ),
    )


def _model_max_retries(value: Any, *, field_name: str) -> int:
    if value is None:
        return 5
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _model_stream_chunk_timeout(value: Any, *, field_name: str) -> float:
    """Seconds without a parsed stream chunk before the current model request retries.

    ``0`` disables the idle timeout. ``None`` keeps the 120 second default.
    """
    if value is None:
        return DEFAULT_STREAM_CHUNK_TIMEOUT
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a non-negative number of seconds")
    number = float(value)
    if not (number >= 0 and number < float("inf")):
        raise ValueError(f"{field_name} must be a non-negative number of seconds")
    return number


def _model_request_timeout(value: Any, *, field_name: str) -> float:
    """Shared HTTP wait for the first response bytes. ``None`` keeps 60 seconds."""
    if value is None:
        return DEFAULT_REQUEST_TIMEOUT
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a positive number of seconds")
    number = float(value)
    if not (number > 0 and number < float("inf")):
        raise ValueError(f"{field_name} must be a positive number of seconds")
    return number
