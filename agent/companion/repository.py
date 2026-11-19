"""Bounded, read-only source inspection for the Companion worker (Linux)."""

from contextlib import contextmanager
from itertools import islice
import os
from pathlib import Path
import stat
import re
from time import monotonic

SOURCE = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".rs",
    ".go",
    ".c",
    ".h",
    ".cpp",
    ".java",
    ".kt",
    ".rb",
    ".sh",
    ".md",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".lock",
    ".ini",
    ".cfg",
    ".swift",
    ".sql",
    ".vue",
    ".html",
    ".css",
}
SKIP = {"node_modules", "__pycache__", "venv", "vendor", "target", "dist", "build"}
PRIVATE = re.compile(
    r"(?:secret|credentials?|passwd|password|auth(?:entication)?|token|api[-_]?key|kubeconfig|id_rsa|id_ed25519|^config(?:uration)?[.])",
    re.I,
)
CREDENTIAL = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY|(?:Bearer |sk-|gh[pousr]_|AKIA)[A-Za-z0-9_.+/=-]{8,}|"
    r"""(?:api[_-]?key|secret|token|password|authorization)\s*["']?\s*[:=]\s*["']?[^\s,;]{4,}""",
    re.I,
)
MAX_FILE = 256 * 1024


def _mount(fd):
    for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
        if line.startswith("mnt_id:"):
            return int(line.split()[1])
    raise OSError("mount identity unavailable")


class Repository:
    def __init__(self, root, protected=(), secrets=()):
        self.secrets = tuple(s for s in secrets if s)
        self.root = Path(root).resolve()
        self.protected = tuple(Path(path).resolve() for path in protected)

    @contextmanager
    def opened(self, path="", *, directory=False):
        parts = path.split("/") if path else []
        if any(
            not p
            or p.startswith(".")
            or p in SKIP
            or PRIVATE.search(p)
            or not p.isprintable()
            for p in parts
        ):
            raise ValueError("outside searchable source")
        if not directory:
            name = Path(path)
            if name.suffix.lower() not in SOURCE and name.name not in {
                "Dockerfile",
                "Makefile",
                "Gemfile",
            }:
                raise ValueError("unsupported file type")
        flags = os.O_PATH | os.O_NOFOLLOW
        fd = os.open(self.root, flags | os.O_DIRECTORY)
        try:
            mount = _mount(fd)
            for i, part in enumerate(parts):
                child = os.open(
                    part,
                    flags | (os.O_DIRECTORY if directory or i < len(parts) - 1 else 0),
                    dir_fd=fd,
                )
                os.close(fd)
                fd = child
                if _mount(fd) != mount:
                    raise ValueError("nested mount")
            actual = Path(os.readlink(f"/proc/self/fd/{fd}"))
            if actual != self.root.joinpath(*parts) or any(
                actual.is_relative_to(p) for p in self.protected
            ):
                raise ValueError("outside searchable source")
            info = os.fstat(fd)
            if not directory and (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > MAX_FILE
            ):
                raise ValueError("unsupported file")
            # Reopen the verified inode, never the caller-controlled pathname.
            # O_PATH above also avoids opening a device or FIFO during validation.
            readable = os.open(f"/proc/self/fd/{fd}", os.O_RDONLY | os.O_NONBLOCK)
            try:
                yield readable
            finally:
                os.close(readable)
        finally:
            os.close(fd)

    def lines(self, path):
        with self.opened(path) as fd:
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(MAX_FILE + 1)
            if len(data) > MAX_FILE or b"\0" in data:
                raise ValueError("unsupported content")
            text = data.decode("utf-8")
            if CREDENTIAL.search(text) or any(
                secret in text for secret in self.secrets
            ):
                raise ValueError("sensitive source")
            return text.splitlines()

    def read(self, path, start=1):
        try:
            lines = self.lines(path)
        except (OSError, ValueError):
            return "File unavailable."
        start = max(1, start)
        return (
            "\n".join(
                f"{path}:{i} {line[:240]}"
                for i, line in enumerate(lines[start - 1 : start + 15], start)
            )[:5000]
            or "No lines."
        )

    def search(self, term, *, paths_only=False):
        term = term.strip().casefold()
        if (not term and not paths_only) or len(term) > 160:
            return "Supply a short literal or filename."
        directories, hits = [""], []
        remaining = 3000
        budget = 8 * 1024 * 1024
        deadline = monotonic() + 1.5
        while directories and remaining and budget > 0 and monotonic() < deadline:
            base = directories.pop()
            try:
                with self.opened(base, directory=True) as fd, os.scandir(fd) as entries:
                    for entry in islice(entries, remaining):
                        if monotonic() >= deadline:
                            break
                        remaining -= 1
                        name = entry.name
                        if name.startswith(".") or name in SKIP:
                            continue
                        path = f"{base}/{name}" if base else name
                        if entry.is_dir(follow_symlinks=False):
                            directories.append(path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        size = entry.stat(follow_symlinks=False).st_size
                        if size > min(budget, MAX_FILE):
                            continue
                        budget -= size
                        try:
                            lines = self.lines(path)
                        except (OSError, ValueError):
                            continue
                        if paths_only:
                            if term in path.casefold():
                                hits.append(path)
                            if len(hits) == 12:
                                return "\n".join(hits)
                            continue
                        for i, line in enumerate(lines, 1):
                            if term in line.casefold() or (
                                i == 1 and term in path.casefold()
                            ):
                                hits.append(f"{path}:{i} {line[:180]}")
                            if len(hits) == 12:
                                return "\n".join(hits)
                        if budget <= 0:
                            break
            except (OSError, ValueError):
                continue
        return "\n".join(hits) or "No match in the inspected source."
