"""Private subprocess entrypoint. Search only files opened below authorized roots.

All descendants share the supervisor's process group, including ripgrep.
Descriptor-relative O_NOFOLLOW opens prevent symlink swaps from escaping a mount.
"""
from __future__ import annotations

import base64
import json
import io
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys

from wcmatch import glob as wcglob

BATCH_SIZE = 64
LINE_BYTES = 2048


def emit(record: dict) -> None:
    print(json.dumps(record, ensure_ascii=True), flush=True)


def open_below(root_fd: int, parts: tuple[str, ...]) -> int:
    fd = os.dup(root_fd)
    try:
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def files(fd: int, prefix: str = ""):
    if stat.S_ISREG(os.fstat(fd).st_mode):
        yield prefix, os.dup(fd)
        return
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        return
    with os.scandir(fd) as entries:
        for entry in entries:
            # Never follow symlinks, including links inside the authorized mount.
            if entry.is_symlink():
                continue
            child = open_below(fd, (entry.name,))
            relative = f"{prefix}/{entry.name}".lstrip("/")
            try:
                yield from files(child, relative)
            finally:
                os.close(child)


def clipped_line(line: str) -> str:
    raw = line.rstrip("\r\n").encode("utf-8")
    if len(raw) > LINE_BYTES:
        emit({"warning": "Results truncated: matching line content shortened to 2048 bytes."})
        return raw[:LINE_BYTES].decode("utf-8", errors="ignore") + " [line truncated]"
    return raw.decode("utf-8")


def text_field(field: dict) -> str:
    if "text" in field:
        return field["text"]
    return base64.b64decode(field["bytes"]).decode("utf-8", errors="replace")


def rg_search(batch: list[tuple[str, int]], payload: dict) -> None:
    paths = {f"/proc/self/fd/{fd}": virtual for virtual, fd in batch}
    argv = [payload["rg"], "--no-config", "--json", "--text"]
    if payload["fixed_string"]:
        argv.append("-F")
    argv.extend(["--", payload["pattern"], *paths])
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   pass_fds=tuple(fd for _, fd in batch),
                                   restore_signals=True)
    except BaseException:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        raise
    try:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        assert process.stdout is not None
        while raw := process.stdout.readline(payload["file_limit"] * 8 + 65536):
            if not raw.endswith(b"\n"):
                raise ValueError("ripgrep output record exceeded the size limit")
            try:
                record = json.loads(raw)
            except ValueError:
                raise ValueError("ripgrep execution or regex error: " + raw[:2048].decode(errors="replace")) from None
            if record.get("type") == "match":
                data = record["data"]
                emit({"path": paths[text_field(data["path"])],
                      "line": data["line_number"], "text": clipped_line(text_field(data["lines"]))})
        if process.wait() not in (0, 1):
            raise ValueError("ripgrep execution failed (exit status " + str(process.returncode) + ")")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        if process.stdout:
            process.stdout.close()


def run(payload: dict) -> None:
    regex = None if payload["rg"] or payload["fixed_string"] else re.compile(payload["pattern"])
    if payload["rg"]:
        empty_fd = os.open("/dev/null", os.O_RDONLY)
        try:
            rg_search([("", empty_fd)], payload)
        finally:
            os.close(empty_fd)
    # The supervisor validates with Deep Agents' public glob compiler. Use its
    # same public wcmatch flags here without importing the entire agent runtime.
    include = payload["glob"]
    compiled = wcglob.compile(include.lstrip("/"), flags=wcglob.BRACE | wcglob.GLOBSTAR) if include else None
    matcher = (lambda rel: compiled.match(rel if "/" in include else Path(rel).name)) if compiled else None
    # Open the absolute root without following any ancestor symlink.
    root_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    batch = []
    try:
        mount_fd = open_below(root_fd, Path(payload["root"]).parts[1:])
        try:
            search_fd = open_below(mount_fd, tuple(p for p in payload["relative"].split("/") if p))
            try:
                is_file = stat.S_ISREG(os.fstat(search_fd).st_mode)
                prefix = Path(payload["relative"]).name if is_file else ""
                base = Path(payload["virtual_root"]) / payload["relative"]
                if is_file:
                    base = base.parent
                for relative, fd in files(search_fd, prefix):
                    try:
                        if matcher and not matcher(relative):
                            continue
                        if os.fstat(fd).st_size > payload["file_limit"]:
                            emit({"warning": "Results incomplete: files larger than 10 MiB skipped."})
                            continue
                        # Match binary handling across engines. Only regular files are read.
                        data = os.read(fd, payload["file_limit"] + 1)
                        if len(data) > payload["file_limit"]:
                            emit({"warning": "Results incomplete: files larger than 10 MiB skipped."})
                            continue
                        if b"\x00" in data:
                            continue
                        os.lseek(fd, 0, os.SEEK_SET)
                        virtual = (base / relative).as_posix()
                        if payload["rg"]:
                            batch.append((virtual, fd))
                            fd = -1
                            if len(batch) == BATCH_SIZE:
                                rg_search(batch, payload)
                                for _, handle in batch:
                                    os.close(handle)
                                batch.clear()
                        else:
                            for number, line in enumerate(io.StringIO(data.decode("utf-8", errors="replace")), 1):
                                line = line.rstrip("\r\n")
                                if (payload["pattern"] in line if payload["fixed_string"] else regex.search(line)):
                                    emit({"path": virtual, "line": number, "text": clipped_line(line)})
                    finally:
                        if fd != -1:
                            os.close(fd)
                if batch:
                    rg_search(batch, payload)
            finally:
                os.close(search_fd)
        finally:
            os.close(mount_fd)
    finally:
        os.close(root_fd)
        for _, fd in batch:
            os.close(fd)


def terminate(_signum, _frame) -> None:
    raise SystemExit(1)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)
    try:
        run(json.load(sys.stdin))
    except re.error as exc:
        emit({"error": f"Invalid Python regular expression: {exc}"})
    except OSError as exc:
        emit({"error": f"Search access/execution error: {exc.strerror}"})
    except Exception as exc:
        emit({"error": str(exc)[:2048]})
