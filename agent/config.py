from __future__ import annotations

from dataclasses import dataclass, field
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


class ConfigError(ValueError):
    """A user configuration error safe to display without a traceback."""


def default_skills_dir() -> Path:
    return Path.home() / ".deep-agent" / "skills"


@dataclass(frozen=True)
class BindMount:
    """An explicit host-to-sandbox mount."""

    source: Path
    destination: str
    optional: bool = False


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

    def supports_input(self, kind: "InputKind") -> bool:
        return kind in self.input


InputKind = Literal["text", "image"]
_INPUT_KINDS = frozenset({"text", "image"})


@dataclass(frozen=True)
class SandboxConfig:
    """Bubblewrap policy. Defaults expose only the workspace and read-only runtimes."""

    workspace: Path = field(default_factory=Path.cwd)
    bwrap_path: str = "bwrap"
    allow_unsandboxed: bool = False
    timeout_seconds: int | None = None
    max_output_bytes: int = 100_000
    env_allowlist: tuple[str, ...] = ()
    env_set: dict[str, str] = field(default_factory=dict)
    extra_read_only_mounts: tuple[BindMount, ...] = ()
    extra_read_write_mounts: tuple[BindMount, ...] = ()


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

    thinking_tail_lines: int = 5
    execute_tail_lines: int = 4
    tool_tail_lines: int = 8
    expanded_tool_lines: int = 40
    explore_preview_items: int = 5
    explore_failure_items: int = 3
    edit_preview_changed_lines: int = 8
    write_preview_lines: int = 6
    create_preview_items: int = 5
    failure_preview_lines: int = 4
    command_failure_tail_lines: int = 6
    editor_max_lines: int = 10
    completion_menu_lines: int = 8

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            minimum = 2 if name == "edit_preview_changed_lines" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                requirement = "a positive integer" if minimum == 1 else "an integer >= 2"
                raise ValueError(f"ui.{name} must be {requirement}")

    @classmethod
    def from_mapping(cls, ui: Mapping[str, Any]) -> "UiDisplayLimits":
        return cls(**{name: ui[name] for name in cls.__dataclass_fields__ if name in ui})


DEFAULT_UI_DISPLAY_LIMITS = UiDisplayLimits()


@dataclass(frozen=True)
class Settings:
    llm_profiles: tuple[ModelProfile, ...] = field(default_factory=_default_profiles)
    llm_default: str = "default"
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    state_path: Path | None = None
    config_dir: Path | None = None
    source_path: Path | None = None
    agent_instructions: str | None = None
    ui_timezone: str = DEFAULT_UI_TIMEZONE
    ui_display_limits: UiDisplayLimits = field(default_factory=UiDisplayLimits)
    tavily_api_key: str | None = field(default=None, repr=False)

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
    def load(cls, path: str | Path | None = None, *, base_dir: Path | None = None) -> "Settings":
        """Load settings from a path, explicit base directory, or ~/.deep-agent/config.yaml."""
        resolved = resolve_config_path(path, base_dir=base_dir)
        if resolved is None:
            return cls()
        try:
            data = _read_yaml(resolved)
            if "llm" not in data:
                raise ValueError("llm.models must define grouped model sources")
            return cls.from_mapping(data, base_dir=resolved.parent, source_path=resolved)
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
    ) -> "Settings":
        raw = dict(data or {})
        root = (base_dir or Path.cwd()).expanduser().resolve()
        llm = _section(raw, "llm")
        agent = _section(raw, "agent")
        ui = _section(raw, "ui")
        paths = _section(raw, "paths")
        sandbox_raw = _section(raw, "sandbox")
        web_search = _section(raw, "web_search")
        if "protected_workspace_paths" in sandbox_raw:
            raise ValueError(
                "sandbox.protected_workspace_paths was removed; move skills to "
                "~/.deep-agent/skills and use explicit read-only mounts for other resources"
            )
        profiles, default_id = _llm_profiles_from_mapping(llm) if "llm" in raw else (_default_profiles(), "default")
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
        )
        if result.source_path is not None:
            require_outside_workspace(result.source_path, result.sandbox.workspace, label="config file")
        if result.config_dir is not None:
            require_keybindings_outside_workspace(result.config_dir, result.sandbox.workspace)
        return result

    # Backward-compatible alias used by older call sites / docs.
    @classmethod
    def from_env(cls) -> "Settings":
        return cls.load()


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
        raise ValueError(f"{label} must be outside workspace: {path}")


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
        if optional and not source_path.exists():
            continue
        mounts.append(BindMount(source=source_path, destination=str(destination), optional=optional))
    return tuple(mounts)


def _llm_profiles_from_mapping(llm: Mapping[str, Any]) -> tuple[tuple[ModelProfile, ...], str]:
    _reject_removed_protocol_fields(llm, field_name="llm")
    source_fields = {"model", "api_key", "base_url", "input", "api", "context_window", "source", "reasoning_efforts"}
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
        _model_reasoning_efforts(group.get("reasoning_efforts"), field_name=f"llm.models.{source}.reasoning_efforts")
        for model_key, item in group["models"].items():
            if not isinstance(model_key, str) or not model_key.strip() or "/" in model_key:
                raise ValueError(f"llm.models.{source}.models keys must be non-empty strings containing no slash")
            name = model_key.strip()
            field = f"llm.models.{source}.models.{name}"
            if not isinstance(item, Mapping):
                raise ValueError(f"{field} must be a mapping")
            def inherited(key: str) -> Any:
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
    return result  # type: ignore[return-value]


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


def _sandbox_from_mapping(data: Mapping[str, Any], *, base_dir: Path) -> SandboxConfig:
    workspace = _workspace_path(data.get("workspace"), base_dir=base_dir)
    return SandboxConfig(
        workspace=workspace,
        bwrap_path=str(data.get("bwrap_path") or "bwrap"),
        allow_unsandboxed=_as_bool(
            data.get("allow_unsandboxed", False), field_name="sandbox.allow_unsandboxed",
        ),
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
