"""Real HTTP test fixture, never shipped as a DeepAgent MCP server."""
import asyncio
import sys

from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent, ImageContent, EmbeddedResource, BlobResourceContents

server = FastMCP("deep-agent-test")


@server.tool
def remote_add(a: int, b: int) -> dict:
    """Add two integers on the remote server."""
    print("REMOTE_CALL remote_add", flush=True)
    return {"sum": a + b}


@server.tool
def rich_result() -> ToolResult:
    """Return text, image, file and structured content."""
    return ToolResult(content=[
        TextContent(type="text", text="rich output"),
        ImageContent(type="image", data="aGVsbG8=", mime_type="image/png"),
        EmbeddedResource(type="resource", resource=BlobResourceContents(
            uri="file:///test.txt", mime_type="text/plain", blob="aGVsbG8=")),
    ], structured_content={"value": [1, 2]})


@server.tool
def fail_tool() -> None:
    """Report a remote execution error."""
    raise ValueError("remote failure")


@server.tool
async def slow_tool(delay: float = 10) -> str:
    """A cancellable slow request."""
    await asyncio.sleep(delay)
    return "finished"


if __name__ == "__main__":
    server.run(transport=sys.argv[1], host="127.0.0.1", port=int(sys.argv[2]), show_banner=False)
