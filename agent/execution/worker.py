"""Bubblewrap 沙箱的主进程：常驻命令 Worker。

宿主只读挂载本文件，用 ``python3 -I -S`` 运行；因此只能用标准库，不能 import ``agent``。

协议为按行分隔的 JSON。控制通道是继承来的 stdin/stdout 管道，不会暴露成
文件路径或 socket。

宿主 -> Worker: {"op": "run", "id": N, "command": str, "stdin_mode"?: "none"|"pipe"|"pty"}
                | {"op": "run_argv", "id": N, "argv": list[str], "cwd": str, ...}
                | {"op": "write", "id": N, "data": base64}
                | {"op": "close_stdin", "id": N}
                | {"op": "resize", "id": N, "rows": int, "cols": int}
                | {"op": "kill", "id": N}
Worker -> 宿主: {"op": "ready"} | {"op": "out", "id": N, "data": base64, "stream"?: str}
                | {"op": "exit", "id": N, "code": int}
                | {"op": "spawn_error", "id": N, "message": str}
                | {"op": "write_status", "id": N, "ok": bool, "error"?: str}

stdin 读到 EOF 表示宿主已退出或要求关闭沙箱：Worker 退出，PID 命名空间会带走
沙箱里剩下的全部进程。
"""
import base64
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

_send_lock = threading.Lock()
_running_lock = threading.RLock()
_running: dict[int, subprocess.Popen[bytes]] = {}
_stdin_modes: dict[int, str] = {}
_pty_masters: dict[int, int] = {}
# FIFO of (payload, acknowledge) waiting for the process to accept stdin.
_pending_writes: dict[int, list[tuple[bytes, bool]]] = {}
# In-flight remainder after a partial non-blocking write: (payload, acknowledge).
_partial_writes: dict[int, tuple[bytes, bool]] = {}
_pending_close: set[int] = set()
_killed_before_start: set[int] = set()
_control_out = -1
_PENDING_WRITE_LIMIT = 64

# Child-side helper: new process, single-threaded — safe for setsid + TIOCSCTTY.
# argv: status_fd, slave_fd, command. Writes one status byte then execs.
_PTY_CHILD = (
    "import fcntl, os, sys, termios\n"
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
    "os.execvp('/bin/sh', ['/bin/sh', '-lc', sys.argv[3]])\n"
)


def _send(message):
    data = (json.dumps(message, separators=(",", ":")) + "\n").encode("ascii")
    with _send_lock:
        view = memoryview(data)
        try:
            while view:
                view = view[os.write(_control_out, view):]
        except OSError:
            os._exit(0)


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


def _run(
    request_id, command=None, argv=None, cwd=WORKDIR, mount_identity=None, file=None,
    owner=None, stdin_mode="none",
):
    descriptors = []
    structured = argv is not None
    stdin_mode = stdin_mode if stdin_mode in {"none", "pipe", "pty"} else "none"
    if structured and stdin_mode != "none":
        _send({"op": "spawn_error", "id": request_id, "message": "ValueError: interactive stdin is unsupported for argv runs"})
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
        popen_kwargs = {
            "env": {**os.environ, "DEEP_AGENT_EXECUTION_OWNER": owner} if owner else None,
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
                env={**os.environ, "DEEP_AGENT_EXECUTION_OWNER": owner} if owner else None,
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
                import fcntl
                flags = fcntl.fcntl(process.stdin.fileno(), fcntl.F_GETFL)
                fcntl.fcntl(process.stdin.fileno(), fcntl.F_SETFL, flags | os.O_NONBLOCK)
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
        _fail_pending_writes(request_id, f"spawn failed: {type(exc).__name__}")
        _send({"op": "spawn_error", "id": request_id, "message": f"{type(exc).__name__}: {detail}"})
        return
    finally:
        for fd in descriptors:
            os.close(fd)
    with _running_lock:
        _running[request_id] = process
        _stdin_modes[request_id] = stdin_mode
        if master_fd is not None:
            _pty_masters[request_id] = master_fd
            import fcntl
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        queued = _pending_writes.pop(request_id, [])
        close_after = request_id in _pending_close
        _pending_close.discard(request_id)
        if request_id in _killed_before_start:
            _killed_before_start.discard(request_id)
            _kill_group(process.pid)
            for _payload, acknowledge in queued:
                if acknowledge:
                    _write_ack(request_id, ok=False, error="process killed before start")
            queued = []
            close_after = False
    for payload, acknowledge in queued:
        _write_payload(request_id, payload, acknowledge=acknowledge)
    if close_after:
        _close_stdin(request_id)

    selector = selectors.DefaultSelector()
    if stdin_mode == "pty":
        selector.register(master_fd, selectors.EVENT_READ, "pty")
    else:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    # 后台子进程可能持续写管道；shell 退出后只排空一次固定窗口。
    drain_deadline = None
    try:
        while selector.get_map() or request_id in _partial_writes or _pending_writes.get(request_id):
            _flush_writes(request_id)
            exited = process.poll() is not None
            if exited and drain_deadline is None:
                drain_deadline = time.monotonic() + POST_EXIT_DRAIN_SECONDS
            if drain_deadline is not None and time.monotonic() >= drain_deadline:
                break
            wait = 0.1 if drain_deadline is None else max(0.0, min(0.05, drain_deadline - time.monotonic()))
            events = selector.select(timeout=wait)
            for key, _mask in events:
                try:
                    if key.data == "pty":
                        chunk = os.read(key.fileobj, CHUNK_BYTES)
                    else:
                        chunk = key.fileobj.read1(CHUNK_BYTES)
                except Exception:  # noqa: BLE001
                    chunk = b""
                if chunk:
                    message = {"op": "out", "id": request_id, "data": base64.b64encode(chunk).decode("ascii")}
                    if structured:
                        message["stream"] = key.data
                    _send(message)
                else:
                    selector.unregister(key.fileobj)
        code = process.wait()
    finally:
        selector.close()
        _fail_pending_writes(request_id, "process exited")
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
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
        with _running_lock:
            _running.pop(request_id, None)
            _stdin_modes.pop(request_id, None)
            _pty_masters.pop(request_id, None)
            _pending_close.discard(request_id)
    _send({"op": "exit", "id": request_id, "code": 128 - code if code < 0 else code})


def _write_ack(request_id, *, ok, error=""):
    _send({"op": "write_status", "id": request_id, "ok": ok, "error": error})


def _fail_pending_writes(request_id, error):
    with _running_lock:
        queued = _pending_writes.pop(request_id, [])
        partial = _partial_writes.pop(request_id, None)
    for _payload, acknowledge in queued:
        if acknowledge:
            _write_ack(request_id, ok=False, error=error)
    if partial is not None and partial[1]:
        _write_ack(request_id, ok=False, error=error)


def _write_payload(request_id, payload, *, acknowledge):
    with _running_lock:
        process = _running.get(request_id)
        mode = _stdin_modes.get(request_id, "none")
        master = _pty_masters.get(request_id)
        if request_id in _partial_writes:
            # Preserve FIFO order: park behind the in-flight remainder.
            queued = _pending_writes.setdefault(request_id, [])
            if len(queued) >= _PENDING_WRITE_LIMIT:
                if acknowledge:
                    _write_ack(request_id, ok=False, error="pending write buffer full")
                return
            queued.append((payload, acknowledge))
            return
    if process is None:
        if acknowledge:
            _write_ack(request_id, ok=False, error="process not running")
        return
    try:
        if mode == "pty" and master is not None:
            target_fd = master
        elif mode == "pipe" and process.stdin is not None:
            target_fd = process.stdin.fileno()
        else:
            if acknowledge:
                _write_ack(request_id, ok=False, error="stdin not available")
            return
        view = memoryview(payload)
        while view:
            try:
                written = os.write(target_fd, view)
            except BlockingIOError:
                with _running_lock:
                    _partial_writes[request_id] = (view.tobytes(), acknowledge)
                return
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
    except OSError as exc:
        if acknowledge:
            _write_ack(request_id, ok=False, error=str(exc.strerror or exc))
        return
    if acknowledge:
        _write_ack(request_id, ok=True)
    _flush_writes(request_id)


def _flush_writes(request_id):
    while True:
        with _running_lock:
            partial = _partial_writes.pop(request_id, None)
            queued = _pending_writes.get(request_id)
        if partial is not None:
            _write_payload(request_id, partial[0], acknowledge=partial[1])
            with _running_lock:
                if request_id in _partial_writes:
                    return
            continue
        if not queued:
            return
        with _running_lock:
            if not _pending_writes.get(request_id):
                return
            payload, acknowledge = _pending_writes[request_id].pop(0)
            if not _pending_writes[request_id]:
                _pending_writes.pop(request_id, None)
        _write_payload(request_id, payload, acknowledge=acknowledge)
        with _running_lock:
            if request_id in _partial_writes:
                return


def _write(request_id, data_b64):
    try:
        payload = base64.b64decode(data_b64)
    except Exception:  # noqa: BLE001
        _write_ack(request_id, ok=False, error="invalid base64")
        return
    with _running_lock:
        process = _running.get(request_id)
        if process is None and request_id not in _killed_before_start:
            # Queue only; ACK after the bytes are actually written once the process starts.
            queued = _pending_writes.setdefault(request_id, [])
            if len(queued) >= _PENDING_WRITE_LIMIT:
                _write_ack(request_id, ok=False, error="pending write buffer full")
                return
            queued.append((payload, True))
            return
        if process is None:
            _write_ack(request_id, ok=False, error="process not running")
            return
    _write_payload(request_id, payload, acknowledge=True)


def _close_stdin(request_id):
    with _running_lock:
        process = _running.get(request_id)
        mode = _stdin_modes.get(request_id, "none")
        if process is None and request_id not in _killed_before_start:
            _pending_close.add(request_id)
            return
        if process is None:
            return
    # PTY: do not close the master as a plain EOF; callers send terminal EOF bytes if needed.
    if mode == "pipe" and process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass


def _resize(request_id, rows, cols):
    with _running_lock:
        master = _pty_masters.get(request_id)
        mode = _stdin_modes.get(request_id, "none")
    if mode != "pty" or master is None:
        return
    try:
        import fcntl
        import struct
        import termios
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", int(rows), int(cols), 0, 0))
    except Exception:  # noqa: BLE001
        pass


def _kill(request_id):
    with _running_lock:
        process = _running.get(request_id)
        _fail_pending_writes(request_id, "process killed")
        _pending_close.discard(request_id)
        if process is None:
            _killed_before_start.add(request_id)
            return
        _kill_group(process.pid)


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
    global _control_out
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
                threading.Thread(
                    target=_run,
                    args=(request_id, str(message["command"])),
                    kwargs={"owner": message.get("owner"), "stdin_mode": message.get("stdin_mode", "none")},
                    daemon=True,
                ).start()
            elif op == "run_argv":
                threading.Thread(
                    target=_run, kwargs={"request_id": request_id, "argv": message["argv"],
                                         "cwd": message["cwd"], "mount_identity": message.get("mount_identity"),
                                         "file": message.get("file")}, daemon=True,
                ).start()
            elif op == "write":
                _write(request_id, message.get("data", ""))
            elif op == "close_stdin":
                _close_stdin(request_id)
            elif op == "resize":
                _resize(request_id, message.get("rows", 24), message.get("cols", 80))
            elif op == "kill":
                _kill(request_id)


if __name__ == "__main__":
    main()
