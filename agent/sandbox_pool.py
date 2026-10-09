"""按网络能力懒启动、会话内复用的 Bubblewrap 沙箱。"""
from __future__ import annotations

import atexit
import base64
from collections import deque
from concurrent.futures import Future
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

    def __init__(self, sandbox: PersistentSandbox, request_id: int) -> None:
        self.sandbox = sandbox
        self.request_id = request_id
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=_EVENT_QUEUE_SIZE)
        self.released = False
        self.kill_requested = False

    def kill(self) -> None:
        """杀掉这条命令的进程组，沙箱本身继续运行。"""
        self.kill_requested = True
        try:
            self.sandbox._send({"op": "kill", "id": self.request_id})
        except SandboxLostError:
            pass

    # agent.cancel 调用此方法，而不是对宿主 PID 发 killpg。
    cancel_execution = kill

    def next_event(self, timeout: float) -> tuple[str, Any] | None:
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            if self.sandbox.lost_reason is not None:
                return ("lost", self.sandbox.lost_reason)
            return None

    def release(self) -> None:
        self.released = True
        self.sandbox._forget(self.request_id)

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

    def start_command(self, command: str) -> RemoteCommand:
        with self._lock:
            if self.lost_reason is not None:
                raise SandboxLostError(self.lost_reason)
            handle = RemoteCommand(self, next(self._ids))
            self._commands[handle.request_id] = handle
        try:
            self._send({"op": "run", "id": handle.request_id, "command": command})
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
            commands = list(self._commands.values())
        for handle in commands:
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
                    handle._deliver(("out", base64.b64decode(message["data"])))
                elif op == "exit":
                    handle._deliver(("exit", int(message["code"])))
                elif op == "spawn_error":
                    handle._deliver(("spawn_error", str(message.get("message", ""))))
        except OSError:
            pass
        except (ValueError, KeyError, TypeError):
            reason = "sandbox worker sent a malformed control message"
        self._mark_lost(reason)
        if self.process.poll() is None:
            _killpg(self.process.pid)

    def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            for chunk in iter(lambda: self.process.stderr.read1(4096), b""):  # type: ignore[union-attr]
                self._stderr.append(chunk)
                self._stderr_size += len(chunk)
                while self._stderr_size > _STDERR_LIMIT and len(self._stderr) > 1:
                    self._stderr_size -= len(self._stderr.popleft())
        except (OSError, ValueError):
            pass


class SandboxPool:
    """每个 Agent 会话最多一个无网沙箱、一个有网沙箱。

    都在首次使用时创建，并在会话内复用。权限模式只决定之后的命令要不要审批，
    不销毁沙箱。
    """

    def __init__(self, launch_args: Callable[[bool], list[str]]) -> None:
        self._launch_args = launch_args
        self._lock = threading.RLock()
        self._sandboxes: dict[bool, PersistentSandbox] = {}
        _POOLS.add(self)

    def sandbox(self, network: bool) -> PersistentSandbox | None:
        """指定网络模式下正在运行的持久沙箱；没有则返回 None。"""
        with self._lock:
            current = self._sandboxes.get(network)
            return current if current is not None and current.alive else None

    def run(self, network: bool, fn: Callable[[PersistentSandbox], T]) -> T:
        with self._lock:
            sandbox = self._acquire(network)
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
        """销毁本会话的全部沙箱。"""
        with self._lock:
            sandboxes = list(self._sandboxes.values())
            self._sandboxes.clear()
        for sandbox in sandboxes:
            sandbox.stop(reason)

    def _acquire(self, network: bool) -> PersistentSandbox:
        current = self._sandboxes.get(network)
        if current is not None and current.alive:
            return current
        if current is not None:
            # 失效沙箱上正在执行的命令不得重放。
            LOG.warning("sandbox %s is unhealthy (%s); starting a new one", current.id, current.lost_reason)
            del self._sandboxes[network]
            current.stop(current.lost_reason or "sandbox unhealthy")
        sandbox = PersistentSandbox(self._launch_args(network), network=network)
        sandbox.start()
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
