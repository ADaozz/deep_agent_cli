"""Bubblewrap 沙箱的主进程：常驻命令 Worker。

宿主只读挂载本文件，用 ``python3 -I -S`` 运行；因此只能用标准库，不能 import ``agent``。

协议为按行分隔的 JSON。控制通道是继承来的 stdin/stdout 管道，不会暴露成
文件路径或 socket。

宿主 -> Worker: {"op": "run", "id": N, "command": str} | {"op": "kill", "id": N}
                | {"op": "run_argv", "id": N, "argv": list[str], "cwd": str, ...}
Worker -> 宿主: {"op": "ready"} | {"op": "out", "id": N, "data": base64, "stream"?: str}
                | {"op": "exit", "id": N, "code": int}
                | {"op": "spawn_error", "id": N, "message": str}

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
_running_lock = threading.Lock()
_running: dict[int, subprocess.Popen[bytes]] = {}
_killed_before_start: set[int] = set()
_control_out = -1


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


def _run(request_id, command=None, argv=None, cwd=WORKDIR, mount_identity=None, file=None):
    descriptors = []
    structured = argv is not None
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
        process = subprocess.Popen(
            argv if structured else ["/bin/sh", "-lc", command],
            shell=False,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
            pass_fds=tuple(descriptors),
        )
    except Exception as exc:  # noqa: BLE001
        # OSError embeds paths; only return its path-free diagnostic.
        detail = exc.strerror if structured and isinstance(exc, OSError) else str(exc)
        _send({"op": "spawn_error", "id": request_id, "message": f"{type(exc).__name__}: {detail}"})
        return
    finally:
        for fd in descriptors:
            os.close(fd)
    with _running_lock:
        _running[request_id] = process
        if request_id in _killed_before_start:
            _killed_before_start.discard(request_id)
            _kill_group(process.pid)

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    # 后台子进程可能持续写管道；shell 退出后只排空一次固定窗口。
    drain_deadline = None
    try:
        while selector.get_map():
            exited = process.poll() is not None
            if exited and drain_deadline is None:
                drain_deadline = time.monotonic() + POST_EXIT_DRAIN_SECONDS
            if drain_deadline is not None and time.monotonic() >= drain_deadline:
                break
            wait = 0.1 if drain_deadline is None else max(0.0, min(0.05, drain_deadline - time.monotonic()))
            events = selector.select(timeout=wait)
            for key, _mask in events:
                try:
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
        process.stdout.close()
        process.stderr.close()
        with _running_lock:
            _running.pop(request_id, None)
    _send({"op": "exit", "id": request_id, "code": 128 - code if code < 0 else code})


def _kill(request_id):
    with _running_lock:
        process = _running.get(request_id)
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
                    target=_run, args=(request_id, str(message["command"])), daemon=True,
                ).start()
            elif op == "run_argv":
                threading.Thread(
                    target=_run, kwargs={"request_id": request_id, "argv": message["argv"],
                                         "cwd": message["cwd"], "mount_identity": message.get("mount_identity"),
                                         "file": message.get("file")}, daemon=True,
                ).start()
            elif op == "kill":
                _kill(request_id)


if __name__ == "__main__":
    main()
