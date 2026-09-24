"""Local execute tool with optional NETWORK capability declaration."""
from __future__ import annotations

from deepagents.backends.protocol import SandboxBackendProtocol
from langchain_core.tools import BaseTool, StructuredTool
from agent.network import reset_execute_network, set_execute_network


def build_execute_tool(backend: SandboxBackendProtocol) -> BaseTool:
    """Shell execute bound to a sandbox backend.

    ``network=True`` shares the host network namespace for this command.
    """

    def execute(
        command: str,
        timeout: int | None = None,
        network: bool = False,
    ) -> tuple[str, dict[str, object]]:
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
            if "[Output truncated: showing " not in output and marker not in output:
                output = f"{output.rstrip()}{marker}"
        artifact: dict[str, object] = {
            "exit_code": response.exit_code,
            "truncated": response.truncated,
            "host_log_path": getattr(response, "host_log_path", None),
            "agent_log_path": getattr(response, "agent_log_path", None),
            "log_error": getattr(response, "log_error", None),
            "termination_reason": getattr(response, "termination_reason", None),
            "max_output_bytes": getattr(response, "max_output_bytes", None),
        }
        return output, artifact

    return StructuredTool.from_function(
        func=execute,
        name="execute",
        response_format="content_and_artifact",
        description=(
            "Run a shell command inside the sandbox workspace (/workspace). "
            "Defaults to no network. Under permission ask every execute needs "
            "approval. network=true grants host network access, including localhost "
            "and LAN addresses, for this command. "
            "Prefer ls/read_file/glob/grep/write_file for filesystem work."
        ),
    )
