"""Bubblewrap 沙箱的主进程：常驻命令 Worker。

宿主只读挂载本文件，用 ``python3 -I -S`` 运行；因此只能用标准库，不能 import ``agent``。

协议为按行分隔的 JSON。控制通道是继承来的 stdin/stdout 管道，不会暴露成
文件路径或 socket。

宿主 -> Worker: {"op": "run", "id": N, "command": str} | {"op": "kill", "id": N}
Worker -> 宿主: {"op": "ready"} | {"op": "out", "id": N, "data": base64}
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
import subprocess
import sys
import threading
import time

WORKDIR = "/workspace"
CHUNK_BYTES = 65536
POST_EXIT_DRAIN_SECONDS = 0.1

_send_lock = threading.Lock()
_running_lock = threading.Lock()
_running = {}
_killed_before_start = set()
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


def _run(request_id, command):
    try:
        process = subprocess.Popen(
            ["/bin/sh", "-lc", command],
            cwd=WORKDIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001
        _send({"op": "spawn_error", "id": request_id, "message": f"{type(exc).__name__}: {exc}"})
        return
    with _running_lock:
        _running[request_id] = process
        if request_id in _killed_before_start:
            _killed_before_start.discard(request_id)
            _kill_group(process.pid)

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)
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
                    _send({"op": "out", "id": request_id, "data": base64.b64encode(chunk).decode("ascii")})
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
            elif op == "kill":
                _kill(request_id)


if __name__ == "__main__":
    main()
