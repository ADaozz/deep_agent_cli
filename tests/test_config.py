"""YAML config loading."""
from __future__ import annotations

from pathlib import Path

import pytest
from langchain_openai import ChatOpenAI

from agent.config import Settings, UiDisplayLimits, require_keybindings_outside_workspace, resolve_config_path
from agent.llm import QwenChatOpenAI, build_chat_model

_MODEL_YAML = "llm:\n  default: local/test\n  models:\n    local:\n      models:\n        test: {}\n"


def test_load_defaults_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEP_AGENT_CONFIG", raising=False)
    settings = Settings.load(base_dir=tmp_path)
    assert settings.llm_model == "qwen3.5-plus"
    assert settings.sandbox.allow_unsandboxed is False
    assert settings.sandbox.timeout_seconds is None
    assert settings.state_path is None
    assert settings.ui_timezone == "Asia/Shanghai"
    assert settings.ui_display_limits.thinking_tail_lines == 5


def test_secret_environment_references_and_tavily_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "model-secret")
    monkeypatch.setenv("YAML_TAVILY_KEY", "yaml-secret")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    data = {
        "llm": {"default": "local/test", "models": {"local": {
            "api_key": "${MODEL_API_KEY}", "models": {"test": {}},
        }}},
        "web_search": {"tavily_api_key": "${YAML_TAVILY_KEY}"},
    }
    settings = Settings.from_mapping(data)
    assert settings.llm_api_key == "model-secret"
    assert settings.tavily_api_key == "yaml-secret"
    monkeypatch.setenv("TAVILY_API_KEY", "env-secret")
    assert Settings.from_mapping(data).tavily_api_key == "env-secret"


def test_missing_secret_reference_reports_field_without_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(ValueError, match="web_search.tavily_api_key references missing environment variable MISSING_API_KEY"):
        Settings.from_mapping({"web_search": {"tavily_api_key": "${MISSING_API_KEY}"}})
    with pytest.raises(ValueError, match="llm.models.local.models.test.api_key references missing environment variable MISSING_API_KEY"):
        Settings.from_mapping({"llm": {"default": "local/test", "models": {
            "local": {"api_key": "${MISSING_API_KEY}", "models": {"test": {}}},
        }}})


def test_ui_timezone_can_be_configured_and_invalid_names_fail(tmp_path: Path) -> None:
    settings = Settings.from_mapping({"ui": {"timezone": "Asia/Tokyo"}})
    assert settings.ui_timezone == "Asia/Tokyo"
    for value in ("Mars/Olympus", "", 42):
        with pytest.raises(ValueError, match="ui.timezone"):
            Settings.from_mapping({"ui": {"timezone": value}})

    path = tmp_path / "config.yaml"
    path.write_text(_MODEL_YAML + "ui:\n  timezone: Mars/Olympus\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ui.timezone"):
        Settings.load(path)


def test_thinking_tail_lines_can_be_configured(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(_MODEL_YAML + "ui:\n  thinking_tail_lines: 3\n", encoding="utf-8")
    assert Settings.load(path).ui_display_limits.thinking_tail_lines == 3


@pytest.mark.parametrize("value", [0, -1, True, False, 3.5, "5", None])
def test_thinking_tail_lines_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="ui.thinking_tail_lines must be a positive integer"):
        Settings.from_mapping({"ui": {"thinking_tail_lines": value}})


def test_all_ui_display_limits_are_loaded_and_validated() -> None:
    values = {name: 2 for name in UiDisplayLimits.__dataclass_fields__}
    configured = Settings.from_mapping({"ui": values}).ui_display_limits
    assert all(getattr(configured, name) == value for name, value in values.items())
    for name in values:
        for invalid in (0, -1, True, None, "2", 2.5):
            with pytest.raises(ValueError, match=f"ui.{name}"):
                Settings.from_mapping({"ui": {name: invalid}})
    with pytest.raises(ValueError, match="ui.edit_preview_changed_lines"):
        Settings.from_mapping({"ui": {"edit_preview_changed_lines": 1}})


def test_removed_protected_paths_fail_with_migration_message() -> None:
    with pytest.raises(ValueError, match="move skills to"):
        Settings.from_mapping({"sandbox": {"protected_workspace_paths": ["skills"]}})


def test_default_config_is_in_home_and_project_config_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".deep-agent"
    config_dir.mkdir(parents=True)
    home_config = config_dir / "config.yaml"
    home_config.write_text(
        "llm:\n  default: local/home\n  models:\n    local:\n      models:\n        home:\n          model: from-home\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "config.yaml").write_text(
        "llm:\n  default: local/project\n  models:\n    local:\n      models:\n        project:\n          model: from-project\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DEEP_AGENT_CONFIG", raising=False)
    monkeypatch.chdir(project)

    settings = Settings.load()
    assert settings.llm_model == "from-home"
    assert settings.source_path == home_config.resolve()
    home_config.unlink()
    assert Settings.load().source_path is None
    assert Settings.load().llm_model == "qwen3.5-plus"


def test_runtime_config_paths_must_be_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    local_config = workspace / "config.yaml"
    local_config.write_text(_MODEL_YAML + f"sandbox:\n  workspace: {workspace}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="config file must be outside workspace"):
        Settings.load(local_config)

    external = tmp_path / "config.yaml"
    external.write_text(
        _MODEL_YAML + f"sandbox:\n  workspace: {workspace}\npaths:\n  config_dir: {workspace / 'keys'}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="keybindings directory must be outside workspace"):
        Settings.load(external)
    with pytest.raises(ValueError, match="keybindings directory must be outside workspace"):
        require_keybindings_outside_workspace(workspace / "keys", workspace)
    require_keybindings_outside_workspace(tmp_path / "keys", workspace)


def test_keybindings_symlink_into_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    keybindings = workspace / "keybindings.json"
    keybindings.write_text("{}\n", encoding="utf-8")
    external_dir = tmp_path / "keys"
    external_dir.mkdir()
    (external_dir / "keybindings.json").symlink_to(keybindings)
    config = tmp_path / "config.yaml"
    config.write_text(
        _MODEL_YAML + f"sandbox:\n  workspace: {workspace}\npaths:\n  config_dir: {external_dir}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="keybindings file must be outside workspace"):
        Settings.load(config)


def test_config_symlink_into_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    local_config = workspace / "config.yaml"
    local_config.write_text(_MODEL_YAML + f"sandbox:\n  workspace: {workspace}\n", encoding="utf-8")
    external_link = tmp_path / "config.yaml"
    external_link.symlink_to(local_config)
    with pytest.raises(ValueError, match="config file must be outside workspace"):
        Settings.load(external_link)


def test_home_as_workspace_rejects_home_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / ".deep-agent"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(_MODEL_YAML, encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DEEP_AGENT_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="config file must be outside workspace"):
        Settings.load()


def test_load_config_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
agent:
  instructions: |
    优先使用中文回答。
llm:
  default: local/demo
  models:
    local:
      api_key: secret
      base_url: http://example.test/v1
      models:
        demo:
          model: demo-model
sandbox:
  workspace: work
  allow_unsandboxed: true
  timeout_seconds: 30
  max_output_bytes: 2048
  env_allowlist:
    - JAVA_HOME
  env_set:
    APP_ENV: test
  extra_read_only_mounts:
    - source: mounts/ro
      destination: /opt/ro
paths:
  state_path: state/agent.sqlite3
  config_dir: conf
""",
        encoding="utf-8",
    )
    (tmp_path / "work").mkdir()
    (tmp_path / "mounts" / "ro").mkdir(parents=True)

    settings = Settings.load(path)
    assert settings.llm_default == "local/demo"
    assert settings.agent_instructions == "优先使用中文回答。\n"
    assert settings.llm_model == "demo-model"
    assert settings.llm_api_key == "secret"
    assert settings.llm_base_url == "http://example.test/v1"
    assert len(settings.llm_profiles) == 1
    assert settings.active_profile.id == "local/demo"
    assert settings.active_profile.input == ("text",)
    assert settings.sandbox.workspace == (tmp_path / "work").resolve()
    assert settings.sandbox.allow_unsandboxed is True
    assert settings.sandbox.timeout_seconds == 30
    assert settings.sandbox.max_output_bytes == 2048
    assert settings.sandbox.env_allowlist == ("JAVA_HOME",)
    assert settings.sandbox.env_set == {"APP_ENV": "test"}
    assert settings.sandbox.extra_read_only_mounts[0].destination == "/opt/ro"
    assert settings.state_path == (tmp_path / "state" / "agent.sqlite3").resolve()
    assert settings.config_dir == (tmp_path / "conf").resolve()
    assert settings.source_path == path.resolve()


def test_optional_mount_is_skipped_when_source_is_missing(tmp_path: Path) -> None:
    present = tmp_path / "present"
    present.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(
        _MODEL_YAML
        + f"""
sandbox:
  extra_read_only_mounts:
    - source: {present}
      destination: /opt/present
      optional: true
    - source: {tmp_path / "missing"}
      destination: /opt/missing
      optional: true
    - source: {tmp_path / "required-missing"}
      destination: /opt/required
""",
        encoding="utf-8",
    )
    mounts = Settings.load(config).sandbox.extra_read_only_mounts
    assert [(mount.destination, mount.optional) for mount in mounts] == [
        ("/opt/present", True),
        ("/opt/required", False),
    ]


def test_sandbox_timeout_is_optional_and_must_be_positive(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(_MODEL_YAML + "sandbox:\n  timeout_seconds: null\n", encoding="utf-8")
    assert Settings.load(config).sandbox.timeout_seconds is None
    config.write_text(_MODEL_YAML + "sandbox:\n  timeout_seconds: 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sandbox.timeout_seconds must be positive"):
        Settings.load(config)


def test_deep_agent_config_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "custom.yaml"
    path.write_text(
        "llm:\n  default: local/env\n  models:\n    local:\n      models:\n        env:\n          model: from-env\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEP_AGENT_CONFIG", str(path))
    settings = Settings.load(base_dir=tmp_path / "other")
    assert settings.llm_model == "from-env"
    assert resolve_config_path(base_dir=tmp_path / "other") == path.resolve()


def test_agent_instructions_rejects_non_text(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(_MODEL_YAML + "agent:\n  instructions: [not, text]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="agent.instructions"):
        Settings.load(path)


def test_workspace_dot_follows_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    path = cfg_dir / "config.yaml"
    path.write_text(_MODEL_YAML + "sandbox:\n  workspace: .\n", encoding="utf-8")
    monkeypatch.chdir(project)
    settings = Settings.load(path)
    assert settings.sandbox.workspace == project.resolve()


def test_workspace_relative_resolves_against_config_dir(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    path = tmp_path / "config.yaml"
    path.write_text(_MODEL_YAML + "sandbox:\n  workspace: work\n", encoding="utf-8")
    settings = Settings.load(path)
    assert settings.sandbox.workspace == work.resolve()


def test_multi_model_catalog_and_prefix(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
llm:
  default: local/qwen-plus
  models:
    local:
      api_key: a
      base_url: http://a.test/v1
      models:
        qwen-plus:
          model: qwen3.5-plus
        qwen-fast:
          model: qwen-turbo
          api_key: b
          base_url: http://b.test/v1
""",
        encoding="utf-8",
    )
    settings = Settings.load(path)
    assert settings.llm_default == "local/qwen-plus"
    assert [item.id for item in settings.llm_profiles] == ["local/qwen-plus", "local/qwen-fast"]
    assert settings.get_profile("local/qwen-f").id == "local/qwen-fast"
    with pytest.raises(KeyError, match="Ambiguous"):
        settings.get_profile("local/qwen")
    with pytest.raises(KeyError, match="Unknown"):
        settings.get_profile("missing")


def test_grouped_models_inherit_source_settings_and_reject_old_ids() -> None:
    settings = Settings.from_mapping({"llm": {
        "default": "token-plan/auto",
        "models": {
            "local": {"base_url": "http://local/v1", "models": {
                "qwen-plus": {"model": "qwen3.5-plus"},
                "qwen3.8-max": {},
            }},
            "token-plan": {
                "api_key": "secret", "base_url": "https://plan/v1",
                "api": "chat_completions",
                "context_window": "1m", "models": {
                    "auto": {"input": ["text"]},
                    "qwen3.8-max": {"input": ["text", "image"], "context_window": "128k"},
                },
            },
        },
    }})
    assert settings.active_profile.id == "token-plan/auto"
    assert settings.active_profile.model == "auto"
    assert settings.active_profile.api == "chat_completions"
    assert settings.active_profile.context_window == 1_000_000
    assert not hasattr(settings.active_profile, "stream_usage")
    assert settings.get_profile("token-plan/qwen3.8-max").context_window == 128_000
    for old_id in ("token-plan-auto", "qwen-plus", "qwen3.8-max", "token-plan-qwen3.8-max"):
        with pytest.raises(KeyError, match="Unknown"):
            settings.get_profile(old_id)


@pytest.mark.parametrize("llm", [
    {"model": "qwen3.5-plus"},
    {"default": "qwen-plus", "models": {"qwen-plus": {"model": "qwen3.5-plus"}}},
    {"default": "local/test", "api_key": "old", "models": {"local": {"models": {"test": {}}}}},
])
def test_old_model_config_is_rejected(llm: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"llm\.(model|models|api_key)"):
        Settings.from_mapping({"llm": llm})


def test_config_file_requires_grouped_models(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="llm.models must define grouped"):
        Settings.load(path)


@pytest.mark.parametrize("default", [None, "", "token-plan/missing", "missing/auto", 123])
def test_grouped_models_require_a_valid_explicit_default(default: object) -> None:
    with pytest.raises(ValueError, match="llm.default"):
        Settings.from_mapping({"llm": {"default": default, "models": {
            "token-plan": {"models": {"auto": {}}},
        }}})


def test_grouped_model_errors_include_field_and_config_path(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("llm:\n  default: token-plan/auto\n  models:\n    token-plan:\n      models:\n        auto:\n          context_window: huge\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"config.yaml: llm.models.token-plan.models.auto.context_window"):
        Settings.load(config)


def test_grouped_models_reject_non_string_connection_settings() -> None:
    with pytest.raises(ValueError, match=r"llm.models.token-plan.models.auto.api_key"):
        Settings.from_mapping({"llm": {
            "default": "token-plan/auto",
            "models": {"token-plan": {"api_key": ["bad"], "models": {"auto": {}}}},
        }})


def test_yaml_syntax_error_reports_location_without_source_text(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("llm:\n  models: [secret: value\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"config.yaml: invalid YAML syntax at line") as error:
        Settings.load(config)
    assert "secret" not in str(error.value)


def test_model_input_capabilities(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "llm:\n  default: local/vision\n  models:\n    local:\n      models:\n        vision:\n          model: qwen\n          input: [text, image]\n",
        encoding="utf-8",
    )
    profile = Settings.load(path).active_profile
    assert profile.input == ("text", "image")
    assert profile.supports_input("image")


def test_context_window_accepts_counts_and_shorthand(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
llm:
  default: local/exact
  models:
    local:
      context_window: 32k
      models:
        exact:
          model: qwen3.5-plus
          context_window: 1000000
        shorthand:
          model: qwen-turbo
          context_window: 128k
        fractional:
          model: qwen-max
          context_window: 1.5m
        inherited:
          model: qwen-flash
""",
        encoding="utf-8",
    )
    windows = {item.id: item.context_window for item in Settings.load(path).llm_profiles}
    assert windows == {
        "local/exact": 1_000_000,
        "local/shorthand": 128_000,
        "local/fractional": 1_500_000,
        "local/inherited": 32_000,
    }


def test_context_window_rejects_nonsense(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "llm:\n  default: local/broken\n  models:\n    local:\n      models:\n        broken:\n          model: qwen\n          context_window: huge\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="context_window"):
        Settings.load(path)


def test_api_selects_compatible_chatopenai_and_rejects_unknown(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "llm:\n  default: gateway/compatible\n  models:\n    gateway:\n      api: chat_completions\n      models:\n        compatible:\n          model: local\n",
        encoding="utf-8",
    )
    model = build_chat_model(Settings.load(path).active_profile)
    assert isinstance(model, ChatOpenAI)
    assert model.use_responses_api is False
    path.write_text("llm:\n  default: gateway/compatible\n  models:\n    gateway:\n      api: unknown\n      models:\n        compatible: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="api"):
        Settings.load(path)
    assert isinstance(build_chat_model(Settings().active_profile), QwenChatOpenAI)


def test_model_source_is_loaded_for_display(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "llm:\n  default: token-plan/qwen3.6-flash\n  models:\n    token-plan:\n      models:\n        qwen3.6-flash: {}\n",
        encoding="utf-8",
    )
    assert Settings.load(path).active_profile.source == "token-plan"


def test_chat_completions_always_requests_stream_usage(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "llm:\n  default: plan/auto\n  models:\n"
        "    plan:\n      api: chat_completions\n      models:\n        auto: {}\n"
        "    local:\n      api: chat_completions\n      models:\n        qwen3.6-flash: {}\n",
        encoding="utf-8",
    )
    settings = Settings.load(path)
    assert build_chat_model(settings.get_profile("plan/auto")).stream_usage is True
    assert build_chat_model(settings.get_profile("local/qwen3.6-flash")).stream_usage is True


@pytest.mark.parametrize("value,match", [
    ("[]", "non-empty"),
    ("[image]", "include text"),
    ("[text, text]", "duplicate"),
    ("[text, audio]", "unsupported"),
])
def test_invalid_model_inputs(tmp_path: Path, value: str, match: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"llm:\n  default: local/bad\n  models:\n    local:\n      models:\n        bad:\n          model: qwen\n          input: {value}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=match):
        Settings.load(path)


def test_invalid_default_model_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
llm:
  default: local/nope
  models:
    local:
      models:
        only:
          model: m
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="llm.default"):
        Settings.load(path)


def test_invalid_bool_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(_MODEL_YAML + "sandbox:\n  allow_unsandboxed: maybe\n", encoding="utf-8")
    with pytest.raises(ValueError, match="allow_unsandboxed"):
        Settings.load(path)


def test_reasoning_efforts_inherit_replace_and_clear() -> None:
    settings = Settings.from_mapping({"llm": {"default": "local/inherit", "models": {
        "local": {"reasoning_efforts": ["none", "low", "high"], "models": {
            "inherit": {}, "override": {"reasoning_efforts": ["high", "max"]},
            "empty": {"reasoning_efforts": []}, "null": {"reasoning_efforts": None},
        }}, "other": {"models": {"plain": {}}},
    }}})
    assert settings.get_profile("local/inherit").reasoning_efforts == ("none", "low", "high")
    assert settings.get_profile("local/override").reasoning_efforts == ("high", "max")
    for name in ("local/empty", "local/null", "other/plain"):
        assert settings.get_profile(name).reasoning_efforts == ()


@pytest.mark.parametrize("value", ["low", True, [""], [1], ["default"], ["low", "low"], [" low "]])
def test_invalid_reasoning_efforts(value) -> None:
    with pytest.raises(ValueError, match="reasoning_efforts"):
        Settings.from_mapping({"llm": {"default": "local/test", "models": {
            "local": {"models": {"test": {"reasoning_efforts": value}}},
        }}})


def test_invalid_group_efforts_are_checked_even_when_model_overrides() -> None:
    with pytest.raises(ValueError, match="llm.models.local.reasoning_efforts"):
        Settings.from_mapping({"llm": {"default": "local/test", "models": {
            "local": {"reasoning_efforts": "low", "models": {"test": {"reasoning_efforts": []}}},
        }}})


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
def test_api_inheritance_and_model_override(api):
    settings = Settings.from_mapping({"llm": {"default": "source/a", "models": {"source": {
        "api": api, "models": {"a": {}, "b": {"api": "chat_completions"}},
    }}}})
    assert settings.get_profile("source/a").api == api
    assert settings.get_profile("source/b").api == "chat_completions"
    assert not hasattr(settings.active_profile, "provider")
    assert not hasattr(settings.active_profile, "stream_usage")


@pytest.mark.parametrize("value", ["auto", None, {}])
@pytest.mark.parametrize("level", ["source", "model"])
def test_invalid_api_is_rejected(value, level):
    source = {"models": {"a": {}}}
    target = source if level == "source" else source["models"]["a"]
    target["api"] = value
    with pytest.raises(ValueError, match="api"):
        Settings.from_mapping({"llm": {"default": "source/a", "models": {"source": source}}})


@pytest.mark.parametrize("field,value", [
    ("provider", "qwen-responses"), ("provider", "openai-compatible"),
    ("stream_usage", True), ("stream_usage", False),
])
@pytest.mark.parametrize("level", ["llm", "source", "model"])
@pytest.mark.parametrize("explicit_api", [False, True])
def test_removed_protocol_fields_are_rejected(field, value, level, explicit_api):
    source = {"models": {"a": {}}}
    llm = {"default": "source/a", "models": {"source": source}}
    target = llm if level == "llm" else source if level == "source" else source["models"]["a"]
    target[field] = value
    if explicit_api:
        target["api"] = "responses"
    with pytest.raises(ValueError, match=rf"{field} is unsupported"):
        Settings.from_mapping({"llm": llm})


def test_api_loading_does_not_rewrite_config_file(tmp_path):
    path = tmp_path / "config.yaml"
    original = 'llm:\n  default: s/m\n  models:\n    s:\n      api: responses\n      models:\n        m: {}\n'
    path.write_text(original)
    assert Settings.load(path).active_profile.api == "responses"
    assert path.read_text() == original


def test_removed_fields_error_does_not_rewrite_config_file(tmp_path):
    path = tmp_path / "config.yaml"
    original = 'llm:\n  default: s/m\n  models:\n    s:\n      provider: qwen-responses\n      models:\n        m: {}\n'
    path.write_text(original)
    with pytest.raises(ValueError, match="provider is unsupported"):
        Settings.load(path)
    assert path.read_text() == original
