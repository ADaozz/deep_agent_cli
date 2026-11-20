"""Bounded, read-only source inspection for the Companion worker (Linux)."""

from contextlib import contextmanager
import os
from pathlib import Path
import stat
from time import monotonic

from agent.companion.config import SOURCE_READ_MAX_LINES
from agent.companion.redaction import PRIVATE_KEY_MARKER, redact_source_text

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
PRIVATE_NAMES = frozenset({
    "credentials", "credential", "secrets", "secret", "passwords", "api_keys", "api-key",
    "kubeconfig", "id_rsa", "id_ed25519", "companion.key",
})
PRIVATE_CONFIG_NAMES = PRIVATE_NAMES | {"auth", "token", "tokens", "password", "api_key"}
PRIVATE_CONFIG_SUFFIXES = {".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".txt", ".xml"}
MAX_FILE = 256 * 1024
SEARCH_SECONDS = 1.5
OUTPUT_CHARS = 5000


def _private_name(part):
    name = Path(part.casefold())
    return name.name in PRIVATE_NAMES or (
        name.stem in PRIVATE_CONFIG_NAMES and name.suffix in PRIVATE_CONFIG_SUFFIXES
    ) or name.suffix in {".pem", ".key", ".p12", ".pfx"}


def _source_name(path):
    name = Path(path)
    return name.suffix.lower() in SOURCE or name.name in {"Dockerfile", "Makefile", "Gemfile"}


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
    def opened(self, path="", *, directory=False, budget=None):
        if budget:
            budget.check()
        if len(path) > 512:
            raise ValueError("outside searchable source")
        parts = path.split("/") if path else []
        if any(
            not p
            or p.startswith(".")
            or p in SKIP
            or _private_name(p)
            or any(secret in p for secret in self.secrets)
            or not p.isprintable()
            for p in parts
        ):
            raise ValueError("outside searchable source")
        if not directory and not _source_name(path):
            raise ValueError("unsupported file type")
        flags = os.O_PATH | os.O_NOFOLLOW
        fd = os.open(self.root, flags | os.O_DIRECTORY)
        try:
            mount = _mount(fd)
            for i, part in enumerate(parts):
                if budget:
                    budget.check()
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

    def lines(self, path, budget=None):
        with self.opened(path, budget=budget) as fd:
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(MAX_FILE + 1)
            if budget:
                budget.check()
            if len(data) > MAX_FILE or b"\0" in data:
                raise ValueError("unsupported content")
            text = data.decode("utf-8")
            if PRIVATE_KEY_MARKER in text and "-----BEGIN " in text or any(
                "\n" in secret and secret in text for secret in self.secrets
            ):
                raise ValueError("private key material")
            lines = redact_source_text(text, self.secrets).splitlines()
            if budget:
                budget.check()
            return lines

    def read(self, path, start=1, limit=50, *, budget=None):
        try:
            lines = self.lines(path, budget)
        except TimeoutError:
            raise
        except (OSError, ValueError):
            return "File unavailable."
        start = max(1, start)
        limit = min(SOURCE_READ_MAX_LINES, max(1, limit))
        output, used = [], 0
        for number, line in enumerate(lines[start - 1:start - 1 + limit], start):
            text = f"{path}:{number} {line[:239]}…" if len(line) > 240 else f"{path}:{number} {line}"
            if used + len(text) + 1 > OUTPUT_CHARS - 100:
                output.append(f"[Character limit reached. Continue with start={number}.]")
                break
            output.append(text)
            used += len(text) + 1
        else:
            if start - 1 + limit < len(lines):
                output.append(f"[More lines available. Continue with start={start + limit}.]")
        return "\n".join(output) or "No lines."

    def search(self, term, *, directory="", paths_only=False, budget=None):
        term = term.strip().casefold()
        if (not term and not paths_only) or len(term) > 160:
            return "Supply a short literal or filename."
        directories, hits = [directory], []
        deadline = monotonic() + SEARCH_SECONDS
        incomplete = set()

        def result():
            while hits and sum(len(hit) + 1 for hit in hits) > OUTPUT_CHARS - 160:
                incomplete.add("character limit")
                hits.pop()
            text = "\n".join(hits) or "No match in the inspected source."
            if incomplete:
                return text + "\n[Scan incomplete: " + ", ".join(sorted(incomplete)) + ".]"
            return text

        while directories:
            base = directories.pop()
            try:
                with self.opened(base, directory=True, budget=budget) as fd, os.scandir(fd) as entries:
                    for entry in entries:
                        if monotonic() >= deadline:
                            incomplete.add("time budget")
                            return result()
                        if budget:
                            budget.check()
                        name = entry.name
                        if name.startswith(".") or name in SKIP or _private_name(name):
                            continue
                        path = f"{base}/{name}" if base else name
                        if entry.is_dir(follow_symlinks=False):
                            directories.append(path)
                            continue
                        if not _source_name(name) or not entry.is_file(follow_symlinks=False):
                            continue
                        if paths_only and term not in path.casefold():
                            continue
                        try:
                            lines = self.lines(path, budget)
                        except TimeoutError:
                            raise
                        except (OSError, ValueError):
                            continue
                        if paths_only:
                            if term in path.casefold():
                                hits.append(path)
                            if len(hits) == 12:
                                incomplete.add("result limit")
                                return result()
                            continue
                        for i, line in enumerate(lines, 1):
                            if i % 64 == 1:
                                if budget:
                                    budget.check()
                                if monotonic() >= deadline:
                                    incomplete.add("time budget")
                                    return result()
                            if term in line.casefold() or (
                                i == 1 and term in path.casefold()
                            ):
                                hits.append(f"{path}:{i} {line[:180]}")
                            if len(hits) == 12:
                                incomplete.add("result limit")
                                return result()
            except TimeoutError:
                raise
            except (OSError, ValueError):
                if base == directory and directory:
                    return "Search directory unavailable."
        return result()
