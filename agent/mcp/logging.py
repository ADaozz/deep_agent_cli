"""Private, bounded background logs that never write logging errors to the TUI."""
from __future__ import annotations

import os
from logging.handlers import RotatingFileHandler
from typing import Any


class MCPLogHandler(RotatingFileHandler):
    def _open(self) -> Any:
        fd = os.open(self.baseFilename, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding=self.encoding, errors=self.errors)

    def handleError(self, record: Any) -> None:
        # logging's default error handler prints a traceback to stderr.
        pass
