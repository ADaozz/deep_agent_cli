"""Smoke entrypoints: configuration is explicit and live calls remain opt-in."""
import argparse
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from agent.config import ModelProfile, Settings
from examples import e2e_live_smoke as live
from examples import stream_smoke as stream


def test_live_help_does_not_load_config(monkeypatch):
    monkeypatch.setattr(live.Settings, "load", lambda: pytest.fail("config loaded"))
    with pytest.raises(SystemExit) as error:
        live.main(["--help"])
    assert error.value.code == 0


def test_live_workspace_and_model_are_configurable_and_resources_closed(tmp_path, monkeypatch):
    settings = Settings(llm_profiles=(ModelProfile("custom/one", "one"), ModelProfile("custom/two", "two")), llm_default="custom/one")
    monkeypatch.setattr(live.Settings, "load", lambda: settings)
    seen = []
    closed = []
    class Runner:
        def __init__(self, *, settings, **_kwargs):
            seen.append(settings)
        def close(self):
            closed.append(True)
    async def slash(*_args):
        return "session"
    class App:
        def __init__(self, *_args, **_kwargs):
            from types import SimpleNamespace
            self._io_executor = SimpleNamespace(shutdown=lambda **_kwargs: None)
    monkeypatch.setattr(live, "AgentRunner", Runner)
    monkeypatch.setattr(live, "CliApplication", App)
    monkeypatch.setattr(live, "run_slash_suite", slash)
    monkeypatch.setattr(live, "run_permission_suite", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(live, "run_coding_suite", lambda *_args: None)
    args = argparse.Namespace(model="custom/two", workspace=None, network=False)
    assert asyncio.run(live.async_main(args)) == 0
    assert len(seen) == len(closed) == 3
    assert all(config.active_profile.id == "custom/two" for config in seen)
    workspace = seen[0].sandbox.workspace
    assert workspace != settings.sandbox.workspace
    assert not workspace.exists()
    assert not seen[0].state_path.exists()
    assert not seen[0].sandbox.allow_unsandboxed


def test_live_refuses_nonempty_workspace(tmp_path, monkeypatch):
    (tmp_path / "keep.txt").write_text("keep")
    monkeypatch.setattr(live.Settings, "load", lambda: Settings())
    args = argparse.Namespace(model=None, workspace=tmp_path, network=False)
    assert asyncio.run(live.async_main(args)) == 2
    assert (tmp_path / "keep.txt").read_text() == "keep"


@pytest.mark.parametrize("provider,effort", [("openai-compatible", None), ("qwen-responses", "none"), ("qwen-responses", "high")])
def test_stream_smoke_rejects_wrong_profile_or_effort_before_api(monkeypatch, provider, effort):
    settings = Settings(llm_profiles=(ModelProfile("test", "test", provider=provider, reasoning_efforts=("low",)),), llm_default="test")
    monkeypatch.setattr(stream.Settings, "load", lambda: settings)
    monkeypatch.setattr(stream, "build_chat_model", lambda *_args, **_kwargs: pytest.fail("model built"))
    with pytest.raises(SystemExit) as error:
        stream.main([] if effort is None else ["--effort", effort])
    assert error.value.code == 2
