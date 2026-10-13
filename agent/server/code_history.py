"""Private, bounded workspace snapshots and conflict-checked per-Run undo/redo.

No caller supplies paths or file contents. File access uses directory descriptors
and O_NOFOLLOW; the journal rolls incomplete restores back before admission.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
from threading import RLock
from uuid import uuid4

EXCLUDED = frozenset({".git", ".deep-agent", "node_modules", ".venv", "venv", "__pycache__"})
MAX_FILE = 8 * 1024 * 1024
MAX_SNAPSHOT = 64 * 1024 * 1024
MAX_ENTRIES = 5000
MAX_STORAGE = 512 * 1024 * 1024
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class CodeHistoryError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class CodeHistory:
    def __init__(self, state_dir: Path, projects: dict[str, Path]):
        self.projects = projects
        self.lock = RLock()
        self.blocked_projects: set[str] = set()
        self.objects = state_dir / "code-objects"
        self.objects.mkdir(mode=0o700, exist_ok=True)
        with self._objects_fd() as directory:
            os.fchmod(directory, 0o700)
            self.bytes_stored = sum(os.stat(name, dir_fd=directory, follow_symlinks=False).st_size for name in os.listdir(directory))
        path = state_dir / "code-history.sqlite3"
        self.conn = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL,
                before_state TEXT, after_state TEXT, reason TEXT NOT NULL DEFAULT '', revision TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS restores (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, project_id TEXT NOT NULL,
                source_state TEXT NOT NULL, target_state TEXT NOT NULL, paths TEXT NOT NULL
            );
        """)
        # A service crash did not establish a trustworthy post-Run boundary.
        with self.conn:
            self.conn.execute("UPDATE checkpoints SET status='unavailable',reason=? WHERE status='recording'",
                              ("轮次未完整结束，无法确认修改后的快照",))
        self.recover()

    def blocked(self, project: str) -> bool:
        return project in self.blocked_projects

    @contextmanager
    def _root(self, project: str):
        fd = os.open(self.projects[project], DIRECTORY_FLAGS)
        try:
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def _parent(self, root: int, path: str):
        parts = PurePosixPath(path).parts
        if not parts or path.startswith("/") or any(p in {".", ".."} or p in EXCLUDED for p in parts):
            raise CodeHistoryError("不安全的快照路径")
        fd = os.dup(root)
        try:
            for part in parts[:-1]:
                child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def _blob(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        with self._objects_fd() as directory:
            try:
                os.stat(digest, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                if self.bytes_stored + len(data) > MAX_STORAGE:
                    raise CodeHistoryError("代码快照存储达到512MiB上限，本轮不能回退")
                fd = os.open(digest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.fsync(directory)
                    self.bytes_stored += len(data)
                except BaseException:
                    os.unlink(digest, dir_fd=directory)
                    raise
        return digest

    @contextmanager
    def _objects_fd(self):
        fd = os.open(self.objects, DIRECTORY_FLAGS)
        try:
            yield fd
        finally:
            os.close(fd)

    def capture(self, project: str, *, save: bool = True) -> dict:
        result: dict = {}
        total = 0
        with self._root(project) as root:
            info = os.fstat(root)
            result[""] = {"kind": "root", "device": info.st_dev, "inode": info.st_ino}

            def walk(directory: int, prefix: str):
                nonlocal total
                for name in sorted(os.listdir(directory)):
                    if name in EXCLUDED:
                        continue
                    path = f"{prefix}/{name}" if prefix else name
                    if len(result) >= MAX_ENTRIES:
                        raise CodeHistoryError("工作区超过5000项，无法完整记录本轮快照")
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    mode = stat.S_IMODE(info.st_mode) & 0o777
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(name, DIRECTORY_FLAGS, dir_fd=directory)
                        try:
                            actual = os.fstat(child)
                            if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
                                raise CodeHistoryError("快照期间目录发生变化")
                            result[path] = {"kind": "directory", "mode": mode}
                            walk(child, path)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode):
                        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                        try:
                            before = os.fstat(fd)
                            if (before.st_dev, before.st_ino, before.st_mode) != (info.st_dev, info.st_ino, info.st_mode):
                                raise CodeHistoryError("快照期间文件发生变化")
                            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE:
                                raise CodeHistoryError("文件超过8MiB或不是普通文件，无法完整记录快照")
                            with os.fdopen(fd, "rb", closefd=False) as stream:
                                data = stream.read(MAX_FILE + 1)
                            after = os.fstat(fd)
                            if len(data) > MAX_FILE or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                                raise CodeHistoryError("快照期间文件发生变化")
                        finally:
                            os.close(fd)
                        total += len(data)
                        if total > MAX_SNAPSHOT:
                            raise CodeHistoryError("工作区内容超过64MiB，无法完整记录本轮快照")
                        digest = self._blob(data) if save else hashlib.sha256(data).hexdigest()
                        result[path] = {"kind": "file", "hash": digest, "mode": mode, "size": len(data)}
                    elif stat.S_ISLNK(info.st_mode):
                        result[path] = {"kind": "symlink", "target": os.readlink(name, dir_fd=directory)}
                    else:
                        result[path] = {"kind": "special", "inode": info.st_ino, "device": info.st_dev}
            walk(root, "")
        return result

    @staticmethod
    def changed(before: dict, after: dict) -> list[str]:
        return sorted(path for path in before.keys() | after.keys() if path and before.get(path) != after.get(path))

    def begin(self, run_id: str, project: str):
        with self.lock:
            try:
                before = self.capture(project)
                status, reason = "recording", ""
            except (OSError, CodeHistoryError) as exc:
                before, status = None, "unavailable"
                reason = str(exc) if isinstance(exc, CodeHistoryError) else "无法安全读取工作区快照"
            with self.conn:
                self.conn.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
                                  (run_id, project, status, json.dumps(before), None, reason, str(uuid4())))

    def end(self, run_id: str):
        with self.lock:
            row = self.conn.execute("SELECT * FROM checkpoints WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["status"] != "recording":
                return
            after = None
            try:
                before = json.loads(row["before_state"])
                after = self.capture(row["project_id"])
                if before[""] != after[""]:
                    raise CodeHistoryError("项目根目录发生变化，不能安全回退")
                for path in self.changed(before, after):
                    if any(state.get(path, {}).get("kind") in {"symlink", "special"} for state in (before, after)):
                        raise CodeHistoryError("本轮修改了符号链接或特殊文件，不能安全回退")
                status, reason = "ready", ""
            except (OSError, CodeHistoryError) as exc:
                status = "unavailable"
                reason = str(exc) if isinstance(exc, CodeHistoryError) else "无法安全读取轮次结束快照"
            with self.conn:
                self.conn.execute("UPDATE checkpoints SET status=?,after_state=?,reason=?,revision=? WHERE run_id=?",
                                  (status, json.dumps(after), reason, str(uuid4()), run_id))

    def describe(self, run_id: str) -> dict:
        with self.lock:
            row = self.conn.execute("SELECT * FROM checkpoints WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return {"run_id": run_id, "available": False, "status": "unavailable", "reason": "旧轮次没有代码快照，无法回退", "files": []}
            available = row["status"] in {"ready", "reverted"} and not self.blocked(row["project_id"])
            before = json.loads(row["before_state"] or "null") or {}
            after = json.loads(row["after_state"] or "null") or {}
            paths = self.changed(before, after) if available else []
            files = [{"path": path, "kind": (after.get(path) or before[path])["kind"],
                      "change": "create" if path not in before else "delete" if path not in after else "modify"} for path in paths]
            return {"run_id": run_id, "available": available, "status": row["status"], "revision": row["revision"],
                    "reason": "代码回退恢复未完成，需要人工排查" if self.blocked(row["project_id"]) else row["reason"],
                    "files": files, "can_undo": available and bool(paths) and row["status"] == "ready",
                    "can_redo": available and bool(paths) and row["status"] == "reverted"}

    def _validate(self, current: dict, source: dict, target: dict, paths: list[str]):
        if current.get("") != source.get(""):
            raise CodeHistoryError("项目根目录已改变，拒绝回退")
        conflicts = [p for p in paths if current.get(p) != source.get(p)]
        # Never delete a created directory if unrelated files were added there.
        for path in paths:
            if source.get(path, {}).get("kind") == "directory" and target.get(path, {}).get("kind") != "directory":
                if any(p.startswith(path + "/") and p not in paths for p in current):
                    conflicts.append(path)
        if conflicts:
            raise CodeHistoryError("文件已被后续轮次或手动修改，拒绝覆盖：" + "、".join(sorted(set(conflicts))[:10]))

    def _assert_entry(self, parent: int, name: str, expected: dict | None):
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if expected is None:
                return
            raise CodeHistoryError("恢复期间文件被移除") from None
        if expected is None:
            raise CodeHistoryError("恢复期间出现新文件，拒绝覆盖")
        if stat.S_IMODE(info.st_mode) & 0o777 != expected["mode"]:
            raise CodeHistoryError("恢复期间文件权限被修改")
        if expected["kind"] == "directory" and stat.S_ISDIR(info.st_mode):
            return
        if expected["kind"] != "file" or not stat.S_ISREG(info.st_mode):
            raise CodeHistoryError("恢复期间文件类型被修改")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(MAX_FILE + 1)
        if hashlib.sha256(data).hexdigest() != expected["hash"]:
            raise CodeHistoryError("恢复期间文件内容被修改，拒绝覆盖")

    def _put_file(self, root: int, path: str, entry: dict, operation: str, expected: dict | None):
        digest = entry["hash"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise CodeHistoryError("代码快照索引损坏，拒绝恢复", 503)
        with self._objects_fd() as objects:
            fd = os.open(digest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=objects)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
                os.close(fd)
                raise CodeHistoryError("代码快照内容损坏，拒绝恢复", 503)
            with os.fdopen(fd, "rb") as stream:
                data = stream.read(MAX_FILE + 1)
        if hashlib.sha256(data).hexdigest() != digest:
            raise CodeHistoryError("代码快照内容损坏，拒绝恢复", 503)
        with self._parent(root, path) as (parent, name):
            temporary = f".code-restore-{operation}-{hashlib.sha256(path.encode()).hexdigest()[:12]}"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), entry["mode"])
                    os.fsync(stream.fileno())
                self._assert_entry(parent, name, expected)
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def _check_removals(self, root: int, current: dict, target: dict, paths: list[str]):
        # Include ignored/unrecorded children; never discard a dependency folder.
        for path in paths:
            if current.get(path, {}).get("kind") == "directory" and target.get(path, {}).get("kind") != "directory":
                with self._parent(root, path) as (parent, name):
                    child = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
                    try:
                        if any(f"{path}/{entry}" not in paths for entry in os.listdir(child)):
                            raise CodeHistoryError("目录包含本轮以外的文件，拒绝删除：" + path)
                    finally:
                        os.close(child)

    def _apply(self, project: str, current: dict, target: dict, paths: list[str], operation: str):
        states = current.copy()
        with self._root(project) as root:
            info = os.fstat(root)
            if current[""] != {"kind": "root", "device": info.st_dev, "inode": info.st_ino}:
                raise CodeHistoryError("项目根目录发生变化")
            self._check_removals(root, current, target, paths)
            for path in sorted(paths, key=lambda p: (-p.count("/"), p)):
                old, new = current.get(path), target.get(path)
                if old and old["kind"] == "file" and (not new or new["kind"] != "file"):
                    with self._parent(root, path) as (parent, name):
                        self._assert_entry(parent, name, old)
                        os.unlink(name, dir_fd=parent)
                        os.fsync(parent)
                    states.pop(path, None)
                if old and old["kind"] == "directory" and (not new or new["kind"] != "directory"):
                    with self._parent(root, path) as (parent, name):
                        self._assert_entry(parent, name, old)
                        os.rmdir(name, dir_fd=parent)
                        os.fsync(parent)
                    states.pop(path, None)
            for path in sorted(paths, key=lambda p: (p.count("/"), p)):
                old, new = current.get(path), target.get(path)
                if new and new["kind"] == "directory":
                    with self._parent(root, path) as (parent, name):
                        self._assert_entry(parent, name, states.get(path))
                        if not old or old["kind"] != "directory":
                            os.mkdir(name, new["mode"], dir_fd=parent)
                        child = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
                        try:
                            os.fchmod(child, new["mode"])
                            os.fsync(child)
                        finally:
                            os.close(child)
                        os.fsync(parent)
                    states[path] = new
                elif new and new["kind"] == "file" and old != new:
                    self._put_file(root, path, new, operation, states.get(path))
                    states[path] = new

    def _rollback(self, row) -> None:
        project = row["project_id"]
        source, target, paths = json.loads(row["source_state"]), json.loads(row["target_state"]), json.loads(row["paths"])
        current = self.capture(project, save=False)
        if current.get("") != source.get(""):
            raise CodeHistoryError("项目根目录发生变化，无法恢复")
        if any(current.get(p) not in (source.get(p), target.get(p)) for p in paths):
            raise CodeHistoryError("恢复期间文件被修改，需要人工排查")
        self._validate(current, current, source, paths)
        self._apply(project, current, source, paths, row["id"])
        after = self.capture(project, save=False)
        if after.get("") != source.get("") or any(after.get(p) != source.get(p) for p in paths):
            raise CodeHistoryError("代码恢复未能完成，需要人工排查")
        with self.conn:
            self.conn.execute("DELETE FROM restores WHERE id=?", (row["id"],))

    def recover(self):
        with self.lock:
            for row in self.conn.execute("SELECT * FROM restores").fetchall():
                try:
                    if row["project_id"] not in self.projects:
                        raise CodeHistoryError("Project no longer configured")
                    self._rollback(row)
                except (OSError, CodeHistoryError):
                    self.blocked_projects.add(row["project_id"])

    def restore(self, run_id: str, action: str, revision: str) -> dict:
        with self.lock:
            if action not in {"undo", "redo"}:
                raise CodeHistoryError("无效的代码回退操作", 422)
            row = self.conn.execute("SELECT * FROM checkpoints WHERE run_id=?", (run_id,)).fetchone()
            expected = "ready" if action == "undo" else "reverted"
            if row is None or row["status"] != expected or row["revision"] != revision or self.blocked(row["project_id"]):
                raise CodeHistoryError("回退状态已改变或快照不可用，请重新检查")
            before, after = json.loads(row["before_state"]), json.loads(row["after_state"])
            source, target = (after, before) if action == "undo" else (before, after)
            paths = self.changed(source, target)
            if not paths:
                raise CodeHistoryError("本轮没有可回退的文件修改")
            current = self.capture(row["project_id"], save=False)
            self._validate(current, source, target, paths)
            with self._root(row["project_id"]) as root:
                self._check_removals(root, current, target, paths)
            operation = str(uuid4())
            with self.conn:
                self.conn.execute("INSERT INTO restores VALUES (?,?,?,?,?,?)", (operation, run_id, row["project_id"],
                                  json.dumps(source), json.dumps(target), json.dumps(paths)))
            try:
                self._apply(row["project_id"], current, target, paths, operation)
                check = self.capture(row["project_id"], save=False)
                if check.get("") != target.get("") or any(check.get(p) != target.get(p) for p in paths):
                    raise CodeHistoryError("恢复期间文件发生变化")
                with self.conn:
                    self.conn.execute("UPDATE checkpoints SET status=?,revision=? WHERE run_id=?",
                                      ("reverted" if action == "undo" else "ready", str(uuid4()), run_id))
                    self.conn.execute("DELETE FROM restores WHERE id=?", (operation,))
            except Exception:
                pending = self.conn.execute("SELECT * FROM restores WHERE id=?", (operation,)).fetchone()
                try:
                    self._rollback(pending)
                except (OSError, CodeHistoryError):
                    self.blocked_projects.add(row["project_id"])
                    raise CodeHistoryError("代码恢复未完成，项目已锁定，请人工排查", 503) from None
                raise CodeHistoryError("代码回退未成功，已恢复操作前的文件状态", 503) from None
            return self.describe(run_id)

    def close(self):
        with self.lock:
            self.conn.close()
