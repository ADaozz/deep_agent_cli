from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlsplit

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def local_url(value: str) -> str:
    url = urlsplit(value)
    if (url.scheme not in {"http", "https"} or url.hostname not in LOOPBACK
            or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}):
        raise ValueError("Gateway URLs must be loopback HTTP origins without credentials or paths")
    try:
        _ = url.port
    except ValueError:
        raise ValueError("Invalid gateway port") from None
    return f"{url.scheme}://{url.netloc.lower()}"


@dataclass(frozen=True)
class GatewayConfig:
    token: str = field(repr=False)
    upstream: str = "http://127.0.0.1:8080"
    origins: tuple[str, ...] = ("http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765", "http://127.0.0.1:5173", "http://localhost:5173")
    dist_dir: Path | None = None
    max_body_bytes: int = 1_048_576

    def __post_init__(self):
        if not self.token or not self.token.strip() or "\r" in self.token or "\n" in self.token:
            raise ValueError("Set a nonempty service token in the gateway environment")
        object.__setattr__(self, "upstream", local_url(self.upstream))
        object.__setattr__(self, "origins", tuple(local_url(origin) for origin in self.origins))
        if not self.origins or self.max_body_bytes < 1:
            raise ValueError("Gateway origins and body limit must be configured")
        if self.dist_dir is not None and (not self.dist_dir.is_dir() or not (self.dist_dir / "index.html").is_file()):
            raise ValueError("WEB_DIST_DIR must reference a web build directory containing index.html")

    @classmethod
    def from_env(cls, port: int = 8765):
        origins = os.environ.get("DEEP_AGENT_WEB_ORIGINS")
        dist = os.environ.get("WEB_DIST_DIR")
        return cls(
            token=os.environ.get("DEEP_AGENT_SERVICE_TOKEN", ""),
            upstream=os.environ.get("DEEP_AGENT_SERVICE_BASE_URL", "http://127.0.0.1:8080"),
            origins=tuple(part.strip() for part in origins.split(",")) if origins else (
                f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}",
                "http://127.0.0.1:5173", "http://localhost:5173",
            ),
            dist_dir=Path(dist).expanduser().resolve() if dist else None,
        )
