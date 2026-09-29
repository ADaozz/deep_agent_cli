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
    assert Settings.load().source_path == path


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
    assert "Usage: deep-agent" in capsys.readouterr().out
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
    with pytest.raises(SystemExit) as error:
        run_cli.main([])
    assert error.value.code == 2
    output = capsys.readouterr().err
    assert "Configuration error:" in output
    assert "config.yaml" in output
    assert "llm.default" in output
    assert "Traceback" not in output
