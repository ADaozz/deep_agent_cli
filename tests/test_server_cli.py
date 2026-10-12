"""The optional server command works without bootstrapping the terminal UI."""
import sys

import pytest

from agent.cli import main as cli
from agent.server import main as server


def test_serve_dispatch_skips_terminal_bootstrap(monkeypatch):
    captured = []
    monkeypatch.setattr(server, "main", lambda args: captured.append(args))
    monkeypatch.setattr(cli, "initialize_user_files", lambda: pytest.fail("serve must not bootstrap CLI files"))
    cli.main(["serve", "--config", "/tmp/service.yaml", "--port", "8081"])
    assert captured == [["--config", "/tmp/service.yaml", "--port", "8081"]]


def test_server_help_does_not_require_optional_dependencies(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--help"])
    assert exc.value.code == 0
    assert "--config" in capsys.readouterr().out


def test_server_missing_dependency_has_install_hint(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    with pytest.raises(SystemExit) as exc:
        server.main(["--config", "/tmp/not-read.yaml"])
    assert exc.value.code == 2
    assert 'deep-agent-cli[server]' in capsys.readouterr().err
