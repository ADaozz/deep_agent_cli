from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any, Literal, Mapping

import yaml

SAFE_INHERITED_ENV = (
    "LANG",
    "TERM",
    "COLORTERM",
    "NO_COLOR",
    "TZ",
)

_DEFAULT_CONFIG_NAMES = ("config.yaml", "config.yml", "config.example.yaml", "config.example.yml")


@dataclass(frozen=True)
class BindMount:
    """An explicit host-to-sandbox mount."""

    source: Path
    destination: str


@dataclass(frozen=True)
class ModelProfile:
    """Named OpenAI-compatible chat model profile."""

    id: str
    model: str
    api_key: str = "sk-local"
    base_url: str = "http://localhost:8000/v1"
    input: tuple["InputKind", ...] = ("text",)
    provider: Literal["qwen-responses", "openai-compatible"] = "qwen-responses"

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
    timeout_seconds: int = 120
    max_output_bytes: int = 100_000
    env_allowlist: tuple[str, ...] = ()
    env_set: dict[str, str] = field(default_factory=dict)
    extra_read_only_mounts: tuple[BindMount, ...] = ()
    extra_read_write_mounts: tuple[BindMount, ...] = ()
    protected_workspace_paths: tuple[str, ...] = ("skills",)


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
class Settings:
    llm_profiles: tuple[ModelProfile, ...] = field(default_factory=_default_profiles)
    llm_default: str = "default"
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    state_path: Path | None = None
    config_dir: Path | None = None
    source_path: Path | None = None
    agent_instructions: str | None = None

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
        """Load settings from config.yaml (or defaults when no file is found)."""
        resolved = resolve_config_path(path, base_dir=base_dir)
        if resolved is None:
            return cls()
        data = _read_yaml(resolved)
        return cls.from_mapping(data, base_dir=resolved.parent, source_path=resolved)

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
        paths = _section(raw, "paths")
        sandbox_raw = _section(raw, "sandbox")
        profiles, default_id = _llm_profiles_from_mapping(llm)
        instructions = agent.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise ValueError("agent.instructions must be a string or null")
        return cls(
            llm_profiles=profiles,
            llm_default=default_id,
            sandbox=_sandbox_from_mapping(sandbox_raw, base_dir=root),
            state_path=_optional_path(paths.get("state_path"), base_dir=root),
            config_dir=_optional_path(paths.get("config_dir"), base_dir=root),
            source_path=source_path.resolve() if source_path is not None else None,
            agent_instructions=instructions,
        )

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
    search_roots: list[Path] = []
    if base_dir is not None:
        search_roots.append(base_dir.expanduser().resolve())
    else:
        search_roots.append(Path.cwd().resolve())
        # Prefer the package/template root when running from examples/.
        search_roots.append(Path(__file__).resolve().parents[1])
    seen: set[Path] = set()
    for root in search_roots:
        if root in seen:
            continue
        seen.add(root)
        for name in _DEFAULT_CONFIG_NAMES:
            candidate = root / name
            if candidate.is_file():
                return candidate.resolve()
    return None


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
        mounts.append(BindMount(source=source_path, destination=str(destination)))
    return tuple(mounts)


def _llm_profiles_from_mapping(llm: Mapping[str, Any]) -> tuple[tuple[ModelProfile, ...], str]:
    models_raw = llm.get("models")
    if models_raw is None:
        model = str(llm.get("model") or "qwen3.5-plus")
        profile = ModelProfile(
            id="default",
            model=model,
            api_key=str(llm.get("api_key") or "sk-local"),
            base_url=str(llm.get("base_url") or "http://localhost:8000/v1"),
            input=_model_inputs(llm.get("input"), field_name="llm.input"),
            provider=_model_provider(llm.get("provider"), field_name="llm.provider"),
        )
        return (profile,), "default"
    if not isinstance(models_raw, Mapping) or not models_raw:
        raise ValueError("llm.models must be a non-empty mapping of profile id → config")
    profiles: list[ModelProfile] = []
    for profile_id, item in models_raw.items():
        if not isinstance(item, Mapping):
            raise ValueError(f"llm.models.{profile_id} must be a mapping")
        pid = str(profile_id).strip()
        if not pid:
            raise ValueError("llm.models keys must be non-empty profile ids")
        model_name = str(item.get("model") or "").strip()
        if not model_name:
            raise ValueError(f"llm.models.{pid}.model is required")
        profiles.append(ModelProfile(
            id=pid,
            model=model_name,
            api_key=str(item.get("api_key") or llm.get("api_key") or "sk-local"),
            base_url=str(item.get("base_url") or llm.get("base_url") or "http://localhost:8000/v1"),
            input=_model_inputs(item.get("input"), field_name=f"llm.models.{pid}.input"),
            provider=_model_provider(item.get("provider", llm.get("provider")), field_name=f"llm.models.{pid}.provider"),
        ))
    default_id = str(llm.get("default") or profiles[0].id).strip()
    if not any(item.id == default_id for item in profiles):
        known = ", ".join(item.id for item in profiles)
        raise ValueError(f"llm.default {default_id!r} is not in llm.models ({known})")
    return tuple(profiles), default_id


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


def _model_provider(value: Any, *, field_name: str) -> Literal["qwen-responses", "openai-compatible"]:
    provider = str(value or "qwen-responses").strip()
    if provider not in {"qwen-responses", "openai-compatible"}:
        raise ValueError(f"{field_name} must be qwen-responses or openai-compatible")
    return provider  # type: ignore[return-value]


def _sandbox_from_mapping(data: Mapping[str, Any], *, base_dir: Path) -> SandboxConfig:
    workspace = _workspace_path(data.get("workspace"), base_dir=base_dir)
    protected = data.get("protected_workspace_paths")
    if protected is None:
        protected_paths: tuple[str, ...] = ("skills",)
    else:
        protected_paths = _as_str_tuple(protected, field_name="sandbox.protected_workspace_paths")
    return SandboxConfig(
        workspace=workspace,
        bwrap_path=str(data.get("bwrap_path") or "bwrap"),
        allow_unsandboxed=_as_bool(
            data.get("allow_unsandboxed", False), field_name="sandbox.allow_unsandboxed",
        ),
        timeout_seconds=_as_positive_int(
            data.get("timeout_seconds"), field_name="sandbox.timeout_seconds", default=120,
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
        protected_workspace_paths=protected_paths,
    )


settings = Settings.load()
