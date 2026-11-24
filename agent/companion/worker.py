"""Private JSON-lines ReAct observer. stdout belongs exclusively to IPC."""

import asyncio
import json
from pathlib import Path
import os
import sys
from typing import Annotated, Literal

import httpx
from openai import APIConnectionError, APITimeoutError

from langchain_core.messages import SystemMessage
from langchain_core.tools import tool
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent
from pydantic import Field

from agent.companion.config import CompanionConfig
from agent.companion.session_inspector import diagnostic_error, format_inspect_response
from agent.models.client import build_chat_model
from agent.models.messages import StreamDeltaCallback, visible_text
from agent.models.profile import ModelProfile
from agent.models.stream_lifecycle import ModelStreamInterrupted


def _system_message(prompt, environment):
    return SystemMessage(content=f"{prompt}\n\n{environment}" if environment else prompt)


def create_chat(model, query, *, persona=None, instructions=None):
    @tool
    async def search_workspace_text(
        term: Annotated[str, Field(description="Non-empty literal text to find, such as a function name or error fragment.")],
        directory: Annotated[str, Field(description="Workspace-relative directory. Empty starts at the workspace root.")] = "",
    ) -> str:
        """Search readable workspace source for a literal, case-insensitive match. Returns at most 12 paths, line numbers, and excerpts. An empty result covers only the scanned range."""
        return await query("search", term=term, directory=directory)

    @tool
    async def read_workspace_file(
        path: Annotated[str, Field(description="Workspace-relative source path, usually taken from search or a file list.")],
        start: Annotated[int, Field(description="Starting line number, from 1. Defaults to the first line.")] = 1,
        limit: Annotated[int | None, Field(description="Lines to read. Defaults to 50, limited by host configuration and the character budget.")] = None,
    ) -> str:
        """Read a slice of an accessible source file, with path and line numbers. To continue, pass the returned next line as start."""
        return await query("read", path=path, start=start, limit=limit)

    @tool
    async def list_workspace_files(
        term: Annotated[str, Field(description="Literal fragment of a file name or relative path. Empty lists scanned files.")] = "",
        directory: Annotated[str, Field(description="Workspace-relative directory. Empty starts at the workspace root.")] = "",
    ) -> str:
        """List readable workspace source paths matching a literal fragment, case-insensitive, at most 12 relative paths. An empty result covers only the scanned range."""
        return await query("files", term=term, directory=directory)

    @tool
    async def session_evidence(
        kind: Annotated[
            Literal["statistics", "failures", "retries", "sequence", "timeline", "git", "turns"],
            Field(description=(
                "statistics: session and current-model tool counts; "
                "failures: recent failures and category changes; retries: possible retries with matching arguments; "
                "sequence: call order; timeline: historical events; git: historical Git samples; turns: recent turn reviews."
            )),
        ],
        start: Annotated[
            float | None, Field(description="Start Unix timestamp in seconds. Used only by timeline and git.")
        ] = None,
        end: Annotated[
            float | None, Field(description="End Unix timestamp in seconds. Used only by timeline and git. Defaults to now.")
        ] = None,
        tool_name: Annotated[str | None, Field(description="Filter by tool name. Git samples do not include tool calls.")] = None,
        turn_id: Annotated[str | None, Field(description="Filter by a turn id returned in a previous result.")] = None,
        limit: Annotated[int | None, Field(description="Number of rows, from 1 to 20.")] = None,
        event_type: Annotated[
            Literal["tool", "incident", "turn", "run", "analysis", "git"] | None,
            Field(description="timeline only. Filter by event type. Defaults to tool, incident, and turn."),
        ] = None,
    ) -> str:
        """Read a bounded view of the current session. timeline and git cover at most the last 14 days, newest first. Other views ignore the time range."""
        return await query(kind, start=start, end=end, tool_name=tool_name, turn_id=turn_id,
                           event_type=event_type, limit=limit)

    @tool
    async def inspect_current_session(
        view: Annotated[
            Literal["recent_tools", "tool_detail", "recent_messages"],
            Field(description=(
                "recent_tools lists recent tool results; "
                "tool_detail shows arguments, output, and exit code for one tool_call_id; "
                "recent_messages shows recent user and main-agent messages."
            )),
        ] = "recent_tools",
        tool_call_id: Annotated[
            str | None, Field(description="Tool call id required by tool_detail.")
        ] = None,
        limit: Annotated[
            int, Field(description="Number of rows, from 1 to 20. Defaults to 10.")
        ] = 10,
        run_id: Annotated[
            str | None, Field(description="Run id that distinguishes same-named calls in tool_detail.")
        ] = None,
        status: Annotated[
            Literal["running", "waiting", "success", "failed", "cancelled", "unknown"] | None,
            Field(description="Filter recent_tools by execution status."),
        ] = None,
        order: Annotated[
            Literal["asc", "desc"], Field(description="Order recent calls by start time.")
        ] = "asc",
    ) -> str:
        """Read the main agent's current session. The host binds the session. Another session or database cannot be selected."""
        return await query(
            "inspect_current_session",
            view=view,
            tool_call_id=tool_call_id,
            limit=min(max(int(limit or 10), 1), 20),
            run_id=run_id,
            status=status,
            order=order,
        )

    defaults = CompanionConfig()
    if persona is None:
        persona = defaults.persona_prompt.read_text(encoding="utf-8")
    if instructions is None:
        instructions = defaults.chat_prompt.read_text(encoding="utf-8")
    prompt = "\n\n".join((persona, instructions))
    graph = create_react_agent(
        model,
        [
            search_workspace_text,
            read_workspace_file,
            list_workspace_files,
            session_evidence,
            inspect_current_session,
        ],
    )

    async def respond(data, *, on_chunk=None):
        context = dict(data)
        question = context.pop("question")
        environment = context.pop("execute_environment", "")
        content = (
            f"当前会话资料（供参考）\n{json.dumps(context, ensure_ascii=False)}"
            f"\n\n用户当前问题（question）\n{question}"
        )
        # create_react_agent burns ~2 steps per tool round; 20 often hits
        # "Sorry, need more steps…" on inspect-heavy questions.
        config = {"recursion_limit": 60}
        if on_chunk is not None:
            published = ""

            def publish(text):
                nonlocal published
                text = text[:1600]
                if text != published:
                    published = text
                    on_chunk(text)

            callback = StreamDeltaCallback(
                lambda *_: None, on_assistant=publish,
            )
            callback.run_inline = True
            config["callbacks"] = [callback]
        result = await graph.ainvoke(
            {"messages": [_system_message(prompt, environment), ("user", content)]}, config,
        )
        return {"message": visible_text(result["messages"][-1])[:1600]}

    return respond


async def serve(reader, send, *, build_chat, config):
    async def receive():
        line = await reader.readline()
        if not line:
            return None
        message = json.loads(line)
        if not isinstance(message, dict):
            raise ValueError("Expected a JSON object")
        return message

    lock = asyncio.Lock()
    identity = {}
    query_seq = 0

    async def query(kind, **args):
        nonlocal query_seq
        # ReAct can issue tools in parallel; only one request reads from the pipe.
        async with lock:
            query_seq += 1
            query_id = query_seq
            try:
                async with asyncio.timeout(config.rpc_timeout_seconds + 0.5):
                    send(dict(**identity, query=kind, args=args, query_id=query_id))
                    while True:
                        reply = await receive()
                        if reply is None:
                            raise ConnectionError("Host disconnected")
                        if reply.get("query_id") != query_id:
                            continue
                        if any(reply.get(key) != value for key, value in identity.items()):
                            return format_inspect_response(diagnostic_error("request_expired", "The active request changed."))
                        if not isinstance(reply.get("data"), str):
                            raise ValueError("Invalid query reply")
                        return reply["data"]
            except TimeoutError:
                return format_inspect_response(diagnostic_error("tool_timeout", "Host did not return a tool reply in time."))
            except (OSError, ValueError):
                return format_inspect_response(diagnostic_error("ipc_error", "Host query transport failed."))

    chat = build_chat(query)
    send({"status": "ready"})
    while (request := await receive()) is not None:
        if "snapshot" not in request:
            # A reply can arrive after its tool timed out.
            continue
        identity = {k: request[k] for k in ("session", "seq", "token")}
        result = identity.copy()
        snapshot = request["snapshot"]

        def publish(text):
            send(dict(**identity, partial=text))

        timeout = asyncio.timeout(config.question_timeout_seconds)
        try:
            async with timeout:
                if "question" not in snapshot:
                    result["status"] = "skipped"
                    send(result)
                    continue
                response = await chat(snapshot, on_chunk=publish)
            result.update(response, status="ok")
        except (APITimeoutError, httpx.TimeoutException, TimeoutError):
            result["status"] = "request_timeout" if timeout.expired() else "model_timeout"
        except ModelStreamInterrupted as exc:
            result["status"] = (
                "model_timeout" if isinstance(exc.__cause__, (APITimeoutError, httpx.TimeoutException, TimeoutError))
                else "model_connection_error"
            )
        except (APIConnectionError, httpx.TransportError):
            result["status"] = "model_connection_error"
        except GraphRecursionError:
            result["status"] = "recursion_limit"
        except Exception:  # noqa: BLE001 - details can contain HTTP credentials
            result["status"] = "model_error"
        send(result)


async def main():
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        print('{"status":"no key"}', flush=True)
        return
    defaults = CompanionConfig()
    paths = tuple(map(Path, sys.argv[2:4])) or (
        defaults.persona_prompt, defaults.chat_prompt,
    )
    prompts = []
    for path in paths:
        try:
            prompts.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            print(json.dumps({"status": "prompt error", "path": str(path)}), flush=True)
            return
    persona, chat_instructions = prompts
    model = build_chat_model(
        ModelProfile(
            id="deepseek/deepseek-flash",
            model="deepseek-flash",
            api="chat_completions",
            base_url="https://api.deepseek.com/v1",
            api_key=key,
            max_retries=0,
        ),
        streaming=True,
        timeout=15,
    )
    try:
        config = defaults if len(sys.argv) < 6 else CompanionConfig(
            rpc_timeout_seconds=float(sys.argv[4]),
            question_timeout_seconds=float(sys.argv[5]),
        )
        reader = asyncio.StreamReader(limit=65536)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer,
        )
        try:
            await serve(
                reader, lambda message: print(json.dumps(message), flush=True),
                build_chat=lambda query: create_chat(model, query, persona=persona, instructions=chat_instructions),
                config=config,
            )
        finally:
            transport.close()
    finally:
        model.http_client.close()
        await model.http_async_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
