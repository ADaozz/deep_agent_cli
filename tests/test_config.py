"""YAML config loading."""
from __future__ import annotations

from pathlib import Path

import pytest
from langchain_openai import ChatOpenAI

from agent.config import Settings, require_keybindings_outside_workspace, resolve_config_path
from agent.llm import QwenChatOpenAI, build_chat_model


def test_load_defaults_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEP_AGENT_CONFIG", raising=False)
    settings = Settings.load(base_dir=tmp_path)
    assert settings.llm_model == "qwen3.5-plus"
    assert settings.sandbox.allow_unsandboxed is False
    assert settings.state_path is None


def test_default_config_is_in_home_and_project_config_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".deep-agent"
    config_dir.mkdir(parents=True)
    home_config = config_dir / "config.yaml"
    home_config.write_text("llm:\n  model: from-home\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / "config.yaml").write_text("llm:\n  model: from-project\n", encoding="utf-8")
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
    local_config.write_text(f"sandbox:\n  workspace: {workspace}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="config file must be outside workspace"):
        Settings.load(local_config)

    external = tmp_path / "config.yaml"
    external.write_text(
        f"sandbox:\n  workspace: {workspace}\npaths:\n  config_dir: {workspace / 'keys'}\n",
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
        f"sandbox:\n  workspace: {workspace}\npaths:\n  config_dir: {external_dir}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="keybindings file must be outside workspace"):
        Settings.load(config)


def test_config_symlink_into_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    local_config = workspace / "config.yaml"
    local_config.write_text(f"sandbox:\n  workspace: {workspace}\n", encoding="utf-8")
    external_link = tmp_path / "config.yaml"
    external_link.symlink_to(local_config)
    with pytest.raises(ValueError, match="config file must be outside workspace"):
        Settings.load(external_link)


def test_home_as_workspace_rejects_home_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / ".deep-agent"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
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
  model: demo-model
  api_key: secret
  base_url: http://example.test/v1
sandbox:
  workspace: work
  allow_unsandboxed: true
  timeout_seconds: 30
  max_output_bytes: 2048
  env_allowlist:
    - JAVA_HOME
  env_set:
    APP_ENV: test
  protected_workspace_paths:
    - skills
    - vendor
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
    assert settings.llm_default == "default"
    assert settings.agent_instructions == "优先使用中文回答。\n"
    assert settings.llm_model == "demo-model"
    assert settings.llm_api_key == "secret"
    assert settings.llm_base_url == "http://example.test/v1"
    assert len(settings.llm_profiles) == 1
    assert settings.active_profile.id == "default"
    assert settings.active_profile.input == ("text",)
    assert settings.sandbox.workspace == (tmp_path / "work").resolve()
    assert settings.sandbox.allow_unsandboxed is True
    assert settings.sandbox.timeout_seconds == 30
    assert settings.sandbox.max_output_bytes == 2048
    assert settings.sandbox.env_allowlist == ("JAVA_HOME",)
    assert settings.sandbox.env_set == {"APP_ENV": "test"}
    assert settings.sandbox.protected_workspace_paths == ("skills", "vendor")
    assert settings.sandbox.extra_read_only_mounts[0].destination == "/opt/ro"
    assert settings.state_path == (tmp_path / "state" / "agent.sqlite3").resolve()
    assert settings.config_dir == (tmp_path / "conf").resolve()
    assert settings.source_path == path.resolve()


def test_deep_agent_config_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "custom.yaml"
    path.write_text("llm:\n  model: from-env\n", encoding="utf-8")
    monkeypatch.setenv("DEEP_AGENT_CONFIG", str(path))
    settings = Settings.load(base_dir=tmp_path / "other")
    assert settings.llm_model == "from-env"
    assert resolve_config_path(base_dir=tmp_path / "other") == path.resolve()


def test_agent_instructions_rejects_non_text(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("agent:\n  instructions: [not, text]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="agent.instructions"):
        Settings.load(path)


def test_workspace_dot_follows_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    path = cfg_dir / "config.yaml"
    path.write_text("sandbox:\n  workspace: .\n", encoding="utf-8")
    monkeypatch.chdir(project)
    settings = Settings.load(path)
    assert settings.sandbox.workspace == project.resolve()


def test_workspace_relative_resolves_against_config_dir(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    path = tmp_path / "config.yaml"
    path.write_text("sandbox:\n  workspace: work\n", encoding="utf-8")
    settings = Settings.load(path)
    assert settings.sandbox.workspace == work.resolve()


def test_multi_model_catalog_and_prefix(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
llm:
  default: qwen-plus
  models:
    qwen-plus:
      model: qwen3.5-plus
      api_key: a
      base_url: http://a.test/v1
    qwen-fast:
      model: qwen-turbo
      api_key: b
      base_url: http://b.test/v1
""",
        encoding="utf-8",
    )
    settings = Settings.load(path)
    assert settings.llm_default == "qwen-plus"
    assert [item.id for item in settings.llm_profiles] == ["qwen-plus", "qwen-fast"]
    assert settings.get_profile("qwen-f").id == "qwen-fast"
    with pytest.raises(KeyError, match="Ambiguous"):
        settings.get_profile("qwen")
    with pytest.raises(KeyError, match="Unknown"):
        settings.get_profile("missing")


def test_model_input_capabilities(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "llm:\n  models:\n    vision:\n      model: qwen\n      input: [text, image]\n",
        encoding="utf-8",
    )
    profile = Settings.load(path).active_profile
    assert profile.input == ("text", "image")
    assert profile.supports_input("image")


def test_provider_selects_plain_chatopenai_and_rejects_unknown(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "llm:\n  models:\n    compatible:\n      model: local\n      provider: openai-compatible\n",
        encoding="utf-8",
    )
    model = build_chat_model(Settings.load(path).active_profile)
    assert type(model) is ChatOpenAI
    assert model.use_responses_api is False
    path.write_text("llm:\n  provider: unknown\n", encoding="utf-8")
    with pytest.raises(ValueError, match="provider"):
        Settings.load(path)
    assert isinstance(build_chat_model(Settings().active_profile), QwenChatOpenAI)


@pytest.mark.parametrize("value,match", [
    ("[]", "non-empty"),
    ("[image]", "include text"),
    ("[text, text]", "duplicate"),
    ("[text, audio]", "unsupported"),
])
def test_invalid_model_inputs(tmp_path: Path, value: str, match: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"llm:\n  models:\n    bad:\n      model: qwen\n      input: {value}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=match):
        Settings.load(path)


def test_invalid_default_model_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
llm:
  default: nope
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
    path.write_text("sandbox:\n  allow_unsandboxed: maybe\n", encoding="utf-8")
    with pytest.raises(ValueError, match="allow_unsandboxed"):
        Settings.load(path)
