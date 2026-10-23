"""Use cached OAuth credentials first and limit repeated browser authorization."""
from __future__ import annotations

import time
from typing import Any

from fastmcp.client.auth import OAuth
from fastmcp.client.auth.oauth import ClientNotFoundError

from agent.mcp.config import MCPAuthError

_BROWSER_RETRY_INTERVAL = 60


class ManagedOAuth(OAuth):
    def __init__(
        self, *args: Any, server_name: str, allow_browser: bool,
        browser_attempts: dict[str, float] | None = None, **kwargs: Any,
    ) -> None:
        self.server_name = server_name
        self.allow_browser = allow_browser
        self.browser_attempts = browser_attempts if browser_attempts is not None else {}
        super().__init__(*args, **kwargs)

    def authorization_required(self, reason: str = "Browser authorization is disabled for this attempt.") -> MCPAuthError:
        return MCPAuthError(
            f"{reason} Run /mcp test {self.server_name} to authorize."
        )

    async def redirect_handler(self, authorization_url: str) -> None:
        now = time.monotonic()
        previous = self.browser_attempts.get(self.server_name)
        if not self.allow_browser:
            raise self.authorization_required()
        if previous is not None and now - previous < _BROWSER_RETRY_INTERVAL:
            raise self.authorization_required(
                f"Browser authorization was already attempted within the last {_BROWSER_RETRY_INTERVAL} seconds."
            )
        # Set before yielding: retries and replacement clients share this guard.
        self.browser_attempts[self.server_name] = now
        try:
            await super().redirect_handler(authorization_url)
        except ClientNotFoundError:
            # SDK preflight rejects a stale client before opening the browser.
            # Let its fresh-registration retry perform the actual authorization.
            self.browser_attempts.pop(self.server_name, None)
            raise
