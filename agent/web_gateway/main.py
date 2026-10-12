"""python -m agent.web_gateway.main; local single-user gateway."""
import argparse
from agent.web_gateway.config import GatewayConfig, LOOPBACK


def main():
    parser = argparse.ArgumentParser(description="Local DeepAgentCLI Web gateway")
    parser.add_argument("--host", default="127.0.0.1", choices=sorted(LOOPBACK))
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    try:
        import uvicorn
        from agent.web_gateway.app import create_app
        config = GatewayConfig.from_env(args.port)
    except ImportError:
        parser.exit(2, 'Install optional HTTP dependencies: pip install "deep-agent-cli[server]"\n')
    except ValueError as exc:
        parser.exit(2, f"Gateway configuration error: {exc}\n")
    uvicorn.run(create_app(config), host=args.host, port=args.port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
