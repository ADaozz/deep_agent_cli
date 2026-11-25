"""Unified host credential storage (session memory, encrypted disk, and environment)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken

from agent.execution.layout import valid_env_name
from agent.redaction import SecretRef


SecretScope = Literal["host", "sandbox"]
SecretStorage = Literal["session", "persistent", "environment"]
ManagedStorage = Literal["session", "persistent"]

SECRET_SCOPES: tuple[SecretScope, ...] = ("host", "sandbox")
MANAGED_STORAGES: tuple[ManagedStorage, ...] = ("session", "persistent")

# Never allow credentials to shadow loader/sandbox-critical variables.
RESERVED_SECRET_NAMES: frozenset[str] = frozenset({
    "PATH",
    "HOME",
    "PWD",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "LANG",
    "LC_ALL",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "NODE_OPTIONS",
    "BASH_ENV",
    "ENV",
    "IFS",
    "DEEP_AGENT_CONFIG",
    "DEEP_AGENT_EXECUTION_OWNER",
})


class SecretExistsError(ValueError):
    """Raised when writing an existing secret without explicit overwrite confirmation."""

    def __init__(self, names: Sequence[str]) -> None:
        joined = ", ".join(names)
        super().__init__(f"Credential already exists and requires overwrite confirmation: {joined}")
        self.names = tuple(names)


class SecretAccessError(PermissionError):
    """Raised when a managed secret's scope or authorized consumers reject a caller."""


def valid_secret_name(name: object) -> bool:
    return isinstance(name, str) and valid_env_name(name) and name not in RESERVED_SECRET_NAMES


def validate_secret_name(name: object) -> str:
    text = str(name or "").strip()
    if not text or not valid_env_name(text):
        raise ValueError(
            f"Invalid credential reference name {name!r}: must match [A-Za-z_][A-Za-z0-9_]*"
        )
    if text in RESERVED_SECRET_NAMES or text.startswith("LC_"):
        raise ValueError(f"Credential reference name {text!r} is reserved and cannot be used")
    return text


def validate_secret_scope(scope: object) -> SecretScope:
    text = str(scope or "").strip().lower()
    if text == "host":
        return "host"
    if text == "sandbox":
        return "sandbox"
    raise ValueError(f"Invalid secret_scope {scope!r}: must be 'host' or 'sandbox'")


def load_or_create_fernet(directory: Path) -> Fernet:
    """Create ``directory`` (0700) and atomically load or initialize ``directory/key`` (0600)."""
    if directory.exists() and directory.is_symlink():
        raise OSError(f"Refusing symlinked secret directory: {directory}")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    key_path = directory / "key"
    fd = os.open(key_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        os.fchmod(stream.fileno(), 0o600)
        key = stream.read().strip()
        if not key:
            key = Fernet.generate_key()
            stream.seek(0)
            stream.truncate()
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        return Fernet(key)


@dataclass(frozen=True)
class SecretSubmission:
    """Isolated credential input submitted from a trusted host controller."""

    value: str = field(repr=False)
    storage: ManagedStorage = "session"
    overwrite: bool = False
    consumers: tuple[str, ...] = ()


@dataclass(frozen=True)
class SecretMetadata:
    """Public, value-free metadata for UI display and inspection."""

    name: str
    scope: SecretScope
    storage: ManagedStorage
    session_id: str | None = None
    consumers: tuple[str, ...] = ()
    updated_at: str = ""


@dataclass(frozen=True)
class ResolvedSecret:
    """Resolved credential for an authorized host or sandbox consumer."""

    name: str
    value: str = field(repr=False)
    scope: SecretScope = "host"
    storage: SecretStorage = "session"
    consumers: tuple[str, ...] = ()


@dataclass
class _SecretRecord:
    name: str
    value: str = field(repr=False)
    scope: SecretScope = "host"
    storage: ManagedStorage = "session"
    session_id: str | None = None
    consumers: tuple[str, ...] = ()
    updated_at: str = ""

    def metadata(self) -> SecretMetadata:
        return SecretMetadata(
            name=self.name,
            scope=self.scope,
            storage=self.storage,
            session_id=self.session_id,
            consumers=self.consumers,
            updated_at=self.updated_at,
        )


def _consumer_matches(authorized: Iterable[str], requested: str | None, scope: SecretScope) -> bool:
    if requested is None:
        return True
    allowed = set(authorized)
    if scope == "sandbox":
        return requested == "sandbox" and ("sandbox" in allowed or "*" in allowed)
    # Host scope: exact match (e.g. "mcp:github@https://...") or server-level grant
    if requested in allowed or "host:*" in allowed:
        return True
    if requested.startswith("mcp:"):
        # Allow exact "mcp:<server>@<target>" or explicit "mcp:<server>" grant.
        server_prefix, _, _target = requested.partition("@")
        if server_prefix in allowed:
            return True
    return False


class SecretStore:
    """Thread-safe session + encrypted persistent credential store.

    Resolution priority for ``resolve_secret``:
    1. Active root session memory store (``session_id``)
    2. Encrypted persistent store under ``<config_dir>/secrets/entries``
    3. Existing process ``os.environ`` (only when ``allow_env=True`` and no managed
       credential with that name exists in session or persistent storage).
    """

    def __init__(
        self,
        config_dir: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        base = (config_dir or (Path.home() / ".deep-agent")).expanduser()
        self.directory = base / "secrets"
        self.entries_dir = self.directory / "entries"
        self._environ = os.environ if environ is None else environ
        self._lock = threading.RLock()
        self._session_entries: dict[str, dict[str, _SecretRecord]] = {}
        # Keep retired values in memory for the process lifetime so running background
        # processes cannot leak a recently rotated or deleted credential before exit.
        self._retired_refs: list[SecretRef] = []

    def _file_lock(self):
        if self.directory.exists() and self.directory.is_symlink():
            raise OSError(f"Refusing symlinked secret directory: {self.directory}")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        if self.entries_dir.exists() and self.entries_dir.is_symlink():
            raise OSError(f"Refusing symlinked secret entries directory: {self.entries_dir}")
        self.entries_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.entries_dir, 0o700)
        lock_path = self.directory / ".lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        stream = os.fdopen(fd, "r+b")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX)
            os.fchmod(stream.fileno(), 0o600)
            return stream
        except BaseException:
            stream.close()
            raise

    def _entry_path(self, name: str) -> Path:
        validated = validate_secret_name(name)
        return self.entries_dir / f"{validated}.enc"

    def _read_persistent_locked(self, name: str, fernet: Fernet | None = None) -> _SecretRecord | None:
        path = self._entry_path(name)
        if not path.exists():
            return None
        if path.is_symlink():
            raise OSError(f"Refusing symlinked secret entry: {path}")
        cipher = fernet or load_or_create_fernet(self.directory)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            token = handle.read()
        try:
            raw = json.loads(cipher.decrypt(token).decode("utf-8"))
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise OSError(f"Corrupted encrypted credential {name}") from exc
        if not isinstance(raw, dict) or raw.get("name") != name:
            raise OSError(f"Invalid credential record for {name}")
        scope = validate_secret_scope(raw.get("scope"))
        value = raw.get("value")
        if not isinstance(value, str) or value == "":
            raise OSError(f"Invalid credential value for {name}")
        consumers_raw = raw.get("consumers")
        consumers = (
            tuple(dict.fromkeys(str(item) for item in consumers_raw if item))
            if isinstance(consumers_raw, list) else ()
        )
        return _SecretRecord(
            name=name,
            value=value,
            scope=scope,
            storage="persistent",
            session_id=None,
            consumers=consumers,
            updated_at=str(raw.get("updated_at") or ""),
        )

    def _write_persistent_locked(self, record: _SecretRecord, fernet: Fernet | None = None) -> None:
        cipher = fernet or load_or_create_fernet(self.directory)
        payload = json.dumps(
            {
                "name": record.name,
                "scope": record.scope,
                "value": record.value,
                "consumers": list(record.consumers),
                "updated_at": record.updated_at,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        encrypted = cipher.encrypt(payload)
        target = self._entry_path(record.name)
        fd, temp_path = tempfile.mkstemp(dir=self.entries_dir, prefix=f".{record.name}-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, target)
            os.chmod(target, 0o600)
        except BaseException:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def _retire_locked(self, name: str, value: str) -> None:
        if value:
            self._retired_refs.append(SecretRef(name=name, value=value, raw=False))

    def get_metadata(
        self,
        name: str,
        *,
        session_id: str | None = None,
        storage: ManagedStorage | None = None,
    ) -> SecretMetadata | None:
        validated = validate_secret_name(name)
        with self._lock:
            if storage in (None, "session") and session_id:
                rec = self._session_entries.get(session_id, {}).get(validated)
                if rec is not None:
                    return rec.metadata()
                if storage == "session":
                    return None
            if storage in (None, "persistent") and self.entries_dir.is_dir():
                with self._file_lock():
                    rec = self._read_persistent_locked(validated)
                    if rec is not None:
                        return rec.metadata()
        return None

    def find_conflicts(
        self,
        items: Sequence[tuple[str, ManagedStorage]],
        *,
        session_id: str | None = None,
    ) -> list[SecretMetadata]:
        """Return existing credentials that would be overwritten or shadowed."""
        conflicts: list[SecretMetadata] = []
        with self._lock:
            for name, storage in items:
                validated = validate_secret_name(name)
                existing = self.get_metadata(validated, session_id=session_id, storage=storage)
                if existing is None:
                    # Also warn if a persistent credential exists with the same name or vice versa.
                    other: ManagedStorage = "persistent" if storage == "session" else "session"
                    existing = self.get_metadata(validated, session_id=session_id, storage=other)
                if existing is not None:
                    conflicts.append(existing)
        return conflicts

    def set_secret(
        self,
        name: str,
        value: str,
        *,
        scope: SecretScope,
        storage: ManagedStorage = "session",
        session_id: str | None = None,
        consumers: Iterable[str] = (),
        overwrite: bool = False,
    ) -> SecretMetadata:
        return self.set_secrets_atomic(
            [(name, value, scope, storage, tuple(consumers))],
            session_id=session_id,
            overwrite=overwrite,
        )[0]

    def set_secrets_atomic(
        self,
        entries: Sequence[tuple[str, str, SecretScope, ManagedStorage, Sequence[str]]],
        *,
        session_id: str | None = None,
        overwrite: bool = False,
    ) -> list[SecretMetadata]:
        """Validate and store a batch of secrets atomically; rolls back on failure."""
        if not entries:
            return []
        normalized: list[tuple[str, str, SecretScope, ManagedStorage, tuple[str, ...]]] = []
        seen_names: set[str] = set()
        for raw_name, raw_value, raw_scope, raw_storage, raw_consumers in entries:
            name = validate_secret_name(raw_name)
            if name in seen_names:
                raise ValueError(f"Duplicate credential name in batch: {name}")
            seen_names.add(name)
            scope = validate_secret_scope(raw_scope)
            if raw_storage not in MANAGED_STORAGES:
                raise ValueError(f"Unsupported credential storage {raw_storage!r}")
            if raw_storage == "session" and not session_id:
                raise ValueError("Session-scoped credentials require an active session_id")
            if not isinstance(raw_value, str) or raw_value == "":
                raise ValueError(f"Credential {name} must not be empty")
            default_consumers = ("sandbox",) if scope == "sandbox" else ()
            merged_consumers = tuple(dict.fromkeys([*default_consumers, *(str(c) for c in raw_consumers if c)]))
            normalized.append((name, raw_value, scope, raw_storage, merged_consumers))

        now = datetime.now(timezone.utc).isoformat()
        needs_disk = any(storage == "persistent" for _, _, _, storage, _ in normalized) or self.entries_dir.is_dir()
        with self._lock:
            lock_ctx = self._file_lock() if needs_disk else None
            try:
                fernet = load_or_create_fernet(self.directory) if any(
                    storage == "persistent" for _, _, _, storage, _ in normalized
                ) else None
                if not overwrite:
                    conflicts: list[str] = []
                    for name, _val, _scope, storage, _cons in normalized:
                        in_sess = (
                            self._session_entries.get(session_id, {}).get(name)
                            if session_id else None
                        )
                        in_pers = (
                            self._read_persistent_locked(name, fernet)
                            if self.entries_dir.is_dir() else None
                        )
                        if storage == "session" and (in_sess is not None or in_pers is not None):
                            conflicts.append(name)
                        elif storage == "persistent" and (in_pers is not None or in_sess is not None):
                            conflicts.append(name)
                    if conflicts:
                        raise SecretExistsError(conflicts)

                # Snapshot previous state for atomic rollback if a persistent write fails mid-batch.
                prev_session: dict[str, _SecretRecord | None] = {}
                prev_persistent_bytes: dict[str, bytes | None] = {}
                for name, _val, _scope, storage, _cons in normalized:
                    if storage == "session" and session_id:
                        prev_session[name] = self._session_entries.get(session_id, {}).get(name)
                    elif storage == "persistent":
                        path = self._entry_path(name)
                        prev_persistent_bytes[name] = path.read_bytes() if path.exists() else None

                results: list[SecretMetadata] = []
                written_persistent: list[str] = []
                written_session: list[str] = []
                try:
                    for name, value, scope, storage, consumers in normalized:
                        if storage == "session":
                            assert session_id is not None
                            bucket = self._session_entries.setdefault(session_id, {})
                            old = bucket.get(name)
                            merged = consumers
                            if old is not None and old.scope == scope:
                                merged = tuple(dict.fromkeys([*old.consumers, *consumers]))
                            record = _SecretRecord(
                                name=name,
                                value=value,
                                scope=scope,
                                storage="session",
                                session_id=session_id,
                                consumers=merged,
                                updated_at=now,
                            )
                            bucket[name] = record
                            written_session.append(name)
                            if old is not None and old.value != value:
                                self._retire_locked(name, old.value)
                            results.append(record.metadata())
                        else:
                            old = self._read_persistent_locked(name, fernet)
                            merged = consumers
                            if old is not None and old.scope == scope:
                                merged = tuple(dict.fromkeys([*old.consumers, *consumers]))
                            record = _SecretRecord(
                                name=name,
                                value=value,
                                scope=scope,
                                storage="persistent",
                                session_id=None,
                                consumers=merged,
                                updated_at=now,
                            )
                            self._write_persistent_locked(record, fernet)
                            written_persistent.append(name)
                            if old is not None and old.value != value:
                                self._retire_locked(name, old.value)
                            results.append(record.metadata())
                except BaseException:
                    if session_id and written_session:
                        bucket = self._session_entries.get(session_id, {})
                        for name in written_session:
                            prior = prev_session.get(name)
                            if prior is None:
                                bucket.pop(name, None)
                            else:
                                bucket[name] = prior
                    for name in written_persistent:
                        path = self._entry_path(name)
                        prior_bytes = prev_persistent_bytes.get(name)
                        try:
                            if prior_bytes is None:
                                path.unlink(missing_ok=True)
                            else:
                                path.write_bytes(prior_bytes)
                                os.chmod(path, 0o600)
                        except OSError:
                            pass
                    raise
                return results
            finally:
                if lock_ctx is not None:
                    lock_ctx.close()

    def authorize_consumer(
        self,
        name: str,
        consumer: str,
        *,
        expected_scope: SecretScope = "host",
        session_id: str | None = None,
    ) -> bool:
        """Grant ``consumer`` access to an existing managed credential matching ``expected_scope``."""
        validated = validate_secret_name(name)
        if not consumer:
            return False
        with self._lock:
            if session_id:
                rec = self._session_entries.get(session_id, {}).get(validated)
                if rec is not None:
                    if rec.scope != expected_scope:
                        raise SecretAccessError(
                            f"Credential {validated} has scope '{rec.scope}', cannot authorize for '{expected_scope}'"
                        )
                    if consumer not in rec.consumers:
                        rec.consumers = (*rec.consumers, consumer)
                    return True
            if self.entries_dir.is_dir():
                with self._file_lock():
                    fernet = load_or_create_fernet(self.directory)
                    rec = self._read_persistent_locked(validated, fernet)
                    if rec is not None:
                        if rec.scope != expected_scope:
                            raise SecretAccessError(
                                f"Credential {validated} has scope '{rec.scope}', cannot authorize for '{expected_scope}'"
                            )
                        if consumer not in rec.consumers:
                            rec.consumers = (*rec.consumers, consumer)
                            self._write_persistent_locked(rec, fernet)
                        return True
        return False

    def resolve_secret(
        self,
        name: str,
        *,
        scope: SecretScope,
        session_id: str | None = None,
        consumer: str | None = None,
        allow_session: bool = True,
        allow_env: bool = False,
    ) -> ResolvedSecret | None:
        validated = validate_secret_name(name)
        target_scope = validate_secret_scope(scope)
        with self._lock:
            if session_id:
                sess_rec = self._session_entries.get(session_id, {}).get(validated)
                if sess_rec is not None:
                    if not allow_session:
                        raise SecretAccessError(
                            f"Session credential {validated} cannot be used by a persistent global consumer"
                        )
                    if sess_rec.scope != target_scope:
                        raise SecretAccessError(
                            f"Credential {validated} has scope '{sess_rec.scope}' and cannot be used in '{target_scope}'"
                        )
                    if not _consumer_matches(sess_rec.consumers, consumer, target_scope):
                        raise SecretAccessError(
                            f"Credential {validated} is not authorized for consumer {consumer!r}"
                        )
                    return ResolvedSecret(
                        name=validated,
                        value=sess_rec.value,
                        scope=sess_rec.scope,
                        storage="session",
                        consumers=sess_rec.consumers,
                    )
            if self.entries_dir.is_dir():
                with self._file_lock():
                    pers_rec = self._read_persistent_locked(validated)
                if pers_rec is not None:
                    if pers_rec.scope != target_scope:
                        raise SecretAccessError(
                            f"Credential {validated} has scope '{pers_rec.scope}' and cannot be used in '{target_scope}'"
                        )
                    if not _consumer_matches(pers_rec.consumers, consumer, target_scope):
                        raise SecretAccessError(
                            f"Credential {validated} is not authorized for consumer {consumer!r}"
                        )
                    return ResolvedSecret(
                        name=validated,
                        value=pers_rec.value,
                        scope=pers_rec.scope,
                        storage="persistent",
                        consumers=pers_rec.consumers,
                    )
            if allow_env:
                env_val = self._environ.get(validated)
                if env_val:
                    return ResolvedSecret(
                        name=validated,
                        value=env_val,
                        scope=target_scope,
                        storage="environment",
                    )
        return None

    def sandbox_env_for_session(self, session_id: str | None) -> dict[str, str]:
        """Return all authorized ``sandbox``-scoped managed credentials for ``session_id``."""
        merged: dict[str, str] = {}
        with self._lock:
            if self.entries_dir.is_dir():
                with self._file_lock():
                    fernet = load_or_create_fernet(self.directory)
                    for path in sorted(self.entries_dir.glob("*.enc")):
                        name = path.stem
                        if not valid_secret_name(name):
                            continue
                        rec = self._read_persistent_locked(name, fernet)
                        if rec is not None and rec.scope == "sandbox" and _consumer_matches(
                            rec.consumers, "sandbox", "sandbox"
                        ):
                            merged[rec.name] = rec.value
            if session_id:
                for name, rec in sorted(self._session_entries.get(session_id, {}).items()):
                    if rec.scope == "sandbox" and _consumer_matches(rec.consumers, "sandbox", "sandbox"):
                        merged[name] = rec.value
        return merged

    def delete_secret(
        self,
        name: str,
        *,
        storage: ManagedStorage | None = None,
        session_id: str | None = None,
    ) -> bool:
        validated = validate_secret_name(name)
        removed = False
        with self._lock:
            if storage in (None, "session") and session_id:
                bucket = self._session_entries.get(session_id)
                if bucket is not None:
                    old = bucket.pop(validated, None)
                    if old is not None:
                        self._retire_locked(validated, old.value)
                        removed = True
                    if not bucket:
                        self._session_entries.pop(session_id, None)
            if storage in (None, "persistent") and self.entries_dir.is_dir():
                with self._file_lock():
                    path = self._entry_path(validated)
                    if path.exists():
                        try:
                            old = self._read_persistent_locked(validated)
                            if old is not None:
                                self._retire_locked(validated, old.value)
                        except OSError:
                            pass
                        path.unlink(missing_ok=True)
                        removed = True
        return removed

    def list_secrets(self, *, session_id: str | None = None) -> list[SecretMetadata]:
        items: list[SecretMetadata] = []
        with self._lock:
            if session_id:
                for name in sorted(self._session_entries.get(session_id, {})):
                    items.append(self._session_entries[session_id][name].metadata())
            if self.entries_dir.is_dir():
                with self._file_lock():
                    fernet = load_or_create_fernet(self.directory)
                    for path in sorted(self.entries_dir.glob("*.enc")):
                        name = path.stem
                        if not valid_secret_name(name):
                            continue
                        rec = self._read_persistent_locked(name, fernet)
                        if rec is not None:
                            items.append(rec.metadata())
        return items

    def clear_session(self, session_id: str) -> int:
        if not session_id:
            return 0
        with self._lock:
            bucket = self._session_entries.pop(session_id, None)
            if not bucket:
                return 0
            for rec in bucket.values():
                self._retire_locked(rec.name, rec.value)
            return len(bucket)

    def clear_all_sessions(self) -> None:
        with self._lock:
            for bucket in self._session_entries.values():
                for rec in bucket.values():
                    self._retire_locked(rec.name, rec.value)
            self._session_entries.clear()

    def redaction_refs(
        self,
        *,
        session_id: str | None = None,
        min_length: int = 1,
    ) -> tuple[SecretRef, ...]:
        """Return ``SecretRef`` placeholders for active and in-process retired credentials."""
        refs: list[SecretRef] = []
        floor = max(1, min_length)
        with self._lock:
            if session_id:
                for rec in self._session_entries.get(session_id, {}).values():
                    if len(rec.value) >= floor:
                        refs.append(SecretRef(name=rec.name, value=rec.value, raw=False))
            for sid, bucket in self._session_entries.items():
                if sid == session_id:
                    continue
                for rec in bucket.values():
                    if len(rec.value) >= floor:
                        refs.append(SecretRef(name=rec.name, value=rec.value, raw=False))
            if self.entries_dir.is_dir():
                try:
                    with self._file_lock():
                        fernet = load_or_create_fernet(self.directory)
                        for path in sorted(self.entries_dir.glob("*.enc")):
                            name = path.stem
                            if not valid_secret_name(name):
                                continue
                            rec = self._read_persistent_locked(name, fernet)
                            if rec is not None and len(rec.value) >= floor:
                                refs.append(SecretRef(name=rec.name, value=rec.value, raw=False))
                except OSError:
                    pass
            for retired in self._retired_refs:
                if len(retired.value) >= floor:
                    refs.append(retired)
        return tuple(refs)
