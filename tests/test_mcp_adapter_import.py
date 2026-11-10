"""MCP startup notices stay scoped to the supported adapter import."""
import subprocess
import sys


def test_mcp_import_suppresses_only_its_known_beta_notice():
    code = '''
import warnings
from langchain_core._api import LangChainBetaWarning
warnings.simplefilter("error", LangChainBetaWarning)
import agent.mcp.adapter
for message in ("Another beta feature", "`langchain.mcp` is in beta. Later notice"):
    try:
        warnings.warn(message, LangChainBetaWarning)
    except LangChainBetaWarning:
        pass
    else:
        raise AssertionError("Beta warning filters leaked beyond the import")
'''
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "`langchain.mcp` is in beta" not in result.stderr


def test_cli_version_has_no_mcp_beta_notice():
    result = subprocess.run(
        [sys.executable, "-m", "agent.cli.main", "--version"],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("deep-agent ")
    assert "`langchain.mcp` is in beta" not in result.stderr
