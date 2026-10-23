"""Bounded regex search for the local workspace and skills mounts.

Bubblewrap with rg: one ripgrep process in the existing permission-mode SandboxPool.
rg walks the tree itself (ignore files, hidden files, binary and size filtering).
Without rg, or for a non-Bubblewrap executor: one Python worker that opens every
component relative to a directory descriptor with O_NOFOLLOW.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import errno
import json
import os
from pathlib import Path, PurePosixPath
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import sysconfig
import time
from typing import Callable, Literal

from langchain_core.tools import StructuredTool

from agent.cancel import get_cancel_context
from agent.sandbox import BubblewrapBackend, WorkspaceCompositeBackend, SEARCH_BINARY_PATH, KILL_CONFIRM_SECONDS
from agent.sandbox_pool import PersistentSandbox, SandboxLostError, SandboxStartError
from agent.tools import _grep_worker as rules_engine

SEARCH_TIMEOUT = 10.0
DEFAULT_MAX_COUNT = 1000
MAX_OUTPUT_BYTES = 64 * 1024
NOTE_RESERVE = 1024
MAX_STDERR_BYTES = 8 * 1024
TRUNCATED = "Results truncated: maximum match count reached."
FILES_TRUNCATED = "Results truncated: maximum file count reached."
BYTES_TRUNCATED = "Results truncated: maximum output size reached."
TIMED_OUT = "Error: Search timed out; search processes terminated."
PARTIAL_TIMEOUT = "Results incomplete: search timed out; search processes terminated."
RG_PARTIAL = "Results incomplete: ripgrep could not search some files."
WORKER_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONUTF8": "1"}
OutputMode = Literal["files_with_matches", "content", "count"]


def resolve_rg() -> str | None:
    """Prefer rg installed alongside this Python interpreter, then system PATH."""
    candidate = Path(sysconfig.get_path("scripts")) / "rg"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which("rg")


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


class _SearchProcessHandle:
    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process

    def cancel_execution(self) -> None:
        # The supervisor loop notices cancellation and reaps the process group.
        _signal_group(self.process, signal.SIGKILL)


def _stop(process: subprocess.Popen) -> None:
    # Signal the group before reaping so its id cannot have been reused.
    # The host-side Python fallback is the only caller of this process helper.
    if process.returncode is None:
        _signal_group(process, signal.SIGKILL)
    process.wait()


@dataclass
class _Results:
    """Mode-aware accumulation: max_count counts lines (content) or files (others)."""

    mode: str
    limit: int
    budget: int = MAX_OUTPUT_BYTES - NOTE_RESERVE
    lines: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0
    size: int = 0
    notes: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    @property
    def empty(self) -> bool:
        return not self.lines and not self.counts

    def add(self, path: str, line: int = 0, text: str = "", count: int = 0) -> bool:
        """Record one result; return False once a limit stops the search."""
        if self.mode == "content":
            if self.total >= self.limit:
                self.note(TRUNCATED)
                return False
            cost = len(text.encode("utf-8", "replace")) + len(str(line)) + 5
            if path not in self.lines:
                cost += len(path.encode("utf-8", "replace")) + 2
        else:
            if path in self.counts:
                return True
            if len(self.counts) >= self.limit:
                self.note(FILES_TRUNCATED)
                return False
            cost = len(path.encode("utf-8", "replace")) + 1 + (len(str(count)) + 2 if self.mode == "count" else 0)
        if self.size + cost > self.budget:
            self.note(BYTES_TRUNCATED)
            return False
        self.size += cost
        if self.mode == "content":
            self.lines.setdefault(path, []).append((line, text))
            self.total += 1
        else:
            self.counts[path] = count
        return True

    def render(self) -> str:
        if self.mode == "content":
            out = []
            for path in sorted(self.lines):
                out.append(f"{path}:")
                out.extend(f"  {number}: {text}" for number, text in sorted(self.lines[path]))
        elif self.mode == "count":
            out = [f"{path}: {self.counts[path]}" for path in sorted(self.counts)]
        else:
            out = sorted(self.counts)
        body = "\n".join(out) if out else "No matches found"
        return "\n".join([body, *self.notes])


@dataclass
class _Run:
    status: str  # "exited" | "stopped" | "timeout" | "cancelled"
    exit_code: int | None
    stderr: str


def _stream(argv: list[str], consume: Callable[[bytes], bool], *, deadline: float,
            pass_fds: tuple[int, ...] = (), stdin: bytes | None = None,
            env: dict[str, str] | None = None) -> _Run:
    """Run one search process, feed stdout chunks to ``consume`` and always reap it."""
    context = get_cancel_context()
    process = subprocess.Popen(  # noqa: S603 - argv list, never a shell
        argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=pass_fds,
        start_new_session=True, env=env,
    )
    stderr = bytearray()
    status = "exited"
    try:
        if context:
            context.register_process(_SearchProcessHandle(process))
        if stdin is not None:
            assert process.stdin is not None
            try:
                process.stdin.write(stdin)
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()
        stdout = process.stdout
        stderr_pipe = process.stderr
        assert stdout is not None and stderr_pipe is not None
        with selectors.DefaultSelector() as selector:
            selector.register(stdout, selectors.EVENT_READ, "out")
            selector.register(stderr_pipe, selectors.EVENT_READ, "err")
            while selector.get_map():
                if context and context.cancelled:
                    status = "cancelled"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    status = "timeout"
                    break
                for key, _ in selector.select(min(remaining, 0.05)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.data == "err":
                        stderr += chunk[:max(0, MAX_STDERR_BYTES - len(stderr))]
                    elif not consume(chunk):
                        status = "stopped"
                        break
                if status == "stopped":
                    break
        if status == "exited":
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                status = "timeout"
    finally:
        _stop(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()
        if context:
            context.clear_process()
    if context and context.cancelled:
        status = "cancelled"
    return _Run(status, process.returncode, stderr.decode("utf-8", "replace").strip())


def _stream_rg(executor: BubblewrapBackend, argv: list[str], target: _Target,
               consume: Callable[[bytes], bool], *, deadline: float, network: bool) -> _Run:
    """Use the pool's command handle and exit events, leaving its worker alive."""
    context = get_cancel_context()
    pool = executor.pool

    def run(sandbox: PersistentSandbox) -> _Run:
        if context and context.cancelled:
            return _Run("cancelled", None, "")
        if time.monotonic() >= deadline:
            return _Run("timeout", None, "")
        if target.exact_file:
            if target.file_identity is None:
                raise ValueError("search mount changed; retry after restoring the workspace")
            dev, ino = target.file_identity
            virtual_file = str(PurePosixPath(target.virtual_root) / target.relative)
            mount_identity = (virtual_file, dev, ino)
        else:
            identity = os.fstat(target.mount_fd)
            mount_identity = (target.virtual_root, identity.st_dev, identity.st_ino)
        try:
            handle = sandbox.start_argv(
                argv, target.virtual_dir,
                mount_identity=mount_identity,
                file=PurePosixPath(target.relative).name if target.is_file else None,
            )
        except SandboxLostError:
            pool.discard(sandbox, "search worker lost before starting rg")
            raise
        if context:
            context.register_process(handle)
        stderr = bytearray()
        status = "exited"
        exit_code = None
        terminal = False
        killed_at = None
        try:
            while True:
                now = time.monotonic()
                if status == "exited":
                    if context and context.cancelled:
                        status = "cancelled"
                    elif now >= deadline:
                        status = "timeout"
                if status != "exited" and killed_at is None:
                    handle.kill()
                    killed_at = now
                if killed_at is not None and now - killed_at >= KILL_CONFIRM_SECONDS:
                    pool.discard(sandbox, "search worker did not confirm rg termination")
                    break
                event = handle.next_event(0.05)
                if event is None:
                    continue
                kind, data = event
                if kind == "out":
                    if status == "exited" and not consume(data):
                        status = "stopped"
                elif kind == "err":
                    stderr += data[:max(0, MAX_STDERR_BYTES - len(stderr))]
                elif kind == "exit":
                    terminal = True
                    exit_code = data
                    break
                elif kind == "spawn_error":
                    terminal = True
                    raise ValueError("ripgrep could not start: " + str(data))
                else:
                    terminal = True
                    pool.discard(sandbox, str(data))
                    raise ValueError("search sandbox was lost; the search was not retried")
        finally:
            if not terminal and sandbox.alive:
                handle.kill()
                until = time.monotonic() + KILL_CONFIRM_SECONDS
                while time.monotonic() < until:
                    event = handle.next_event(0.05)
                    if event and event[0] in ("exit", "spawn_error", "lost"):
                        break
                else:
                    pool.discard(sandbox, "search cleanup was not confirmed")
            handle.release()
            if context:
                context.clear_process()
        if context and context.cancelled:
            status = "cancelled"
        return _Run(status, exit_code, stderr.decode("utf-8", "replace").strip())

    try:
        return pool.run(network, run, deadline=deadline)
    except (TimeoutError, SandboxStartError):
        if time.monotonic() >= deadline:
            return _Run("timeout", None, "")
        raise


class _Splitter:
    """Split a byte stream on a separator with a bounded record size."""

    def __init__(self, separator: bytes, limit: int) -> None:
        self.separator = separator
        self.limit = limit
        self.buffer = bytearray()

    def feed(self, chunk: bytes):
        self.buffer += chunk
        start = 0
        while (end := self.buffer.find(self.separator, start)) != -1:
            yield bytes(self.buffer[start:end])
            start = end + 1
        del self.buffer[:start]
        if len(self.buffer) > self.limit:
            raise ValueError("search output record exceeded the size limit")


class _CountSplitter:
    """Split ``path\\0count\\n`` records. The path may itself contain newlines."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.buffer = bytearray()

    def feed(self, chunk: bytes):
        self.buffer += chunk
        while (nul := self.buffer.find(b"\0")) != -1 and (nl := self.buffer.find(b"\n", nul + 1)) != -1:
            path = bytes(self.buffer[:nul])
            count = bytes(self.buffer[nul + 1:nl])
            del self.buffer[:nl + 1]
            yield path, count
        if len(self.buffer) > self.limit:
            raise ValueError("search output record exceeded the size limit")


@dataclass
class _Target:
    root: Path
    relative: str
    virtual_root: str
    file_limit: int
    mount_fd: int
    is_file: bool
    exact_file: bool = False
    file_identity: tuple[int, int] | None = None

    @property
    def virtual_dir(self) -> str:
        directory = PurePosixPath(self.virtual_root) / self.relative
        return str(directory.parent if self.is_file else directory)


def _open_target(backend: WorkspaceCompositeBackend, path: str | None) -> tuple[_Target, int]:
    root, relative, virtual_root, file_limit = backend.regex_search_location(path)
    mount_fd = -1
    try:
        mount_fd = rules_engine.open_mount(str(root))
        target_fd = rules_engine.open_below(mount_fd, tuple(p for p in relative.split("/") if p))
    except OSError as exc:
        if mount_fd != -1:
            os.close(mount_fd)
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError("Permission denied: search path must not traverse symlinks") from None
        raise
    mode = os.fstat(target_fd).st_mode
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        os.close(target_fd)
        os.close(mount_fd)
        raise ValueError("search path must be a regular file or directory")
    virtual_file = str(PurePosixPath(virtual_root) / relative) if relative else virtual_root
    exact = virtual_file in getattr(backend, "exact_file_mounts", frozenset())
    file_identity = None
    if exact:
        info = os.fstat(target_fd)
        file_identity = (info.st_dev, info.st_ino)
    return _Target(
        root, relative, virtual_root, file_limit, mount_fd, stat.S_ISREG(mode), exact, file_identity,
    ), target_fd


def _explicit_file_excluded(target: _Target, target_fd: int, rules: list, results: _Results) -> bool:
    """Apply globs, size and binary checks that rg skips for explicit file paths."""
    name = PurePosixPath(target.relative).name
    if rules_engine.override_match(rules, name, False) == rules_engine.IGNORE:
        return True
    if os.fstat(target_fd).st_size > target.file_limit:
        results.note(rules_engine.LARGE_FILE_NOTE)
        return True
    data = os.read(target_fd, target.file_limit + 1)
    if len(data) > target.file_limit:
        results.note(rules_engine.LARGE_FILE_NOTE)
        return True
    return b"\x00" in data


def _rg_arguments(mode: str, fixed_string: bool, globs: list[str], file_limit: int) -> list[str]:
    args = ["--no-config", "--no-ignore-global", "--max-filesize", str(file_limit),
            "--color", "never", "--line-buffered"]
    if mode == "content":
        args.append("--json")
    elif mode == "files_with_matches":
        # NUL-delimited: a newline is legal in a Linux filename and must stay one path.
        # rg block-buffers this stream, so a timeout may not yet include finished paths.
        args += ["--files-with-matches", "--null"]
    else:
        args += ["--count", "--with-filename", "--null"]
    if fixed_string:
        args.append("--fixed-strings")
    for glob in globs:
        args += ["--glob", glob]
    # Appended last so it wins over any include rule, as in the Python engine.
    return args + ["--glob", "!" + rules_engine.ALWAYS_SKIPPED]


def _rg_text(value: dict) -> str:
    if "text" in value:
        return value["text"]
    return base64.b64decode(value["bytes"]).decode("utf-8", errors="replace")


def _search_rg(executor: BubblewrapBackend, target: _Target, pattern: str,
               globs: list[str], mode: str, fixed_string: bool, results: _Results,
               deadline: float, network: bool) -> str:
    cwd = target.virtual_dir
    subject = PurePosixPath(target.relative).name if target.is_file else "."
    argv = [SEARCH_BINARY_PATH, *_rg_arguments(mode, fixed_string, globs, target.file_limit),
            "--", pattern, subject]

    def virtual(raw: str) -> str:
        if target.is_file:
            return str(PurePosixPath(target.virtual_root) / target.relative)
        relative = raw[2:] if raw.startswith("./") else raw
        return str(PurePosixPath(cwd) / relative)

    record_limit = target.file_limit * 8 + 65536
    if mode == "content":
        splitter = _Splitter(b"\n", record_limit)

        def consume(chunk: bytes) -> bool:
            for raw in splitter.feed(chunk):
                record = json.loads(raw)
                if record.get("type") != "match":
                    continue
                data = record["data"]
                line = _rg_text(data["lines"]).rstrip("\n").rstrip("\r")
                if len(line.encode("utf-8")) > rules_engine.LINE_BYTES:
                    results.note(rules_engine.LINE_NOTE)
                    line = line.encode("utf-8")[:rules_engine.LINE_BYTES].decode("utf-8", "ignore") + " [line truncated]"
                if not results.add(virtual(_rg_text(data["path"])), data["line_number"], line):
                    return False
            return True
    elif mode == "files_with_matches":
        splitter = _Splitter(b"\0", record_limit)

        def consume(chunk: bytes) -> bool:
            for raw in splitter.feed(chunk):
                if raw and not results.add(virtual(raw.decode("utf-8", "replace"))):
                    return False
            return True
    else:
        splitter = _CountSplitter(record_limit)

        def consume(chunk: bytes) -> bool:
            for name, count in splitter.feed(chunk):
                if name and not results.add(virtual(name.decode("utf-8", "replace")), count=int(count)):
                    return False
            return True

    run = _stream_rg(executor, argv, target, consume, deadline=deadline, network=network)
    stderr = run.stderr.replace(str(target.root), target.virtual_root)[:2048]
    if run.status in ("cancelled", "timeout", "stopped"):
        return _finish(run.status, results)
    if run.exit_code == 2 and not results.empty:
        results.note(RG_PARTIAL)
        return results.render()
    if run.exit_code == 2:
        return "Error: ripgrep execution failed: " + (stderr or "exit status 2")
    if run.exit_code == 1 and stderr:
        return "Error: ripgrep could not run in the persistent search sandbox: " + stderr
    if run.exit_code not in (0, 1) or (run.exit_code == 0 and results.empty):
        return (f"Error: ripgrep could not run in the persistent search sandbox (exit status {run.exit_code})"
                + (": " + stderr if stderr else ""))
    return results.render()


def _search_python(target: _Target, pattern: str, globs: list[str], mode: str,
                   fixed_string: bool, results: _Results, deadline: float) -> str:
    payload = dict(root=str(target.root), relative=target.relative, virtual_root=target.virtual_root,
                   pattern=pattern, globs=globs, fixed_string=fixed_string, output_mode=mode,
                   file_limit=target.file_limit)
    splitter = _Splitter(b"\n", target.file_limit * 8 + 65536)
    errors: list[str] = []

    def consume(chunk: bytes) -> bool:
        for raw in splitter.feed(chunk):
            record = json.loads(raw)
            if "error" in record:
                errors.append(record["error"])
                return False
            if "warning" in record:
                results.note(record["warning"])
            elif not results.add(record["path"], record.get("line", 0), record.get("text", ""),
                                 record.get("count", 0)):
                return False
        return True

    run = _stream([sys.executable, str(Path(__file__).with_name("_grep_worker.py"))], consume,
                  deadline=deadline, stdin=json.dumps(payload).encode(), env=WORKER_ENV)
    if errors:
        return "Error: " + errors[0]
    if run.status in ("cancelled", "timeout", "stopped"):
        return _finish(run.status, results)
    if run.exit_code != 0 or splitter.buffer:
        return "Error: Search worker failed."
    return results.render()


def _finish(status: str, results: _Results) -> str:
    if status == "cancelled":
        return "Error: Search cancelled."
    if status == "timeout":
        if results.empty:
            return TIMED_OUT
        results.note(PARTIAL_TIMEOUT)
    return results.render()


def search(backend: WorkspaceCompositeBackend, pattern: str, path: str | None,
           glob: str | list[str] | None, output_mode: str, max_count: int | None,
           fixed_string: bool, *, network: bool = False) -> str:
    """Run one bounded, killable search; never use execute or a shell."""
    context = get_cancel_context()
    deadline = time.monotonic() + SEARCH_TIMEOUT
    target_fd = -1
    target = None
    try:
        if len(pattern.encode("utf-8")) > 16 * 1024 or "\x00" in pattern or "\n" in pattern:
            raise ValueError("pattern must be a single line of at most 16384 bytes without NUL")
        limit = DEFAULT_MAX_COUNT if max_count is None else max_count
        if limit <= 0:
            raise ValueError("max_count must be positive")
        if output_mode not in {"files_with_matches", "content", "count"}:
            raise ValueError("invalid output_mode")
        rules = rules_engine.parse_globs(glob)
        globs = [rule.source for rule in rules]
        target, target_fd = _open_target(backend, path)
        results = _Results(output_mode, limit)
        if target.is_file:
            excluded = _explicit_file_excluded(target, target_fd, rules, results)
            if excluded:
                return results.render()
        os.close(target_fd)
        target_fd = -1
        if context and context.cancelled:
            return "Error: Search cancelled."
        rg = resolve_rg()
        if rg and isinstance(backend.executor, BubblewrapBackend):
            return _search_rg(backend.executor, target, pattern, globs, output_mode,
                              fixed_string, results, deadline, network)
        return _search_python(target, pattern, globs, output_mode, fixed_string, results, deadline)
    except FileNotFoundError:
        return "Error: search path not found"
    except (OSError, ValueError, SandboxStartError, SandboxLostError) as exc:
        # OSError includes host paths; never surface those to the model.
        detail = exc.strerror if isinstance(exc, OSError) else str(exc)
        return f"Error: {detail}"
    finally:
        if target_fd != -1:
            os.close(target_fd)
        if target is not None:
            os.close(target.mount_fd)


def build_regex_grep_tool(backend: WorkspaceCompositeBackend, *, network: bool = False) -> StructuredTool:
    def grep(
        pattern: str,
        path: str | None = None,
        glob: str | list[str] | None = None,
        output_mode: OutputMode = "files_with_matches",
        max_count: int | None = None,
        fixed_string: bool = False,
    ) -> str:
        """Search source files using regular expressions.

        Behavior:
        - Patterns are regular expressions by default.
        - Set fixed_string=true for literal text searches.
        - Uses ripgrep when available.
        - Falls back to Python regex search when ripgrep is unavailable.
        - Searches the project workspace by default. /skills and declared sandbox mounts can be searched by path.
        - Like ripgrep, skips hidden files, .gitignore/.ignore/.rgignore matches,
          symlinks, binary files and files over 10 MiB. .git is always skipped.

        glob: one ripgrep glob or a list of them, relative to the search path.
        - "*.py" searches only Python files (no "/" matches at any depth).
        - "!*.test.py" excludes files; "!**/node_modules/**" excludes a directory.
        - ["*.py", "!**/tests/**"]: Python files outside tests/.
        - Rules apply in order and the last matching rule wins, so
          ["!**/tests/**", "*.py"] includes tests/*.py again.
        - Prefer "!**/.git/**" over "!*/.git/*": a "/" anchors the glob to the
          search path, so "*/.git/*" only matches one directory level deep.
        - Include globs can override ignore rules for matching files.

        Output modes:
        - files_with_matches: Return matching file paths.
        - content: Return matching lines with line numbers.
        - count: Return matching line counts grouped by file.

        max_count (default 1000) limits matching lines in content mode and
        files in files_with_matches/count mode; counts per file are complete.
        Results are bounded by size and time and truncation is reported.
        Prefer grep for locating source code. Use read_file with offset and
        limit to inspect surrounding code.
        """
        return search(backend, pattern, path, glob, output_mode, max_count, fixed_string, network=network)

    return StructuredTool.from_function(grep)
