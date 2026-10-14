"""Noninteractive single-process service entry point."""
from __future__ import annotations

import argparse
from pathlib import Path

from agent.server.config import ServerConfig


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="deep-agent serve", description="Run the internal HTTP agent service")
    parser.add_argument("--config", required=True, type=Path, help="Server YAML configuration")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8080, type=int)
    args = parser.parse_args(argv)
    try:
        import uvicorn
        from agent.server.app import create_app
    except ImportError:
        parser.exit(2, 'Install HTTP dependencies with: pip install "deep-agent-cli[server]"\n')
    try:
        config = ServerConfig.load(args.config)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Server configuration error: {exc}\n")
    print(f"MCP service startup profile: {config.mcp_snapshot.current}; configuration is fixed until restart")
    uvicorn.run(create_app(config), host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
