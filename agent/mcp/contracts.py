"""MCP connection status and operation outcomes."""

from dataclasses import dataclass
from enum import StrEnum


class ServerStatus(StrEnum):
    DISABLED = "disabled"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    AUTH_REQUIRED = "auth_required"
    ERROR = "error"


@dataclass(frozen=True)
class MCPResult:
    ok: bool
    changed: bool
    message: str
    pending: bool = False
