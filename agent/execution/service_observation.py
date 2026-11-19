"""Identify descendants of commands launched by this CLI, including detached jobs."""
import threading
import uuid

OWNER_VARIABLE = "DEEP_AGENT_EXECUTION_OWNER"
OWNER = uuid.uuid4().hex
_lock = threading.Lock()
_active: set[str] = set()


def begin_command() -> str:
    token = f"{OWNER}:{uuid.uuid4().hex}"
    with _lock:
        _active.add(token)
    return token


def end_command(token: str) -> None:
    with _lock:
        _active.discard(token)


def active_commands() -> set[str]:
    with _lock:
        return _active.copy()
