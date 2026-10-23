"""Shared proxy and certificate environment policy for child processes."""
from __future__ import annotations

from typing import Mapping

HTTP_ENV_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS", "NODE_USE_ENV_PROXY",
)


def apply_http_environment(
    env: Mapping[str, str], source: Mapping[str, str], *, trust_env: bool,
) -> dict[str, str]:
    """Inherit networking settings only when enabled; keep explicit overrides."""
    result = dict(env)
    if trust_env:
        for name in HTTP_ENV_VARS:
            if name in source:
                result.setdefault(name, source[name])
        # Opt in to env proxies for Node versions that support this switch.
        result.setdefault("NODE_USE_ENV_PROXY", "1")
    else:
        for name in HTTP_ENV_VARS:
            result.pop(name, None)
    return result
