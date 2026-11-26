"""Bubblewrap 沙箱的主进程：常驻命令 Worker。

宿主只读挂载本文件，用 ``python3 -I -S`` 运行；因此只能用标准库，不能 import ``agent``。

协议为按行分隔的 JSON。控制通道是继承来的 stdin/stdout 管道，不会暴露成
文件路径或 socket。

宿主 -> Worker: {"op": "run", "id": N, "command": str, "stdin_mode"?: "none"|"pipe"|"pty"}
                | {"op": "run_argv", "id": N, "argv": list[str], "cwd": str, ...}
                | {"op": "write", "id": N, "write_id": str, "data": base64}
                | {"op": "close_stdin", "id": N}
                | {"op": "resize", "id": N, "rows": int, "cols": int}
                | {"op": "interrupt", "id": N, "write_id": str}
                | {"op": "kill", "id": N}
Worker -> 宿主: {"op": "ready"} | {"op": "out", "id": N, "data": base64, "stream"?: str}
                | {"op": "exit", "id": N, "code": int}
                | {"op": "spawn_error", "id": N, "message": str}
                | {"op": "write_status", "id": N, "write_id": str, "ok": bool, "error"?: str}

stdin 读到 EOF 表示宿主已退出或要求关闭沙箱：Worker 退出，PID 命名空间会带走
沙箱里剩下的全部进程。
"""
import base64
from collections import deque
import fcntl
import json
import os
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time

WORKDIR = os.getcwd()
CHUNK_BYTES = 65536
POST_EXIT_DRAIN_SECONDS = 0.1
MAX_INPUT_BYTES = 1024 * 1024
_RESERVED_CHILD_ENV = frozenset({
    "PATH", "HOME", "PWD", "TMPDIR", "USER", "LOGNAME", "SHELL", "TERM",
    "LANG", "LC_ALL", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "NODE_OPTIONS",
    "BASH_ENV", "ENV", "IFS", "DEEP_AGENT_CONFIG", "DEEP_AGENT_EXECUTION_OWNER",
})


def _build_child_env(owner, extra_env):
    """Build per-command child env without mutating the persistent Worker's os.environ."""
    if not owner and not extra_env:
        return None
    merged = dict(os.environ)
    if isinstance(extra_env, dict):
        for key, val in extra_env.items():
            if not isinstance(key, str) or not isinstance(val, str):
                continue
            if not key or key in _RESERVED_CHILD_ENV or key.startswith("LC_"):
                continue
            merged[key] = val
    if owner:
        merged["DEEP_AGENT_EXECUTION_OWNER"] = owner
    return merged

_send_lock = threading.Lock()
_running_lock = threading.RLock()
_running: dict[int, subprocess.Popen[bytes]] = {}
_stdin_modes: dict[int, str] = {}
_pty_masters: dict[int, int] = {}
_inputs: dict[int, "_InputQueue"] = {}
_killed_before_start: set[int] = set()
_control_out = -1
_baseline_pids: frozenset[int] = frozenset()

# Child-side helper: new process, single-threaded — safe for setsid + TIOCSCTTY.
# argv: status_fd, slave_fd, command. Writes one status byte then execs.
_PTY_CHILD = (
    "import fcntl, os, signal, sys, termios\n"
    "status, slave = int(sys.argv[1]), int(sys.argv[2])\n"
    "os.setsid()\n"
    "try:\n"
    "    fcntl.ioctl(slave, termios.TIOCSCTTY, 0)\n"
    "except OSError as exc:\n"
    "    try:\n"
    "        os.write(status, b'E' + str(exc).encode('utf-8', 'replace')[:200])\n"
    "    except OSError:\n"
    "        pass\n"
    "    os._exit(71)\n"
    "try:\n"
    "    os.write(status, b'O')\n"
    "except OSError:\n"
    "    pass\n"
    "try:\n"
    "    os.close(status)\n"
    "except OSError:\n"
    "    pass\n"
    "os.dup2(slave, 0)\n"
    "os.dup2(slave, 1)\n"
    "os.dup2(slave, 2)\n"
    "if slave > 2:\n"
    "    os.close(slave)\n"
    "signal.signal(signal.SIGINT, signal.SIG_DFL)\n"
    "signal.signal(signal.SIGQUIT, signal.SIG_DFL)\n"
    "signal.signal(signal.SIGPIPE, signal.SIG_DFL)\n"
    "os.execvp('/bin/sh', ['/bin/sh', '-lc', sys.argv[3]])\n"
)


class _InputQueue:
    """Per-command byte-bounded queue unifying pre-start, active, partial writes, interrupt and EOF.

    Normal writes and EOF stay FIFO. Interrupt items are inserted at the front so
    Ctrl+C is drained by the same single stdin writer ahead of pending normal data.
    """

    def __init__(self, stdin_mode: str) -> None:
        self.stdin_mode = stdin_mode
        # Elements: ("write"|"interrupt", write_id, remaining_bytes, written_bytes) | ("eof", None, b"", 0)
        self.items: deque[tuple[str, str | None, bytes, int]] = deque()
        # Includes both queued items and remaining unwritten bytes of the active write.
        self.queued_bytes = 0
        self.eof_enqueued = False
        self.closed_for_new = False
        self.close_error = "process not running"
        self.killed = False
        self.wake_r: int | None = None
        self.wake_w: int | None = None

    def attach_wake(self, wake_r: int, wake_w: int) -> None:
        self.wake_r = wake_r
        self.wake_w = wake_w

    def wake(self) -> None:
        fd = self.wake_w
        if fd is not None:
            try:
                os.write(fd, b"\x01")
            except OSError:
                pass

    def requeue_partial(self, item: tuple[str, str | None, bytes, int]) -> None:
        """Re-queue an item after EAGAIN without displacing a pending interrupt.

        Normal writes keep FIFO order after any interrupts already waiting at the
        front; an interrupt that blocks mid-write stays at the front. Caller holds
        ``_running_lock``; ``queued_bytes`` already includes this remainder.
        """
        if item[0] == "interrupt":
            self.items.appendleft(item)
            return
        index = 0
        while index < len(self.items) and self.items[index][0] == "interrupt":
            index += 1
        self.items.insert(index, item)

    def drain_wake(self) -> None:
        fd = self.wake_r
        if fd is None:
            return
        while True:
            try:
                if not os.read(fd, 4096):
                    break
            except BlockingIOError:
                break
            except OSError:
                break

    def close_wake(self) -> None:
        for attr in ("wake_w", "wake_r"):
            fd = getattr(self, attr)
            if fd is not None:
                setattr(self, attr, None)
                try:
                    os.close(fd)
                except OSError:
                    pass


def _set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _send(message):
    data = (json.dumps(message, separators=(",", ":")) + "\n").encode("ascii")
    with _send_lock:
        view = memoryview(data)
        try:
            while view:
                view = view[os.write(_control_out, view):]
        except OSError:
            os._exit(0)


def _write_ack(request_id, write_id, *, ok, error="", written=0):
    payload = {
        "op": "write_status",
        "id": request_id,
        "write_id": "" if write_id is None else str(write_id),
        "ok": bool(ok),
        "error": error,
    }
    if written:
        payload["written"] = int(written)
    _send(payload)


def _kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _open_cwd(path, mount_identity):
    """Retain grep's no-symlink path checks inside the existing mount namespace."""
    if not isinstance(path, str) or not path.startswith("/") or ".." in path.split("/"):
        raise ValueError("invalid argv working directory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    current = ""
    try:
        for part in filter(None, path.split("/")):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            current += "/" + part
            if mount_identity and current == mount_identity[0]:
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != tuple(mount_identity[1:]):
                    raise ValueError("search mount changed; retry after restoring the workspace")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _register_input_queue(request_id: int, stdin_mode: str) -> _InputQueue:
    mode = stdin_mode if stdin_mode in {"none", "pipe", "pty"} else "none"
    with _running_lock:
        state = _InputQueue(mode)
        if request_id in _killed_before_start:
            _killed_before_start.discard(request_id)
            state.killed = True
            state.closed_for_new = True
            state.close_error = "process killed"
        _inputs[request_id] = state
        return state


def _fail_queue_items(request_id: int, state: _InputQueue, error: str, *, remove: bool = False) -> None:
    with _running_lock:
        state.closed_for_new = True
        state.close_error = error
        pending = list(state.items)
        state.items.clear()
        state.queued_bytes = 0
        if remove:
            if _inputs.get(request_id) is state:
                _inputs.pop(request_id, None)
    for kind, write_id, _payload, written in pending:
        if kind in {"write", "interrupt"}:
            _write_ack(request_id, write_id, ok=False, error=error, written=written)


def _run(
    request_id, command=None, argv=None, cwd=WORKDIR, mount_identity=None, file=None,
    owner=None, stdin_mode="none", env=None,
):
    descriptors = []
    structured = argv is not None
    stdin_mode = stdin_mode if stdin_mode in {"none", "pipe", "pty"} else "none"
    with _running_lock:
        state = _inputs.get(request_id)
        if state is None:
            state = _register_input_queue(request_id, stdin_mode)
    if structured and stdin_mode != "none":
        _fail_queue_items(request_id, state, "ValueError: interactive stdin is unsupported for argv runs", remove=True)
        _send({"op": "spawn_error", "id": request_id, "message": "ValueError: interactive stdin is unsupported for argv runs"})
        return
    with _running_lock:
        if state.killed:
            killed_before_spawn = True
        else:
            killed_before_spawn = False
    if killed_before_spawn:
        _fail_queue_items(request_id, state, "process killed before start", remove=True)
        _send({"op": "exit", "id": request_id, "code": 137})
        return

    master_fd = None
    slave_fd = None
    try:
        if structured:
            if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
                raise ValueError("argv must be a non-empty list of strings")
            requested_cwd = cwd
            directory_fd = _open_cwd(cwd, mount_identity)
            descriptors.append(directory_fd)
            cwd = f"/proc/self/fd/{directory_fd}"
            if file is not None:
                if not file or "/" in file or file in (".", "..") or argv[-1] != file:
                    raise ValueError("invalid search file")
                fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                descriptors.append(fd)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("search file is not a regular file")
                if mount_identity:
                    file_virtual = f"/{file}" if requested_cwd == "/" else f"{requested_cwd.rstrip('/')}/{file}"
                    if mount_identity[0] == file_virtual and (info.st_dev, info.st_ino) != tuple(mount_identity[1:]):
                        raise ValueError("search mount changed; retry after restoring the workspace")
                argv = [*argv[:-1], f"/proc/self/fd/{fd}"]
        child_env = _build_child_env(owner, env)
        popen_kwargs = {
            "env": child_env,
            "shell": False,
            "cwd": cwd,
            "start_new_session": True,
            "close_fds": True,
            "pass_fds": tuple(descriptors),
        }
        if stdin_mode == "pty":
            import pty as _pty
            master_fd, slave_fd = _pty.openpty()
            status_r, status_w = os.pipe()
            # Controlling-TTY setup runs in a fresh child process (not via preexec_fn).
            process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", _PTY_CHILD, str(status_w), str(slave_fd), command],
                env=child_env,
                shell=False,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=False,
                close_fds=True,
                pass_fds=tuple(descriptors) + (slave_fd, status_w),
            )
            os.close(slave_fd)
            slave_fd = None
            os.close(status_w)
            try:
                status = os.read(status_r, 256)
            except OSError:
                status = b""
            finally:
                os.close(status_r)
            if not status.startswith(b"O"):
                detail = status[1:].decode("utf-8", "replace") if status.startswith(b"E") else "controlling tty setup failed"
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=1)
                except Exception:  # noqa: BLE001
                    pass
                if master_fd is not None:
                    try:
                        os.close(master_fd)
                    except OSError:
                        pass
                    master_fd = None
                raise OSError(detail)
        elif stdin_mode == "pipe":
            process = subprocess.Popen(
                argv if structured else ["/bin/sh", "-lc", command],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **{k: v for k, v in popen_kwargs.items() if k != "pass_fds"},
                pass_fds=tuple(descriptors),
            )
            if process.stdin is not None:
                _set_nonblocking(process.stdin.fileno())
        else:
            process = subprocess.Popen(
                argv if structured else ["/bin/sh", "-lc", command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **{k: v for k, v in popen_kwargs.items() if k != "pass_fds"},
                pass_fds=tuple(descriptors),
            )
    except Exception as exc:  # noqa: BLE001
        # OSError embeds paths; only return its path-free diagnostic.
        detail = exc.strerror if structured and isinstance(exc, OSError) else str(exc)
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
        if slave_fd is not None:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        _fail_queue_items(request_id, state, f"spawn failed: {type(exc).__name__}", remove=True)
        _send({"op": "spawn_error", "id": request_id, "message": f"{type(exc).__name__}: {detail}"})
        return
    finally:
        for fd in descriptors:
            os.close(fd)

    wake_r, wake_w = os.pipe()
    _set_nonblocking(wake_r)
    _set_nonblocking(wake_w)
    with _running_lock:
        _running[request_id] = process
        _stdin_modes[request_id] = stdin_mode
        if master_fd is not None:
            _pty_masters[request_id] = master_fd
            _set_nonblocking(master_fd)
        state.attach_wake(wake_r, wake_w)
        killed_now = state.killed
        if killed_now:
            _kill_group(process.pid)
    if killed_now:
        _fail_queue_items(request_id, state, "process killed before start")

    selector = selectors.DefaultSelector()
    open_read_fds: set[object] = set()
    selector.register(wake_r, selectors.EVENT_READ, "wake")
    if stdin_mode == "pty":
        selector.register(master_fd, selectors.EVENT_READ, "pty")
        open_read_fds.add(master_fd)
    else:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        open_read_fds.add(process.stdout)
        open_read_fds.add(process.stderr)

    stdin_closed = False
    stdin_broken_error: str | None = None
    write_watched = False

    def set_write_watch(want: bool) -> None:
        nonlocal write_watched
        if want == write_watched:
            return
        if stdin_mode == "pty":
            if master_fd is not None and master_fd in open_read_fds:
                events = (selectors.EVENT_READ | selectors.EVENT_WRITE) if want else selectors.EVENT_READ
                try:
                    selector.modify(master_fd, events, "pty")
                except (KeyError, ValueError, OSError):
                    want = False
            else:
                want = False
        elif stdin_mode == "pipe":
            if process.stdin is not None and not stdin_closed:
                try:
                    if want:
                        selector.register(process.stdin.fileno(), selectors.EVENT_WRITE, "stdin_write")
                    else:
                        selector.unregister(process.stdin.fileno())
                except (KeyError, ValueError, OSError):
                    want = False
            else:
                want = False
        else:
            want = False
        write_watched = want

    def drain_input() -> bool:
        """Single stdin writer for this command. Returns True while blocked on EVENT_WRITE."""
        nonlocal stdin_closed, stdin_broken_error
        while True:
            with _running_lock:
                if state.killed:
                    killed = True
                    item = None
                else:
                    killed = False
                    item = state.items.popleft() if state.items else None
            if killed:
                set_write_watch(False)
                _fail_queue_items(request_id, state, "process killed")
                return False
            if item is None:
                return False
            kind, write_id, payload, already_written = item
            if kind == "eof":
                if stdin_mode == "pipe" and process.stdin is not None and not stdin_closed:
                    set_write_watch(False)
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                    stdin_closed = True
                continue
            if kind not in {"write", "interrupt"}:
                continue
            active_unwritten = len(payload)
            if stdin_broken_error is not None:
                with _running_lock:
                    state.queued_bytes = max(0, state.queued_bytes - active_unwritten)
                _write_ack(request_id, write_id, ok=False, error=stdin_broken_error, written=already_written)
                continue
            if stdin_closed:
                with _running_lock:
                    state.queued_bytes = max(0, state.queued_bytes - active_unwritten)
                _write_ack(request_id, write_id, ok=False, error="stdin already closed", written=already_written)
                continue
            if stdin_mode == "pty" and master_fd is not None:
                target_fd = master_fd
            elif kind == "write" and stdin_mode == "pipe" and process.stdin is not None:
                target_fd = process.stdin.fileno()
            else:
                with _running_lock:
                    state.queued_bytes = max(0, state.queued_bytes - active_unwritten)
                error = "interrupt requires pty" if kind == "interrupt" else "stdin not available"
                _write_ack(request_id, write_id, ok=False, error=error, written=already_written)
                continue
            view = memoryview(payload)
            blocked = False
            try:
                while view:
                    try:
                        written = os.write(target_fd, view)
                    except BlockingIOError:
                        remainder = view.tobytes()
                        with _running_lock:
                            if state.killed:
                                killed_during_write = True
                                state.queued_bytes = max(0, state.queued_bytes - active_unwritten)
                            else:
                                killed_during_write = False
                                # A blocked normal write must not overtake pending Ctrl+C.
                                state.requeue_partial((kind, write_id, remainder, already_written))
                        if killed_during_write:
                            _write_ack(
                                request_id, write_id, ok=False, error="process killed", written=already_written,
                            )
                            set_write_watch(False)
                            _fail_queue_items(request_id, state, "process killed")
                            return False
                        blocked = True
                        break
                    if written <= 0:
                        raise OSError("short write")
                    already_written += written
                    active_unwritten -= written
                    with _running_lock:
                        state.queued_bytes = max(0, state.queued_bytes - written)
                    view = view[written:]
            except OSError as exc:
                with _running_lock:
                    state.queued_bytes = max(0, state.queued_bytes - active_unwritten)
                err = str(exc.strerror or exc)
                stdin_broken_error = err
                _write_ack(request_id, write_id, ok=False, error=err, written=already_written)
                continue
            if blocked:
                return True
            _write_ack(request_id, write_id, ok=True, written=already_written)

    # 后台子进程可能持续写管道；shell 退出后只排空一次固定窗口。
    drain_deadline = None
    try:
        while open_read_fds or process.poll() is None:
            exited = process.poll() is not None
            if state.killed:
                set_write_watch(False)
                _fail_queue_items(request_id, state, "process killed")
            elif exited:
                set_write_watch(False)
                _fail_queue_items(request_id, state, "process exited")
            else:
                set_write_watch(drain_input())
            if exited and drain_deadline is None:
                drain_deadline = time.monotonic() + POST_EXIT_DRAIN_SECONDS
            if drain_deadline is not None and time.monotonic() >= drain_deadline:
                break
            wait = 0.1 if drain_deadline is None else max(0.0, min(0.05, drain_deadline - time.monotonic()))
            events = selector.select(timeout=wait)
            for key, mask in events:
                if key.data == "wake":
                    state.drain_wake()
                    continue
                if key.data == "stdin_write":
                    continue
                if mask & selectors.EVENT_READ:
                    try:
                        if key.data == "pty":
                            chunk = os.read(key.fileobj, CHUNK_BYTES)
                        else:
                            chunk = key.fileobj.read1(CHUNK_BYTES)
                    except BlockingIOError:
                        continue
                    except Exception:  # noqa: BLE001
                        chunk = b""
                    if chunk:
                        message = {"op": "out", "id": request_id, "data": base64.b64encode(chunk).decode("ascii")}
                        if structured:
                            message["stream"] = key.data
                        _send(message)
                    else:
                        if key.data == "pty":
                            write_watched = False
                        try:
                            selector.unregister(key.fileobj)
                        except (KeyError, ValueError):
                            pass
                        open_read_fds.discard(key.fileobj)
        code = process.wait()
    finally:
        set_write_watch(False)
        selector.close()
        final_error = "process killed" if state.killed else "process exited"
        _fail_queue_items(request_id, state, final_error, remove=True)
        with _running_lock:
            state.close_wake()
        if stdin_mode == "pty":
            with _running_lock:
                master = _pty_masters.pop(request_id, None)
            if master is not None:
                try:
                    os.close(master)
                except OSError:
                    pass
        else:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            if process.stdin is not None and not stdin_closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass
        with _running_lock:
            _running.pop(request_id, None)
            _stdin_modes.pop(request_id, None)
            _pty_masters.pop(request_id, None)
    _send({"op": "exit", "id": request_id, "code": 128 - code if code < 0 else code})


def _write(request_id, write_id, data_b64):
    """Control thread only: validate and enqueue into the command's byte-bounded FIFO."""
    wid = "" if write_id is None else str(write_id)
    try:
        payload = base64.b64decode(data_b64)
    except Exception:  # noqa: BLE001
        _write_ack(request_id, wid, ok=False, error="invalid base64")
        return
    reject_error = None
    with _running_lock:
        state = _inputs.get(request_id)
        if state is None:
            reject_error = "process not running"
        elif state.closed_for_new:
            reject_error = state.close_error
        elif state.stdin_mode == "none":
            reject_error = "stdin not available"
        elif state.eof_enqueued:
            reject_error = "stdin already closed"
        elif state.queued_bytes + len(payload) > MAX_INPUT_BYTES:
            reject_error = "stdin input buffer full"
        else:
            state.items.append(("write", wid, payload, 0))
            state.queued_bytes += len(payload)
            state.wake()
    if reject_error is not None:
        _write_ack(request_id, wid, ok=False, error=reject_error)


def _close_stdin(request_id):
    """Control thread only: enqueue EOF strictly behind all prior writes."""
    with _running_lock:
        state = _inputs.get(request_id)
        if state is None or state.closed_for_new or state.stdin_mode != "pipe" or state.eof_enqueued:
            return
        state.eof_enqueued = True
        state.items.append(("eof", None, b"", 0))
        state.wake()


def _resize(request_id, rows, cols):
    with _running_lock:
        master = _pty_masters.get(request_id)
        mode = _stdin_modes.get(request_id, "none")
    if mode != "pty" or master is None:
        return
    try:
        import struct
        import termios
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", int(rows), int(cols), 0, 0))
    except Exception:  # noqa: BLE001
        pass


def _interrupt(request_id, write_id):
    """Enqueue PTY VINTR ahead of normal writes for the single stdin writer.

    Does not call os.write on the control thread (preserves single-writer). ACK
    reports whether ``\\x03`` was actually delivered; EAGAIN is retried via the
    existing EVENT_WRITE path rather than dropped. Does not SIGKILL.
    """
    wid = "" if write_id is None else str(write_id)
    reject_error = None
    with _running_lock:
        state = _inputs.get(request_id)
        if state is None:
            reject_error = "process not running"
        elif state.closed_for_new:
            reject_error = state.close_error
        elif state.stdin_mode != "pty":
            reject_error = "interrupt requires pty"
        else:
            # Always admit the 1-byte interrupt even when the normal buffer is full.
            state.items.appendleft(("interrupt", wid, b"\x03", 0))
            state.queued_bytes += 1
            state.wake()
    if reject_error is not None:
        _write_ack(request_id, wid, ok=False, error=reject_error)


def _kill(request_id):
    with _running_lock:
        state = _inputs.get(request_id)
        process = _running.get(request_id)
        if state is not None:
            state.killed = True
            state.closed_for_new = True
            state.close_error = "process killed"
            state.wake()
        if process is None:
            if state is None:
                _killed_before_start.add(request_id)
            return
        _kill_group(process.pid)


def _check_idle(request_id):
    """Verify no active commands and no surviving child/orphan PIDs exist in /proc."""
    with _running_lock:
        if _running or _inputs:
            _send({"op": "idle_status", "id": request_id, "idle": False, "other_pids": max(len(_running), len(_inputs))})
            return
        # No managed Popen exists; safely reap any direct dead zombies.
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
                if pid <= 0:
                    break
            except (ChildProcessError, OSError):
                break
        baseline = _baseline_pids or frozenset({os.getpid()})
        try:
            entries = os.listdir("/proc")
        except OSError:
            # Fail closed: cannot verify PID namespace is clean.
            _send({"op": "idle_status", "id": request_id, "idle": False, "other_pids": -1})
            return
        other = 0
        for name in entries:
            if not name.isdigit():
                continue
            if int(name) not in baseline:
                other += 1
        _send({"op": "idle_status", "id": request_id, "idle": other == 0, "other_pids": other})


def _shutdown():
    with _running_lock:
        for process in _running.values():
            _kill_group(process.pid)
    os._exit(0)


def _harden():
    # 同 UID 的沙箱进程不得通过 /proc/<pid>/fd 打开 Worker 的控制管道。
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE
    except Exception:  # noqa: BLE001
        pass


def main():
    global _control_out, _baseline_pids
    # Inside bwrap --unshare-pid, PID 1 is bwrap's init reaper and Worker is PID 2.
    # Capture only Worker (and bwrap init PID 1 when parent) before any command starts.
    my_pid = os.getpid()
    _baseline_pids = frozenset({my_pid, 1} if os.getppid() == 1 else {my_pid})
    control_in = os.dup(0)
    _control_out = os.dup(1)
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 0)
    os.dup2(null, 1)
    os.close(null)
    _harden()
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    reader = os.fdopen(control_in, "rb", buffering=0)
    buffer = b""
    _send({"op": "ready"})
    while True:
        try:
            data = reader.read(65536)
        except OSError:
            data = b""
        if not data:
            _shutdown()
        buffer += data
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            try:
                message = json.loads(line)
                op = message["op"]
                request_id = int(message["id"])
            except Exception:  # noqa: BLE001
                sys.stderr.write("sandbox worker: malformed control message\n")
                _shutdown()
            if op == "run":
                stdin_mode = message.get("stdin_mode", "none")
                _register_input_queue(request_id, stdin_mode)
                threading.Thread(
                    target=_run,
                    args=(request_id, str(message["command"])),
                    kwargs={
                        "owner": message.get("owner"),
                        "stdin_mode": stdin_mode,
                        "env": message.get("env"),
                    },
                    daemon=True,
                ).start()
            elif op == "run_argv":
                _register_input_queue(request_id, "none")
                threading.Thread(
                    target=_run, kwargs={"request_id": request_id, "argv": message["argv"],
                                         "cwd": message["cwd"], "mount_identity": message.get("mount_identity"),
                                         "file": message.get("file")}, daemon=True,
                ).start()
            elif op == "write":
                _write(request_id, message.get("write_id"), message.get("data", ""))
            elif op == "close_stdin":
                _close_stdin(request_id)
            elif op == "resize":
                _resize(request_id, message.get("rows", 24), message.get("cols", 80))
            elif op == "interrupt":
                _interrupt(request_id, message.get("write_id"))
            elif op == "kill":
                _kill(request_id)
            elif op == "check_idle":
                _check_idle(request_id)


if __name__ == "__main__":
    main()
