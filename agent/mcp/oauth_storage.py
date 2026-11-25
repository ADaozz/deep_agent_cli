"""Encrypted OAuth credentials scoped to the MCP configuration directory."""

from __future__ import annotations

from pathlib import Path
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from agent.secrets.store import load_or_create_fernet


def oauth_token_storage(directory: Path) -> FernetEncryptionWrapper:
    fernet = load_or_create_fernet(directory)
    tokens = directory / "tokens"
    tokens.mkdir(mode=0o700, exist_ok=True)
    return FernetEncryptionWrapper(FileTreeStore(
        data_directory=tokens,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(tokens),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(tokens),
    ), fernet=fernet)
