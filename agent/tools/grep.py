"""Bounded regex search for the local workspace and read-only skills mounts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import time
from typing import Literal

from deepagents.backends.utils import compile_grep_include_glob, format_grep_matches
from langchain_core.tools import StructuredTool

from agent.cancel import get_cancel_context
from agent.sandbox import WorkspaceCompositeBackend

SEARCH_TIMEOUT = 10.0
DEFAULT_MAX_COUNT = 1000
MAX_OUTPUT_BYTES = 64 * 1024
MAX_RECORD_BYTES = 32 * 1024
TRUNCATED = "Results truncated: maximum match count reached."


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


class _SearchProcessHandle:
    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process

    def cancel_execution(self) -> None:
        # Let the worker reap rg; the supervisor enforces a short kill deadline.
        _signal_group(self.process, signal.SIGTERM)


def _stop(process: subprocess.Popen) -> None:
    _signal_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        _signal_group(process, signal.SIGKILL)
        process.wait()
    # Clean up any descendants even after an unexpected worker exit.
    _signal_group(process, signal.SIGKILL)


def search(backend: WorkspaceCompositeBackend, pattern: str, path: str | None,
           glob: str | None, output_mode: str, max_count: int | None,
           fixed_string: bool) -> str:
    """Run one killable search; never use execute or a shell."""
    process = None
    context = get_cancel_context()
    try:
        if len(pattern.encode("utf-8")) > 16 * 1024 or "\x00" in pattern or "\n" in pattern:
            raise ValueError("pattern must be a single line of at most 16384 bytes without NUL")
        limit = DEFAULT_MAX_COUNT if max_count is None else max_count
        if limit <= 0:
            raise ValueError("max_count must be positive")
        if output_mode not in {"files_with_matches", "content", "count"}:
            raise ValueError("invalid output_mode")
        if glob is not None:
            if len(glob.encode("utf-8")) > 16 * 1024:
                raise ValueError("glob exceeds 16384 bytes")
            compile_grep_include_glob(glob)
        root, relative, virtual_root, file_limit = backend.regex_search_location(path)
        if context and context.cancelled:
            return "Error: Search cancelled."
        payload = dict(root=str(root), relative=relative, virtual_root=virtual_root,
                       pattern=pattern, glob=glob, fixed_string=fixed_string,
                       rg=shutil.which("rg"), file_limit=file_limit)
        deadline = time.monotonic() + SEARCH_TIMEOUT
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("_grep_worker.py"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if context:
            context.register_process(_SearchProcessHandle(process))
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(payload).encode())
        process.stdin.close()
        matches = []
        size = 0
        buffer = b""
        note = ""
        done = False
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while not done:
                if context and context.cancelled:
                    return "Error: Search cancelled."
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "Error: Search timed out; search processes terminated."
                if not selector.select(min(remaining, 0.05)):
                    continue
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    if context and context.cancelled:
                        return "Error: Search cancelled."
                    if buffer or process.wait() != 0:
                        return "Error: Search worker failed."
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    record = json.loads(line)
                    if "error" in record:
                        return "Error: " + record["error"]
                    if "warning" in record:
                        note = record["warning"]
                        continue
                    record_size = len(line) + 32
                    if size + record_size > MAX_OUTPUT_BYTES - 1024:
                        note = "Results truncated: maximum output size reached."
                        done = True
                        break
                    size += record_size
                    matches.append(record)
                    if len(matches) >= limit:
                        note = TRUNCATED
                        done = True
                        break
                if len(buffer) > MAX_RECORD_BYTES:
                    return "Error: Search worker record exceeded the size limit."
        result = format_grep_matches(matches, output_mode)
        return result + ("\n" if result and note else "") + note
    except (OSError, ValueError) as exc:
        # OSError includes host paths; never surface those to the model.
        detail = exc.strerror if isinstance(exc, OSError) else str(exc)
        return f"Error: {detail}"
    finally:
        if process is not None:
            # Kill the whole group even if the worker has exited: it may have rg children.
            _stop(process)
            if process.stdout:
                process.stdout.close()
            if process.stdin:
                process.stdin.close()
            if context:
                context.clear_process()


def build_regex_grep_tool(backend: WorkspaceCompositeBackend) -> StructuredTool:
    def grep(
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        output_mode: Literal["files_with_matches", "content", "count"] = "files_with_matches",
        max_count: int | None = None,
        fixed_string: bool = False,
    ) -> str:
        """Search source files using regular expressions.

        Behavior:
        - Patterns are regular expressions by default.
        - Set fixed_string=true for literal text searches.
        - Uses ripgrep when available.
        - Falls back to Python regex search when ripgrep is unavailable.
        - Searches /workspace by default; /skills is available when mounted.

        Output modes:
        - files_with_matches: Return matching file paths.
        - content: Return matching lines with line numbers.
        - count: Return matching line counts grouped by file.

        Use glob to restrict file types. Use max_count to limit large searches
        (default 1000 matching lines). Results are bounded by size and time.
        Prefer grep for locating source code. Use read_file with offset and
        limit to inspect surrounding code.
        """
        return search(backend, pattern, path, glob, output_mode, max_count, fixed_string)

    return StructuredTool.from_function(grep)
