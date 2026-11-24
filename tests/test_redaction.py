from agent.redaction import SecretRef, known_secret_refs, replace_known_secrets


def test_longest_first_and_stable_ties():
    refs = (
        SecretRef("short", "token"),
        SecretRef("long", "token-long"),
        SecretRef("short-again", "token"),
    )
    assert replace_known_secrets("token-long token", refs) == "$long $short"
    assert replace_known_secrets("abc", (
        SecretRef("bc", "bc"),
        SecretRef("ab", "ab"),
    )) == "a$bc"


def test_known_secret_refs_config_keys_and_replacements(tmp_path):
    from agent.bootstrap.config import Settings
    from agent.execution.policy import SandboxConfig
    from agent.models.profile import ModelProfile

    settings = Settings(
        resolved_deepseek_api_key="deepseek-key",
        llm_profiles=(
            ModelProfile("local/mock", "mock", api_key=" profile-key\n", source="openai"),
            ModelProfile("deepseek/chat", "deepseek-chat", api_key="deepseek-key", source="deepseek"),
        ),
        llm_default="local/mock",
        redaction_replacements=(("sk-XXXX", "密钥"),),
    )
    sandbox = SandboxConfig(
        workspace=tmp_path,
        env_set={"APP_SECRET": "env-set-secret", "FLAG": "1"},
        env_allowlist=("APP_TOKEN", "PATH", "MISSING"),
    )
    refs = known_secret_refs(settings, "deepseek-key", sandbox, min_length=8)
    by_value = {ref.value: ref.placeholder for ref in refs}
    assert by_value["profile-key"] == "$llm.models.openai.models.mock.api_key"
    assert by_value["deepseek-key"] == "$llm.models.deepseek.api_key"
    assert by_value["sk-XXXX"] == "密钥"
    # Sandbox env values are ignored even when a sandbox config is passed.
    assert "env-set-secret" not in by_value
    assert "1" not in by_value


def test_raw_secret_ref_keeps_custom_display_name():
    assert SecretRef("密钥", "sk-XXXX", raw=True).placeholder == "密钥"
    assert SecretRef("$自定义", "literal", raw=True).placeholder == "$自定义"
    assert SecretRef("llm.models.deepseek.api_key", "key").placeholder == "$llm.models.deepseek.api_key"
