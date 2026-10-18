"""Shared package version for CLI output and terminal presentation."""
from importlib.metadata import PackageNotFoundError, version


def package_version() -> str:
    try:
        return version("deep-agent-cli")
    except PackageNotFoundError:
        return "unknown (source checkout; install the package to read its version)"
