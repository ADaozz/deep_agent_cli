"""Lazy offline and online Bubblewrap instances reused across sessions in one Runner.

Path checks and mount arguments stay in ``sandbox.py``. Each command is a
``RemoteCommand`` sent to the worker in ``worker.py``. Cancelling one
command does not shut the pool down; closing the Runner does.
"""

from __future__ import annotations

import atexit
import base64
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
import io
import itertools
import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from typing import Any, Callable, TypeVar
import uuid
import weakref

from agent.execution.service_observation import begin_command, end_command


LOG = logging.getLogger(__name__)
T = TypeVar("T")

START_TIMEOUT_SECONDS = 15.0
STOP_TIMEOUT_SECONDS = 2.0
_STDERR_LIMIT = 8192
_EVENT_QUEUE_SIZE = 256


class SandboxStartError(RuntimeError):
    """Bubblewrap 或沙箱内 Worker 未能启动。"""


class SandboxLostError(RuntimeError):
    """沙箱 Worker 已失效；正在执行的命令不得自动重试。"""


class StdinWriteUncertainError(TimeoutError):
    """等待 stdin ACK 超时：数据可能已部分或全部写入，结果未知且不得自动重试。"""

    def __init__(self, write_id: str, message: str) -> None:
        super().__init__(message)
        self.write_id = write_id


@dataclass
class _PendingWriteAck:
    event: threading.Event = field(default_factory=threading.Event)
    ok: bool = False
    error: str = ""


class _Launcher:
    """在一条常驻线程上启动所有 Bubblewrap 进程。

    ``--die-with-parent`` 依赖 PR_SET_PDEATHSIG：发出 fork 的那个*线程*退出时
    会触发。工具调用跑在短命的执行线程上；改由常驻线程 spawn，避免持久沙箱
    受某次 bwrap 实现如何投递该信号的影响。
    """

    def __init__(self) -> None:
        self._jobs: queue.SimpleQueue[tuple[Callable[[], Any], Future]] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def call(self, fn: Callable[[], T]) -> T:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._loop, name="deep-agent-sandbox-launcher", daemon=True,
                )
                self._thread.start()
        future: Future = Future()
        self._jobs.put((fn, future))
        return future.result()

    def _loop(self) -> None:
        while True:
            fn, future = self._jobs.get()
            try:
                future.set_result(fn())
            except BaseException as exc:  # noqa: BLE001
                future.set_exception(exc)


_LAUNCHER = _Launcher()


class RemoteCommand:
    """宿主侧句柄，对应持久沙箱里正在跑的一条命令。"""

    def __init__(
        self,
        sandbox: PersistentSandbox,
        request_id: int,
        *,
        stdin_mode: str = "none",
        generation: int = 0,
    ) -> None:
        self.sandbox = sandbox
        self.request_id = request_id
        self.stdin_mode = stdin_mode if stdin_mode in {"none", "pipe", "pty"} else "none"
        self.generation = generation
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=_EVENT_QUEUE_SIZE)
        self.released = False
        self.kill_requested = False
        self.owner_token = ""
        self._write_lock = threading.Lock()
        self._ack_lock = threading.Lock()
        self._write_ids = itertools.count(1)
        self._pending_acks: dict[str, _PendingWriteAck] = {}

    @property
    def execution_id(self) -> str:
        return f"{self.sandbox.id}:{self.generation}:{self.request_id}"

    def kill(self) -> None:
        """杀掉这条命令的进程组，沙箱本身继续运行。"""
        self.kill_requested = True
        try:
            self.sandbox._send({"op": "kill", "id": self.request_id})
        except SandboxLostError:
            pass

    # agent.runtime.cancellation 调用此方法，而不是对宿主 PID 发 killpg。
    cancel_execution = kill

    def write_stdin(self, data: bytes, *, timeout: float = 15.0) -> None:
        if self.stdin_mode == "none":
            raise ValueError("execution does not accept stdin")
        with self._write_lock:
            if self.released or self.sandbox.lost_reason is not None:
                raise OSError(self.sandbox.lost_reason or "execution has finished")
            write_id = f"{self.request_id}-{next(self._write_ids)}"
            pending = _PendingWriteAck()
            with self._ack_lock:
                self._pending_acks[write_id] = pending
            try:
                self.sandbox._send({
                    "op": "write",
                    "id": self.request_id,
                    "write_id": write_id,
                    "data": base64.b64encode(data).decode("ascii"),
                })
            except SandboxLostError as exc:
                with self._ack_lock:
                    self._pending_acks.pop(write_id, None)
                raise OSError(str(exc)) from exc
            # Pre-start and partial writes ACK only after the worker writes all bytes.
            completed = pending.event.wait(max(0.0, timeout))
            with self._ack_lock:
                self._pending_acks.pop(write_id, None)
                completed = completed or pending.event.is_set()
                ok = pending.ok
                error = pending.error
            if not completed:
                raise StdinWriteUncertainError(
                    write_id,
                    f"stdin write {write_id} timed out after {timeout:g}s waiting for acknowledgment; "
                    "outcome is unknown (bytes may have been partially or fully written and were not retried)",
                )
            if not ok:
                raise OSError(error or "stdin write failed")

    def _complete_write(self, *, write_id: str, ok: bool, error: str = "") -> None:
        if not write_id:
            return
        with self._ack_lock:
            pending = self._pending_acks.get(write_id)
            if pending is None or pending.event.is_set():
                return
            pending.ok = ok
            pending.error = error
            pending.event.set()

    def _fail_pending_writes(self, reason: str) -> None:
        with self._ack_lock:
            pending_items = list(self._pending_acks.values())
            for pending in pending_items:
                if not pending.event.is_set():
                    pending.ok = False
                    pending.error = reason
                    pending.event.set()

    def close_stdin(self) -> None:
        if self.stdin_mode == "none":
            raise ValueError("execution does not accept stdin")
        if self.stdin_mode == "pty":
            # Closing the PTY master is not a portable EOF; callers send terminal EOF bytes if needed.
            return
        if self.released or self.sandbox.lost_reason is not None:
            raise OSError(self.sandbox.lost_reason or "execution has finished")
        self.sandbox._send({"op": "close_stdin", "id": self.request_id})

    def resize(self, rows: int, cols: int) -> None:
        if self.stdin_mode != "pty":
            raise ValueError("resize requires a PTY execution")
        self.sandbox._send({
            "op": "resize", "id": self.request_id, "rows": int(rows), "cols": int(cols),
        })

    def next_event(self, timeout: float) -> tuple[str, Any] | None:
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            if self.sandbox.lost_reason is not None:
                return ("lost", self.sandbox.lost_reason)
            return None

    def release(self) -> None:
        self.released = True
        self._fail_pending_writes("execution has finished")
        self.sandbox._forget(self.request_id)
        if self.owner_token:
            end_command(self.owner_token)

    def _deliver(self, event: tuple[str, Any]) -> None:
        while not self.released:
            try:
                self.events.put(event, timeout=0.5)
                return
            except queue.Full:
                continue


class PersistentSandbox:
    """一个 Bubblewrap 实例，主进程是命令 Worker。"""

    def __init__(self, args: list[str], *, network: bool) -> None:
        self.args = args
        self.network = network
        self.id = f"bwrap-{'net' if network else 'offline'}-{uuid.uuid4().hex[:8]}"
        self.process: subprocess.Popen[bytes] | None = None
        self.lost_reason: str | None = None
        self.generation = 0
        self._ids = itertools.count(1)
        self._commands: dict[int, RemoteCommand] = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._ready = threading.Event()
        self._stderr: deque[bytes] = deque()
        self._stderr_size = 0
        self._threads: list[threading.Thread] = []

    @property
    def host_pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    @property
    def alive(self) -> bool:
        return (
            self.process is not None and self.lost_reason is None
            and self.process.poll() is None
        )

    def start(self, timeout: float = START_TIMEOUT_SECONDS) -> None:
        try:
            self.process = _LAUNCHER.call(lambda: subprocess.Popen(  # noqa: S603
                self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
            ))
        except OSError as exc:
            raise SandboxStartError(f"{type(exc).__name__}: {exc}") from exc
        for target, name in ((self._read_control, "control"), (self._read_stderr, "stderr")):
            thread = threading.Thread(target=target, name=f"{self.id}-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)
        deadline = time.monotonic() + timeout
        while not self._ready.wait(0.05):
            if self.lost_reason is not None or time.monotonic() >= deadline:
                break
        if self._ready.is_set() and self.lost_reason is None:
            return
        timed_out = self.lost_reason is None
        self.stop("startup failed")
        code = self.process.returncode
        detail = self.stderr_text().strip() or "no diagnostic output"
        if timed_out:
            raise SandboxStartError(f"sandbox worker did not become ready within {timeout:g}s: {detail}")
        raise SandboxStartError(f"bubblewrap exited with code {code}: {detail}")

    def start_command(self, command: str, *, stdin_mode: str = "none") -> RemoteCommand:
        mode = stdin_mode if stdin_mode in {"none", "pipe", "pty"} else "none"
        return self._start({"op": "run", "command": command, "stdin_mode": mode}, stdin_mode=mode)

    def start_argv(self, argv: list[str], cwd: str, *,
                   mount_identity: tuple[str, int, int] | None = None,
                   file: str | None = None) -> RemoteCommand:
        """Run argv directly; optional search paths are opened without following links."""
        return self._start({"op": "run_argv", "argv": argv, "cwd": cwd,
                            "mount_identity": mount_identity, "file": file})

    def _start(self, request: dict[str, Any], *, stdin_mode: str = "none") -> RemoteCommand:
        with self._lock:
            if self.lost_reason is not None:
                raise SandboxLostError(self.lost_reason)
            handle = RemoteCommand(
                self, next(self._ids), stdin_mode=stdin_mode, generation=self.generation,
            )
            self._commands[handle.request_id] = handle
        try:
            if request["op"] == "run":
                handle.owner_token = begin_command()
                request["owner"] = handle.owner_token
            self._send({**request, "id": handle.request_id})
        except SandboxLostError:
            handle.release()
            raise
        return handle

    def stop(self, reason: str = "sandbox stopped", timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """关闭控制通道并回收整个沙箱。"""
        self._mark_lost(reason)
        process = self.process
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _killpg(process.pid)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                LOG.warning("sandbox %s did not exit after SIGKILL", self.id)
        _killpg(process.pid)
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=timeout)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def stderr_text(self) -> str:
        return b"".join(self._stderr).decode("utf-8", errors="replace")

    def _send(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        process = self.process
        if process is None or process.stdin is None or self.lost_reason is not None:
            raise SandboxLostError(self.lost_reason or "sandbox is not running")
        with self._send_lock:
            try:
                process.stdin.write(data)
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                self._mark_lost(f"sandbox control channel closed ({type(exc).__name__})")
                raise SandboxLostError(self.lost_reason or "sandbox is not running") from exc

    def _forget(self, request_id: int) -> None:
        with self._lock:
            self._commands.pop(request_id, None)

    def _mark_lost(self, reason: str) -> None:
        with self._lock:
            if self.lost_reason is None:
                self.lost_reason = reason
                self.generation += 1
            commands = list(self._commands.values())
        for handle in commands:
            handle._fail_pending_writes(self.lost_reason or reason)
            try:
                handle.events.put_nowait(("lost", self.lost_reason))
            except queue.Full:
                pass

    def _read_control(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        stream = self.process.stdout
        reason = "sandbox worker exited unexpectedly"
        try:
            for line in stream:
                try:
                    message = json.loads(line)
                    op = message["op"]
                except (ValueError, KeyError, TypeError):
                    reason = "sandbox worker sent a malformed control message"
                    break
                if op == "ready":
                    self._ready.set()
                    continue
                with self._lock:
                    handle = self._commands.get(int(message.get("id", -1)))
                if handle is None:
                    continue
                if op == "out":
                    handle._deliver(("err" if message.get("stream") == "stderr" else "out",
                                     base64.b64decode(message["data"])))
                elif op == "exit":
                    handle._deliver(("exit", int(message["code"])))
                elif op == "spawn_error":
                    handle._deliver(("spawn_error", str(message.get("message", ""))))
                elif op == "write_status":
                    write_id = message.get("write_id")
                    handle._complete_write(
                        write_id="" if write_id is None else str(write_id),
                        ok=bool(message.get("ok")),
                        error=str(message.get("error") or ""),
                    )
        except OSError:
            pass
        except (ValueError, KeyError, TypeError):
            reason = "sandbox worker sent a malformed control message"
        self._mark_lost(reason)
        if self.process.poll() is None:
            _killpg(self.process.pid)

    def _read_stderr(self) -> None:
        assert self.process is not None
        stderr = self.process.stderr
        assert isinstance(stderr, io.BufferedReader)
        try:
            for chunk in iter(lambda: stderr.read1(4096), b""):
                self._stderr.append(chunk)
                self._stderr_size += len(chunk)
                while self._stderr_size > _STDERR_LIMIT and len(self._stderr) > 1:
                    self._stderr_size -= len(self._stderr.popleft())
        except (OSError, ValueError):
            pass


class SandboxPool:
    """每个 Runner 最多一个无网沙箱、一个有网沙箱。

    都在首次使用时创建，并在 Runner 内跨会话复用。权限模式只决定之后的命令要不要审批，
    不销毁沙箱。
    """

    def __init__(self, launch_args: Callable[[bool], list[str]]) -> None:
        from agent.execution.lifecycle import ExecutionRegistry

        self._launch_args = launch_args
        self._lock = threading.RLock()
        self._sandboxes: dict[bool, PersistentSandbox] = {}
        self.executions = ExecutionRegistry()
        _POOLS.add(self)

    def sandbox(self, network: bool) -> PersistentSandbox | None:
        """指定网络模式下正在运行的持久沙箱；没有则返回 None。"""
        with self._lock:
            current = self._sandboxes.get(network)
            return current if current is not None and current.alive else None

    def run(self, network: bool, fn: Callable[[PersistentSandbox], T], *, deadline: float | None = None) -> T:
        if deadline is None:
            self._lock.acquire()
        elif not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise TimeoutError("sandbox acquisition timed out")
        try:
            sandbox = self._acquire(network, deadline=deadline)
        finally:
            self._lock.release()
        return fn(sandbox)

    def discard(self, sandbox: PersistentSandbox, reason: str) -> None:
        with self._lock:
            if self._sandboxes.get(sandbox.network) is sandbox:
                del self._sandboxes[sandbox.network]
        sandbox.stop(reason)

    def stop(self, network: bool, reason: str) -> None:
        with self._lock:
            sandbox = self._sandboxes.pop(network, None)
        if sandbox is not None:
            sandbox.stop(reason)

    def shutdown(self, reason: str = "agent session ended") -> None:
        """销毁本 Pool 的全部沙箱。"""
        self.executions.shutdown()
        with self._lock:
            sandboxes = list(self._sandboxes.values())
            self._sandboxes.clear()
        for sandbox in sandboxes:
            sandbox.stop(reason)

    def _acquire(self, network: bool, *, deadline: float | None = None) -> PersistentSandbox:
        current = self._sandboxes.get(network)
        if current is not None and current.alive:
            return current
        if current is not None:
            # 失效沙箱上正在执行的命令不得重放。
            LOG.warning("sandbox %s is unhealthy (%s); starting a new one", current.id, current.lost_reason)
            del self._sandboxes[network]
            current.stop(current.lost_reason or "sandbox unhealthy")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("sandbox acquisition timed out")
        sandbox = PersistentSandbox(self._launch_args(network), network=network)
        if deadline is None:
            sandbox.start()
        else:
            sandbox.start(timeout=min(START_TIMEOUT_SECONDS, max(0.0, deadline - time.monotonic())))
        self._sandboxes[network] = sandbox
        return sandbox


def _killpg(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


_POOLS: weakref.WeakSet[SandboxPool] = weakref.WeakSet()


@atexit.register
def _shutdown_all_pools() -> None:
    for pool in list(_POOLS):
        try:
            pool.shutdown("process exiting")
        except Exception:  # noqa: BLE001
            pass
