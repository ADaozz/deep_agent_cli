"""Only session-owned IDs, never user supplied filesystem paths."""
from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agent.subagents.log_store import SubAgentLogStore


def build_subagent_log_tool(store: SubAgentLogStore):
    @tool
    def read_subagent_log(runtime: ToolRuntime, run_id: str | None = None,
                          offset: int = 0, limit: int = 100) -> dict:
        """List this session's delegated run IDs, or read a bounded page of one run's log."""
        parent = str(runtime.config.get("configurable", {}).get("thread_id", ""))
        if not parent:
            return {"error": "Parent session ID unavailable"}
        try:
            return store.read(parent, run_id, offset, limit)
        except (ValueError, OSError) as exc:
            return {"error": str(exc)}
    return read_subagent_log
