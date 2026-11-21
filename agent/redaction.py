"""Exact replacement of credentials explicitly supplied by the caller."""

from collections.abc import Iterable


REDACTED = "[REDACTED]"


def replace_known_secrets(text: str, secrets: Iterable[str]) -> str:
    unique = dict.fromkeys(secret for secret in secrets if secret)
    for secret in sorted(unique, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text
