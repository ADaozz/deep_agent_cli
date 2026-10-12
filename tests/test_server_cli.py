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


@pytest.mark.parametrize("workspace_setting", [None, "."])
def test_real_server_starts_from_configuration_directory(tmp_path, monkeypatch, workspace_setting):
    import os
    from pathlib import Path
    import signal
    import socket
    import subprocess
    import time
    import httpx
    import yaml
    from agent.config import SandboxConfig
    from agent.sandbox import SandboxUnavailableError, select_backend

    admin = tmp_path / "admin"
    project = tmp_path / "project"
    admin.mkdir()
    project.mkdir()
    model = admin / "model.yaml"
    data = {"llm": {"default": "test/model", "models": {"test": {
        "api_key": "${STARTUP_TEST_MODEL_KEY}", "models": {"model": {}},
    }}}, "paths": {"config_dir": str(admin / "agent-config")}}
    if workspace_setting is not None:
        data["sandbox"] = {"workspace": workspace_setting}
    model.write_text(yaml.safe_dump(data))
    service = admin / "service.yaml"
    service.write_text(yaml.safe_dump({"projects": {"test": str(project)},
                                      "model_config": str(model), "state_dir": str(admin / "state")}))
    # Probe actual prerequisites; never turn a server startup failure into a skip.
    env = {**os.environ, "STARTUP_TEST_MODEL_KEY": "test-key", "DEEP_AGENT_SERVICE_TOKEN": "startup-test-token",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    try:
        select_backend(SandboxConfig(workspace=project))
    except SandboxUnavailableError as exc:
        if os.environ.get("REQUIRE_BWRAP_TEST") == "1":
            pytest.fail(f"Required real server sandbox is unavailable: {exc}")
        pytest.skip(f"BLOCKED: real server needs Bubblewrap support: {exc}")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command = [sys.executable, "-m", "agent.server.main", "--config", str(service), "--port", str(port)]
    with (tmp_path / "startup.log").open("w+") as log:
        process = subprocess.Popen(command, cwd=admin, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 15
            with httpx.Client(trust_env=False, timeout=0.5) as client:
                while process.poll() is None and time.monotonic() < deadline:
                    try:
                        response = client.get(f"http://127.0.0.1:{port}/health")
                        if response.status_code == 200:
                            assert response.json() == {"status": "ok"}
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.03)
                else:
                    log.flush()
                    log.seek(0)
                    pytest.fail(f"Actual server did not start: {log.read()}")
        finally:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)
        log.seek(0)
        assert "Application shutdown complete." in log.read()
        # Recent Uvicorn restores/re-raises SIGTERM after completing shutdown.
        assert process.returncode in (0, -signal.SIGTERM)
