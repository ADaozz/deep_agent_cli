"""Persistent Bubblewrap sandboxes reused across execute calls.

These tests start real sandboxes; they skip when Bubblewrap cannot create
namespaces here (set REQUIRE_BWRAP_TEST=1 to make that a failure).
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
import os
from pathlib import Path
import signal
import socket
import threading
import time
import uuid

import pytest
from langchain_core.messages import AIMessage

from agent.cancel import ToolCancelContext, clear_cancel_context, set_cancel_context
from agent.config import SandboxConfig
from agent.factory import create_agent
from agent.network import reset_execute_network, set_execute_network
from agent.runner import AgentRunner
from agent.sandbox import SandboxUnavailableError, WorkspaceCompositeBackend, select_backend
from agent.sandbox_pool import SandboxPool
from agent.session import SessionStore
from tests.conftest import scripted_model

pytestmark = pytest.mark.sandbox


def _bwrap_config(workspace: Path) -> SandboxConfig:
    import shutil

    executable = shutil.which("bwrap")
    if executable is None:
        if os.environ.get("REQUIRE_BWRAP_TEST"):
            pytest.fail("bubblewrap is required in CI")
        pytest.skip("UNSUPPORTED SANDBOX ENVIRONMENT [not_installed]: bubblewrap is not installed")
    return SandboxConfig(workspace=workspace, bwrap_path=executable)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def backend(workspace: Path) -> Iterator[WorkspaceCompositeBackend]:
    try:
        selected = select_backend(_bwrap_config(workspace))
    except SandboxUnavailableError as exc:
        if os.environ.get("REQUIRE_BWRAP_TEST"):
            pytest.fail(str(exc))
        pytest.skip(f"UNSUPPORTED SANDBOX ENVIRONMENT [{exc.kind}]: {exc}")
    assert isinstance(selected.backend, WorkspaceCompositeBackend)
    yield selected.backend
    selected.backend.executor.pool.shutdown("test finished")


def _pool(backend: WorkspaceCompositeBackend) -> SandboxPool:
    return backend.executor.pool


def run(backend, command: str, *, network: bool = False, timeout: int | None = None):
    token = set_execute_network(network)
    try:
        return backend.execute(command, timeout=timeout) if timeout else backend.execute(command)
    finally:
        reset_execute_network(token)


def _host_pids_with_arg(marker: str) -> list[int]:
    """Host PIDs whose argv contains ``marker`` (sandbox PIDs are namespaced)."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            argv = Path(f"/proc/{entry}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if marker.encode() in argv:
            found.append(int(entry))
    return found


def _descendants(pid: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        ppid = int(stat.rsplit(")", 1)[1].split()[1])
        children.setdefault(ppid, []).append(int(entry))
    result: set[int] = set()
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in result:
                result.add(child)
                stack.append(child)
    return result


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


def _wait_gone(pids, timeout: float = 5.0) -> list[int]:
    deadline = time.monotonic() + timeout
    remaining = [pid for pid in pids if _alive(pid)]
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining = [pid for pid in remaining if _alive(pid)]
    return remaining


def _sleep_marker() -> str:
    # A unique duration lets the host find this exact process by its argv.
    return f"{9000 + uuid.uuid4().int % 900}.{uuid.uuid4().int % 10**6}"


def _start_background_sleep(backend, *, network: bool = False) -> str:
    marker = _sleep_marker()
    result = run(backend, f"nohup sleep {marker} >/dev/null 2>&1 &", network=network)
    assert result.exit_code == 0, result.output
    deadline = time.monotonic() + 5
    while not _host_pids_with_arg(marker) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _host_pids_with_arg(marker), "background process did not start"
    return marker


def test_lazy_creation(backend) -> None:
    pool = _pool(backend)
    # The startup preflight must not leave a sandbox behind.
    assert pool.sandbox(False) is None
    assert pool.sandbox(True) is None
    assert run(backend, "true").exit_code == 0
    assert pool.sandbox(False) is not None
    assert pool.sandbox(True) is None


def test_reuse_same_sandbox(backend) -> None:
    assert run(backend, "echo state > /tmp/marker && cd /tmp && FOO=1").exit_code == 0
    first = _pool(backend).sandbox(False)
    result = run(backend, 'cat /tmp/marker; pwd; echo "foo=${FOO:-unset}"')
    assert result.exit_code == 0, result.output
    # Sandbox state survives; shell state (cwd, variables) does not.
    assert result.output.splitlines() == ["state", "/workspace", "foo=unset"]
    second = _pool(backend).sandbox(False)
    assert second is first
    assert second.host_pid == first.host_pid


def test_dual_sandbox_isolation(backend) -> None:
    pool = _pool(backend)
    assert run(backend, "echo offline > /tmp/side; echo offline > /home/agent/side").exit_code == 0
    online = run(
        backend,
        "test ! -e /tmp/side && test ! -e /home/agent/side && "
        "echo online > /tmp/side && echo online > /home/agent/side",
        network=True,
    )
    assert online.exit_code == 0, online.output
    offline_box, online_box = pool.sandbox(False), pool.sandbox(True)
    assert offline_box is not None and online_box is not None
    assert offline_box.host_pid != online_box.host_pid

    assert run(backend, "cat /tmp/side /home/agent/side").output.split() == ["offline", "offline"]
    assert run(backend, "cat /tmp/side /home/agent/side", network=True).output.split() == ["online", "online"]
    # Separate PID namespaces: each sees only its own processes.
    offline_ps = run(backend, "ls /proc | grep -c '^[0-9]'").output
    assert int(offline_ps.split()[0]) < 10
    assert pool.sandbox(False) is offline_box
    assert pool.sandbox(True) is online_box


def test_workspace_persistence(backend, workspace: Path) -> None:
    assert run(backend, "echo from-offline > shared.txt").exit_code == 0
    assert run(backend, "cat shared.txt && echo from-online >> shared.txt", network=True).output == "from-offline\n"
    assert run(backend, "cat /workspace/shared.txt").output.split() == ["from-offline", "from-online"]
    assert (workspace / "shared.txt").read_text().split() == ["from-offline", "from-online"]


def test_background_process_survives(backend) -> None:
    port = 20000 + uuid.uuid4().int % 20000
    started = run(
        backend,
        f"nohup python3 -m http.server {port} --bind 127.0.0.1 > /tmp/http.log 2>&1 & echo $! > /tmp/http.pid",
    )
    assert started.exit_code == 0, started.output
    result = run(backend, (
        'kill -0 "$(cat /tmp/http.pid)" && python3 -c "'
        "import time, urllib.request\n"
        "for _ in range(100):\n"
        "    try:\n"
        f"        print(urllib.request.urlopen('http://127.0.0.1:{port}/').status); break\n"
        "    except OSError: time.sleep(0.05)\n"
        '"'
    ))
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "200"
    assert run(backend, 'kill -0 "$(cat /tmp/http.pid)"').exit_code == 0


def test_network_isolation(backend) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    probe = (
        "python3 -c \"import socket; s = socket.create_connection(('127.0.0.1', %d), timeout=2); "
        "print('connected')\"" % port
    )
    interfaces = "tail -n +3 /proc/net/dev | cut -d: -f1 | tr -d ' '"
    try:
        offline = run(backend, probe)
        assert offline.exit_code != 0
        assert "connected" not in offline.output
        assert run(backend, interfaces).output.split() == ["lo"]

        online = run(backend, probe, network=True)
        assert online.exit_code == 0, online.output
        assert online.output.strip() == "connected"
        assert run(backend, interfaces, network=True).output.split() != ["lo"]
    finally:
        server.close()


def test_cross_sandbox_ipc_isolation(backend) -> None:
    pool = _pool(backend)
    name = f"deep-agent-ipc-{uuid.uuid4().hex}"
    listener = (
        "import os, socket, time\n"
        "a = socket.socket(socket.AF_UNIX); a.bind('\\0%s'); a.listen()\n"
        "f = socket.socket(socket.AF_UNIX); f.bind('/tmp/%s.sock'); f.listen()\n"
        "time.sleep(300)\n"
    ) % (name, name)
    started = run(
        backend,
        f"printf '%s' {_shell_quote(listener)} > /tmp/listen.py && "
        f"nohup python3 /tmp/listen.py {name} >/dev/null 2>&1 &",
        network=True,
    )
    assert started.exit_code == 0, started.output
    connect = (
        "python3 -c \"import socket, sys, time\n"
        "for _ in range(100):\n"
        "    try:\n"
        "        socket.socket(socket.AF_UNIX).connect('\\0%s'); print('reachable'); sys.exit(0)\n"
        "    except OSError: time.sleep(0.05)\n"
        "print('unreachable'); sys.exit(1)\"" % name
    )
    # Positive control: the listener is reachable from its own sandbox.
    assert run(backend, connect, network=True).output.strip() == "reachable"

    offline = run(backend, connect.replace("range(100)", "range(3)"))
    assert offline.exit_code == 1 and offline.output.startswith("unreachable"), offline.output
    assert run(backend, f"test ! -e /tmp/{name}.sock").exit_code == 0
    # The offline sandbox can neither see the networked worker or listener...
    own = run(backend, "cat /proc/[0-9]*/cmdline 2>/dev/null | tr '\\0' ' '").output
    assert name not in own
    online_box = pool.sandbox(True)
    assert online_box is not None and str(online_box.host_pid) not in run(backend, "ls /proc").output.split()
    # ...nor find a control channel in the shared workspace...
    assert run(backend, "find /workspace \\( -type s -o -type p \\) -print").output == "<no output>"
    # ...nor hijack its own worker's control pipes through /proc.
    hijack = run(backend, "ls /proc/2/fd")
    assert hijack.exit_code != 0 and "Permission denied" in hijack.output


def _shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _run_in_thread(backend, command: str, *, ctx: ToolCancelContext | None = None, **kwargs):
    outcome: dict[str, object] = {}

    def target() -> None:
        if ctx is not None:
            set_cancel_context(ctx)
        try:
            outcome["result"] = run(backend, command, **kwargs)
        finally:
            clear_cancel_context()

    thread = threading.Thread(target=target)
    thread.start()
    return thread, outcome


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.05)


def test_cancel_preserves_sandbox(backend, workspace: Path) -> None:
    assert run(backend, "touch /tmp/kept").exit_code == 0
    sandbox = _pool(backend).sandbox(False)
    marker = _sleep_marker()
    ctx = ToolCancelContext("execute", "call-1", threading.Event())
    thread, outcome = _run_in_thread(backend, f"echo started > started.txt; sleep {marker}", ctx=ctx)
    _wait_for(lambda: bool(_host_pids_with_arg(marker)))
    ctx.request_cancel()
    thread.join(timeout=10)
    result = outcome["result"]
    assert result.exit_code == 130
    assert result.termination_reason == "cancelled"
    assert _wait_gone(_host_pids_with_arg(marker)) == []
    assert _pool(backend).sandbox(False) is sandbox
    assert run(backend, "test -e /tmp/kept && echo ok").output == "ok\n"


def test_timeout_preserves_sandbox(backend) -> None:
    assert run(backend, "touch /tmp/kept").exit_code == 0
    sandbox = _pool(backend).sandbox(False)
    started = time.monotonic()
    result = run(backend, "sleep 30", timeout=1)
    assert time.monotonic() - started < 5
    assert result.exit_code == 124
    assert result.termination_reason == "timeout"
    assert _pool(backend).sandbox(False) is sandbox
    assert run(backend, "test -e /tmp/kept && echo ok").output == "ok\n"


def test_worker_crash_recovery(backend, workspace: Path) -> None:
    assert run(backend, "touch /tmp/before").exit_code == 0
    crashed = _pool(backend).sandbox(False)
    assert crashed is not None
    thread, outcome = _run_in_thread(backend, "echo run >> count.txt; sleep 60")
    _wait_for(lambda: (workspace / "count.txt").exists())
    os.killpg(crashed.host_pid, signal.SIGKILL)
    thread.join(timeout=10)
    result = outcome["result"]
    assert result.termination_reason == "sandbox_lost"
    assert result.exit_code not in (0, None)
    assert "not retried" in result.output

    recovered = run(backend, "test ! -e /tmp/before && cat count.txt")
    assert recovered.exit_code == 0, recovered.output
    # The lost command was not replayed in the new sandbox.
    assert recovered.output == "run\n"
    replacement = _pool(backend).sandbox(False)
    assert replacement is not None and replacement is not crashed


def test_concurrent_commands_share_one_sandbox(backend) -> None:
    assert run(backend, "true").exit_code == 0
    sandbox = _pool(backend).sandbox(False)
    started = time.monotonic()
    calls = [_run_in_thread(backend, f"sleep 1; echo {name}") for name in ("a", "b", "c")]
    for thread, _ in calls:
        thread.join(timeout=10)
    assert time.monotonic() - started < 2.5
    assert [outcome["result"].output for _, outcome in calls] == ["a\n", "b\n", "c\n"]
    assert _pool(backend).sandbox(False) is sandbox


def test_background_job_holding_output_pipe_does_not_block(backend) -> None:
    marker = _sleep_marker()
    started = time.monotonic()
    result = run(backend, f"sleep {marker} & echo done")
    assert time.monotonic() - started < 3
    assert result.exit_code == 0 and result.output == "done\n"
    assert _host_pids_with_arg(marker)


def test_ask_network_sandbox_persists_between_approved_commands(backend) -> None:
    # Under ask the control point is approving each command; the networked
    # sandbox keeps its state like the offline one.
    pool = _pool(backend)
    marker = _start_background_sleep(backend, network=True)
    assert run(backend, "echo x > /tmp/net", network=True).exit_code == 0
    online = pool.sandbox(True)
    assert online is not None
    assert run(backend, "cat /tmp/net", network=True).output == "x\n"
    assert pool.sandbox(True) is online
    assert _host_pids_with_arg(marker)


def _sandbox_runner(workspace: Path, messages: list[AIMessage], **kwargs) -> AgentRunner:
    try:
        return AgentRunner(
            model=scripted_model(messages), sandbox_config=_bwrap_config(workspace), **kwargs,
        )
    except SandboxUnavailableError as exc:
        if os.environ.get("REQUIRE_BWRAP_TEST"):
            pytest.fail(str(exc))
        pytest.skip(f"UNSUPPORTED SANDBOX ENVIRONMENT [{exc.kind}]: {exc}")


def _sandbox_trees(pool: SandboxPool) -> set[int]:
    pids: set[int] = set()
    for network in (False, True):
        sandbox = pool.sandbox(network)
        if sandbox is not None:
            pids |= {sandbox.host_pid, *_descendants(sandbox.host_pid)}
    return pids


def test_session_cleanup(workspace: Path, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    runner = _sandbox_runner(workspace, [AIMessage(content="done")], session_store=store)
    pool = runner.sandbox_pool
    assert pool is not None
    backend = runner.prepared.backend
    first_thread = runner.thread_id

    def populate() -> tuple[set[int], list[str]]:
        markers = [_start_background_sleep(backend), _start_background_sleep(backend, network=True)]
        pids = _sandbox_trees(pool)
        assert len(pids) >= 4
        return pids, markers

    def assert_all_gone(pids: set[int], markers: list[str]) -> None:
        assert _wait_gone(pids) == []
        assert all(not _host_pids_with_arg(marker) for marker in markers)
        assert pool.sandbox(False) is None and pool.sandbox(True) is None

    pids, markers = populate()
    second = runner.new_session()
    assert_all_gone(pids, markers)

    pids, markers = populate()
    runner.switch_session(first_thread)
    assert runner.thread_id == first_thread != second.id
    assert_all_gone(pids, markers)

    pids, markers = populate()
    runner.close()
    assert_all_gone(pids, markers)
    store.close()


def test_model_rebuild_keeps_sandbox(workspace: Path) -> None:
    runner = _sandbox_runner(workspace, [AIMessage(content="done")])
    try:
        pool = runner.sandbox_pool
        assert run(runner.prepared.backend, "touch /tmp/kept").exit_code == 0
        sandbox = pool.sandbox(False)
        runner._rebuild_prepared(model=scripted_model([AIMessage(content="other")]))
        assert runner.sandbox_pool is pool
        assert pool.sandbox(False) is sandbox
        assert run(runner.prepared.backend, "test -e /tmp/kept").exit_code == 0
    finally:
        runner.close()


def test_permission_change_keeps_sandboxes(workspace: Path) -> None:
    marker = _sleep_marker()
    runner = _sandbox_runner(workspace, [
        AIMessage(content="", tool_calls=[{
            "id": "bg-1", "name": "execute",
            "args": {"command": f"nohup sleep {marker} >/dev/null 2>&1 &"},
        }]),
        AIMessage(content="started"),
        AIMessage(content="done"),
        AIMessage(content="done"),
    ])
    try:
        from agent.permission import PermissionMode

        pool = runner.sandbox_pool
        runner.set_permission_mode("allow")
        assert runner.invoke("start a background job").status == "completed"
        assert runner.permission_mode() is PermissionMode.ALLOW
        online = pool.sandbox(True)
        # allow runs execute with network and keeps that sandbox across calls,
        # even after the tool thread that spawned it has finished.
        time.sleep(0.3)
        assert online is not None and online.alive
        assert _host_pids_with_arg(marker)
        offline_marker = _start_background_sleep(runner.prepared.backend)
        offline = pool.sandbox(False)

        # Permission only decides whether later commands need approval; what
        # allow already started stays authorized and keeps running.
        runner.set_permission_mode("ask")
        assert runner.invoke("back to ask").status == "completed"
        assert runner.permission_mode() is PermissionMode.ASK
        assert pool.sandbox(True) is online
        assert pool.sandbox(False) is offline
        assert _host_pids_with_arg(marker)
        assert _host_pids_with_arg(offline_marker)

        runner.set_permission_mode("allow")
        assert runner.invoke("allow again").status == "completed"
        assert pool.sandbox(True) is online
        assert _host_pids_with_arg(marker)
    finally:
        runner.close()


def test_sandbox_start_failure_fails_closed(workspace: Path) -> None:
    config = replace(_bwrap_config(workspace), bwrap_path="/bin/false")
    with pytest.raises(SandboxUnavailableError):
        select_backend(config)


def test_execute_tool_reuses_sandbox_through_agent(workspace: Path) -> None:
    prepared = None
    try:
        selected = select_backend(_bwrap_config(workspace))
    except SandboxUnavailableError as exc:
        pytest.skip(str(exc))
    model = scripted_model([
        AIMessage(content="", tool_calls=[{
            "id": "w", "name": "execute", "args": {"command": "echo agent > /tmp/agent"},
        }]),
        AIMessage(content="", tool_calls=[{
            "id": "r", "name": "execute", "args": {"command": "cat /tmp/agent"},
        }]),
        AIMessage(content="done"),
    ])
    prepared = create_agent(model=model, backend=selected.backend, skills=[], interrupt_on={
        "execute": {"allowed_decisions": ["approve", "reject"]},
    })
    runner = AgentRunner(prepared=prepared, thread_id="reuse")
    try:
        assert runner.invoke("go").status == "waiting_confirmation"
        assert runner.approve_tool("w").status == "waiting_confirmation"
        events = []
        assert runner.approve_tool("r", on_event=events.append).status == "completed"
        completed = [event for event in events if event.type == "tool_completed" and event.tool_call_id == "r"]
        assert completed and completed[0].content.strip() == "agent"
    finally:
        runner.close()
