"""Shared grep glob rules and the descriptor-confined Python search engine.

``agent.tools.grep`` imports the rule helpers; the same file runs as a private
subprocess for the Python engine. It imports only the standard library and
wcmatch so the worker starts quickly.

Every path component is opened relative to a directory descriptor with
O_NOFOLLOW, so swapping a directory for a symlink cannot escape the mount.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import PurePosixPath
import re
import signal
import stat
import sys

from wcmatch import glob as wcglob

LINE_BYTES = 2048
GLOB_BYTES = 16 * 1024
MAX_GLOBS = 64
WHITELIST, IGNORE, UNMATCHED = 1, -1, 0
ALWAYS_SKIPPED = ".git"
# gitignore-style globs: `*` stays within a component and also matches dotfiles.
_GLOB_FLAGS = wcglob.GLOBSTAR | wcglob.BRACE | wcglob.DOTMATCH
LARGE_FILE_NOTE = "Results incomplete: files larger than 10 MiB skipped."
UNREADABLE_NOTE = "Results incomplete: some files or directories could not be read."
LINE_NOTE = "Results truncated: matching line content shortened to 2048 bytes."


@dataclass(frozen=True)
class GlobRule:
    source: str
    negated: bool
    dir_only: bool
    pattern: str

    def matches(self, relative: str, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        return _compiled(self.pattern).match(relative)


_CACHE: dict[str, object] = {}


def _compiled(pattern: str):
    compiled = _CACHE.get(pattern)
    if compiled is None:
        compiled = _CACHE[pattern] = wcglob.compile(pattern, flags=_GLOB_FLAGS)
    return compiled


def _check_syntax(glob: str) -> None:
    """Reject the malformed globs ripgrep's globset rejects, before either engine runs."""
    depth = index = 0
    while index < len(glob):
        char = glob[index]
        if char == "\\":
            if index + 1 == len(glob):
                raise ValueError(f"Invalid glob {glob!r}: dangling '\\'")
            index += 2
            continue
        if char == "[":
            index += 1
            if index < len(glob) and glob[index] in "!^":
                index += 1
            members: list[str] = []
            first = True
            while index < len(glob) and (first or glob[index] != "]"):
                members.append(glob[index])
                first = False
                index += 1
            if index >= len(glob):
                raise ValueError(f"Invalid glob {glob!r}: unclosed character class; missing ']'")
            for position in range(1, len(members) - 1):
                if members[position] == "-" and members[position - 1] > members[position + 1]:
                    raise ValueError(f"Invalid glob {glob!r}: invalid range "
                                     f"{members[position - 1]!r} > {members[position + 1]!r}")
        elif char == "{":
            depth += 1
        elif char == "}":
            if depth == 0:
                raise ValueError(f"Invalid glob {glob!r}: unopened alternate group; missing '{{'")
            depth -= 1
        index += 1
    if depth:
        raise ValueError(f"Invalid glob {glob!r}: unclosed alternate group; missing '}}'")


def parse_globs(globs: str | list[str] | tuple[str, ...] | None) -> list[GlobRule]:
    """Parse ripgrep ``--glob`` rules: ``!`` excludes and the last matching rule wins.

    Translation follows ripgrep's override (gitignore-style) rules: a glob
    without ``/`` matches at any depth, a leading ``/`` or inner ``/`` anchors
    to the search directory, a trailing ``/`` matches only directories and
    ``dir/**`` matches the contents of ``dir`` but not ``dir`` itself.
    """
    if globs is None:
        return []
    items = [globs] if isinstance(globs, str) else list(globs)
    if len(items) > MAX_GLOBS:
        raise ValueError(f"at most {MAX_GLOBS} glob rules are allowed")
    rules = []
    for raw in items:
        if not isinstance(raw, str):
            raise ValueError("glob rules must be strings")
        if len(raw.encode("utf-8")) > GLOB_BYTES or any(c in raw for c in "\x00\n\r"):
            raise ValueError("each glob must be a single line of at most 16384 bytes without NUL")
        text = raw
        negated = False
        if text.startswith(("\\!", "\\#")):
            text = text[1:]
        elif text.startswith("#"):
            raise ValueError(f"Invalid glob {raw!r}: a leading '#' is a comment in ripgrep; escape it as '\\#'")
        elif text.startswith("!"):
            negated = True
            text = text[1:]
        if text.endswith((" ", "\t")) and not text.endswith(("\\ ", "\\\t")):
            raise ValueError(f"Invalid glob {raw!r}: trailing whitespace is ignored by ripgrep; escape it with '\\'")
        if ".." in text.replace("\\", "/").split("/"):
            raise ValueError(f"Path traversal not allowed in glob pattern {raw!r}")
        _check_syntax(text)
        anchored = not raw.startswith(("\\!", "\\#")) and text.startswith("/")
        if anchored:
            text = text[1:]
        dir_only = text.endswith("/")
        if dir_only:
            text = text[:-1]
        if not text:
            raise ValueError(f"Invalid glob {raw!r}: empty pattern")
        if not anchored and "/" not in text and not text.startswith("**/"):
            text = "**/" + text
        if text.endswith("/**"):
            text += "/*"
        try:
            _compiled(text)
        except Exception as exc:  # wcmatch raises private exception types.
            raise ValueError(f"Invalid glob {raw!r}: {exc}") from None
        rules.append(GlobRule(raw, negated, dir_only, text))
    return rules


def override_match(rules: list[GlobRule], relative: str, is_dir: bool) -> int:
    """ripgrep override semantics for a path relative to the search directory."""
    if not rules:
        return UNMATCHED
    for rule in reversed(rules):
        if rule.matches(relative, is_dir):
            return IGNORE if rule.negated else WHITELIST
    if not is_dir and any(not rule.negated for rule in rules):
        return IGNORE
    return UNMATCHED


def open_below(root_fd: int, parts: tuple[str, ...]) -> int:
    fd = os.dup(root_fd)
    try:
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_mount(root: str) -> int:
    """Open an absolute mount root without following any symlink component."""
    root_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fd = open_below(root_fd, PurePosixPath(root).parts[1:])
    finally:
        os.close(root_fd)
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise NotADirectoryError("search mount is not a directory")
    return fd


def emit(record: dict) -> None:
    sys.stdout.write(json.dumps(record, ensure_ascii=True) + "\n")


def display(name: str) -> str:
    return os.fsencode(name).decode("utf-8", errors="replace")


def clipped_line(line: str) -> str:
    raw = line.encode("utf-8")
    if len(raw) > LINE_BYTES:
        emit({"warning": LINE_NOTE})
        return raw[:LINE_BYTES].decode("utf-8", errors="ignore") + " [line truncated]"
    return line


def walk(dir_fd: int, prefix: str, rules: list[GlobRule]):
    """Yield (relative path, open fd) using ripgrep's override/hidden precedence."""
    try:
        with os.scandir(dir_fd) as iterator:
            entries = list(iterator)
    except OSError:
        emit({"warning": UNREADABLE_NOTE})
        return
    for entry in entries:
        name = entry.name
        # Never follow symlinks, including links inside the authorized mount.
        if name == ALWAYS_SKIPPED or entry.is_symlink():
            continue
        is_dir = entry.is_dir(follow_symlinks=False)
        if not is_dir and not entry.is_file(follow_symlinks=False):
            continue
        relative = f"{prefix}/{name}" if prefix else name
        decision = override_match(rules, relative, is_dir)
        if decision == IGNORE or (decision == UNMATCHED and name.startswith(".")):
            continue
        try:
            child = open_below(dir_fd, (name,))
        except OSError:
            emit({"warning": UNREADABLE_NOTE})
            continue
        try:
            if is_dir:
                if stat.S_ISDIR(os.fstat(child).st_mode):
                    yield from walk(child, relative, rules)
            else:
                yield relative, child
        finally:
            os.close(child)


def search_file(fd: int, virtual: str, payload: dict, matcher) -> None:
    mode = payload["output_mode"]
    limit = payload["file_limit"]
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        return
    if info.st_size > limit:
        emit({"warning": LARGE_FILE_NOTE})
        return
    try:
        data = os.read(fd, limit + 1)
    except OSError:
        emit({"warning": UNREADABLE_NOTE})
        return
    if len(data) > limit:
        emit({"warning": LARGE_FILE_NOTE})
        return
    if b"\x00" in data:
        return
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    count = 0
    for number, line in enumerate(lines, 1):
        if not matcher(line):
            continue
        count += 1
        if mode == "content":
            emit({"path": virtual, "line": number, "text": clipped_line(line.rstrip("\r"))})
        elif mode == "files_with_matches":
            emit({"path": virtual})
            break
    if count and mode == "count":
        emit({"path": virtual, "count": count})
    if count:
        # Like rg, publish each finished file so a timeout still returns partial results.
        sys.stdout.flush()


def run(payload: dict) -> None:
    if payload["fixed_string"]:
        literal = payload["pattern"]
        matcher = lambda line: literal in line  # noqa: E731
    else:
        matcher = re.compile(payload["pattern"]).search
    rules = parse_globs(payload["globs"])
    mount_fd = open_mount(payload["root"])
    try:
        target_fd = open_below(mount_fd, tuple(p for p in payload["relative"].split("/") if p))
        try:
            base = PurePosixPath(payload["virtual_root"]) / payload["relative"]
            if stat.S_ISREG(os.fstat(target_fd).st_mode):
                # The supervisor already applied globs and size/binary checks to explicit files.
                search_file(target_fd, display(base.as_posix()), payload, matcher)
                return
            for relative, fd in walk(target_fd, "", rules):
                search_file(fd, display((base / relative).as_posix()), payload, matcher)
        finally:
            os.close(target_fd)
    finally:
        os.close(mount_fd)


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
    finally:
        sys.stdout.flush()
