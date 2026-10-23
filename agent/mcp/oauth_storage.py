"""Encrypted OAuth credentials scoped to the MCP configuration directory."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

from cryptography.fernet import Fernet
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper


def oauth_token_storage(directory: Path) -> FernetEncryptionWrapper:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    key_path = directory / "key"
    # Serialize first use across CLI processes; never replace an existing key.
    fd = os.open(key_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        os.fchmod(stream.fileno(), 0o600)
        key = stream.read()
        if not key:
            key = Fernet.generate_key()
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        fernet = Fernet(key)
    tokens = directory / "tokens"
    tokens.mkdir(mode=0o700, exist_ok=True)
    return FernetEncryptionWrapper(FileTreeStore(
        data_directory=tokens,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(tokens),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(tokens),
    ), fernet=fernet)
