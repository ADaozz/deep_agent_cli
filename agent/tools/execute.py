"""Local execute tool with optional NETWORK capability declaration."""
from __future__ import annotations

from deepagents.backends.protocol import SandboxBackendProtocol
from langchain_core.tools import BaseTool, StructuredTool
from agent.network import reset_execute_network, set_execute_network


def build_execute_tool(backend: SandboxBackendProtocol) -> BaseTool:
    """Shell execute bound to a sandbox backend.

    ``network=True`` declares the NETWORK capability. BubblewrapBackend reads
    the request-local setting when it starts the process.
    """

    def execute(
        command: str,
        timeout: int | None = None,
        network: bool = False,
    ) -> str:
        token = set_execute_network(network)
        try:
            if timeout is not None:
                response = backend.execute(command, timeout=timeout)
            else:
                response = backend.execute(command)
        finally:
            reset_execute_network(token)
        output = response.output or ""
        if response.exit_code not in (0, None):
            suffix = f"\n\nExit code: {response.exit_code}"
            if suffix.strip() not in output:
                output = f"{output.rstrip()}{suffix}"
        if response.truncated:
            marker = "\n\n[output truncated]"
            if marker not in output:
                output = f"{output.rstrip()}{marker}"
        return output

    return StructuredTool.from_function(
        func=execute,
        name="execute",
        description=(
            "Run a shell command inside the sandbox workspace (/workspace). "
            "Defaults to no network. Under permission ask every execute needs "
            "approval. Set network=true only when the command must reach the internet. "
            "Prefer ls/read_file/glob/grep/write_file for filesystem work."
        ),
    )
