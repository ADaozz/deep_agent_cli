from pathlib import Path

import pytest

from agent.bootstrap import initialize_user_files
from agent.config import Settings


def test_first_launch_creates_template_and_skills_then_preserves_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DEEP_AGENT_CONFIG", raising=False)
    path = initialize_user_files(workspace=workspace)
    assert path == home / ".deep-agent" / "config.yaml"
    assert path.is_file()
    assert (home / ".deep-agent" / "skills").is_dir()
    assert path.stat().st_mode & 0o777 == 0o600
    assert (home / ".deep-agent" / "skills").stat().st_mode & 0o777 == 0o700
    original = path.read_bytes()
    assert initialize_user_files(workspace=workspace) is None
    assert path.read_bytes() == original
    # 主模板通过环境变量读取密钥；加载测试使用占位值，不调用真实端点。
    monkeypatch.setenv("TOKEN_PLAN_API_KEY", "test-template-key")
    loaded = Settings.load()
    assert loaded.source_path == path
    assert loaded.active_profile.id == "token-plan/qwen3.8-flash"
    assert loaded.active_profile.api == "chat_completions"


def test_existing_config_only_creates_missing_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    root = home / ".deep-agent"
    root.mkdir(parents=True)
    config = root / "config.yaml"
    config.write_text("{}\n")
    work = tmp_path / "project"
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DEEP_AGENT_CONFIG", raising=False)
    assert initialize_user_files(workspace=work) is None
    assert config.read_text() == "{}\n"
    assert (root / "skills").is_dir()


def test_init_rejects_home_workspace_and_skills_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DEEP_AGENT_CONFIG", raising=False)
    with pytest.raises(ValueError, match="outside workspace"):
        initialize_user_files(workspace=home)
    work = tmp_path / "project"
    work.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    root = home / ".deep-agent"
    root.mkdir()
    (root / "skills").symlink_to(target)
    with pytest.raises(ValueError, match="symlinks"):
        initialize_user_files(workspace=work)


def test_cli_first_run_exits_before_creating_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.cli import main as run_cli

    home = tmp_path / "home"
    work = tmp_path / "project"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DEEP_AGENT_CONFIG", raising=False)
    monkeypatch.chdir(work)
    monkeypatch.setattr(run_cli, "create_runner", lambda settings: pytest.fail("runner started"))
    run_cli.main([])
    assert (home / ".deep-agent" / "config.yaml").exists()
    assert not (home / ".deep-agent" / "sessions").exists()


def test_startup_args_parse_resume() -> None:
    from agent.cli.main import parse_startup_args

    assert parse_startup_args([]) == (None, False)
    assert parse_startup_args(["resume"]) == (None, True)
    assert parse_startup_args(["resume", "abc-123"]) == ("abc-123", True)


def test_help_does_not_bootstrap_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from agent.cli.main import main

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(SystemExit) as error:
        main(["--help"])
    assert error.value.code == 0
    assert "usage: deep-agent" in capsys.readouterr().out
    assert not (home / ".deep-agent").exists()


def test_cli_reports_invalid_model_config_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from agent.cli import main as run_cli

    home = tmp_path / "home"
    work = tmp_path / "project"
    config_dir = home / ".deep-agent"
    config_dir.mkdir(parents=True)
    work.mkdir()
    (config_dir / "config.yaml").write_text(
        "llm:\n  default: token-plan/missing\n  models:\n    token-plan:\n      models:\n        auto: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DEEP_AGENT_CONFIG", raising=False)
    monkeypatch.chdir(work)
    monkeypatch.setattr(run_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(run_cli.sys.stdout, "isatty", lambda: True)
    with pytest.raises(SystemExit) as error:
        run_cli.main([])
    assert error.value.code == 2
    output = capsys.readouterr().err
    assert "Configuration error:" in output
    assert "config.yaml" in output
    assert "llm.default" in output
    assert "Traceback" not in output


@pytest.mark.parametrize("stdin_tty,stdout_tty", [(False, True), (True, False), (False, False)])
def test_non_tty_rejected_before_config_load_or_runner(tmp_path, monkeypatch, capsys, stdin_tty, stdout_tty):
    from agent.cli import main as cli
    monkeypatch.setattr(cli, "initialize_user_files", lambda: None)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: stdin_tty)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: stdout_tty)
    monkeypatch.setattr(cli.Settings, "load", lambda: pytest.fail("configuration loaded"))
    monkeypatch.setattr(cli, "create_runner", lambda _: pytest.fail("runner created"))
    with pytest.raises(SystemExit) as error:
        cli.main([])
    assert error.value.code == 2
    assert "interactive terminal (TTY)" in capsys.readouterr().err


def test_pipe_first_launch_initializes_then_rejects_without_sessions(tmp_path):
    import os
    import subprocess
    import sys
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir(); work.mkdir()
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    env.pop("DEEP_AGENT_CONFIG", None)
    command = [sys.executable, "-m", "agent.cli.main"]
    first = subprocess.run(command, input="hi\n", text=True, capture_output=True, cwd=work, env=env, timeout=20)
    assert first.returncode == 0, first.stderr
    assert (home / ".deep-agent/config.yaml").is_file()
    second = subprocess.run(command, input="hi\n", text=True, capture_output=True, cwd=work, env=env, timeout=20)
    assert second.returncode == 2
    assert "interactive terminal (TTY)" in second.stderr
    assert "Traceback" not in second.stderr
    assert not (home / ".deep-agent/sessions").exists()


@pytest.mark.parametrize("args,expected", [(["--help"], "DEEP_AGENT_CONFIG"), (["--version"], "deep-agent"), (["resume", "--help"], "session_id")])
def test_information_flags_do_not_load_config_or_start_runner(monkeypatch, capsys, args, expected):
    from agent.cli import main as cli
    monkeypatch.setattr(cli, "initialize_user_files", lambda: pytest.fail("bootstrap invoked"))
    monkeypatch.setattr(cli, "create_runner", lambda _: pytest.fail("runner created"))
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 0
    assert expected in capsys.readouterr().out


def test_normal_tty_runs_and_closes_runner(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from agent.cli import main as cli
    from agent.sandbox import ExecutionMode
    calls = []
    monkeypatch.setattr(cli, "initialize_user_files", lambda: None)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    settings = Settings(sandbox=cli.replace(Settings().sandbox, workspace=tmp_path), config_dir=tmp_path.parent / "config")
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    runner = SimpleNamespace(prepared=SimpleNamespace(execution_mode=ExecutionMode.SANDBOXED), close=lambda: calls.append("closed"))
    monkeypatch.setattr(cli, "create_runner", lambda _: runner)
    class App:
        def __init__(self, *_args, **_kwargs):
            self.sessions = SimpleNamespace()
        def run(self):
            calls.append("ran")
        def continue_session_message(self):
            return ""
    monkeypatch.setattr(cli, "CliApplication", App)
    cli.main([])
    assert calls == ["ran", "closed"]
