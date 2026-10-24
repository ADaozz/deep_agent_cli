"""Local OpenAI-compatible SSE endpoint with scripted faults and cancellation stalls.

The server speaks both Chat Completions and Responses streaming over a real
socket. Each request consumes one ``Turn``. A turn may stall before its first
chunk, stall after some chunks, or stream forever. A silent stall sends nothing.
``keepalive_interval`` emits SSE comment lines during the stall so a chunk
timer can be distinguished from a byte-level read timeout. The handler still
watches the socket and records when the client closes the connection.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import itertools
import json
import select
import socket
import threading
import time
from typing import Any


@dataclass
class Turn:
    """One scripted model response."""

    text: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # "finish": send every chunk and end. "hang": send ``chunks_before_hang``
    # content chunks then stay silent. "forever": keep sending deltas.
    # "disconnect" / "transport_disconnect": EOF / truncated HTTP body after
    # chunks_before_hang SSE events. "disconnect_tool": EOF after partial args.
    # "hang_tool": partial tool arguments, then the same stall as "hang".
    mode: str = "finish"
    chunks_before_hang: int = 0
    status: int = 200
    retry_after: str = "0.001"
    interval: float = 0.0
    gap_before_text: float = 0.0
    keepalive_interval: float = 0.0
    usage: dict[str, int] | None = field(
        default_factory=lambda: {"input_tokens": 21, "output_tokens": 7, "total_tokens": 28}
    )


@dataclass
class Connection:
    path: str
    body: dict[str, Any]
    opened_at: float
    chunks_sent: int = 0
    closed_by_client_at: float | None = None
    finished_at: float | None = None
    closed: threading.Event = field(default_factory=threading.Event)
    first_chunk_sent: threading.Event = field(default_factory=threading.Event)
    hanging: threading.Event = field(default_factory=threading.Event)
    keepalives_sent: int = 0


def _chat_payload(model: str, delta: dict[str, Any] | None = None, *, finish: str | None = None, rid: str = "local",
                  usage: dict[str, int] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": f"chatcmpl-{rid}", "object": "chat.completion.chunk", "created": 0, "model": model,
        "choices": [] if delta is None and finish is None else [
            {"index": 0, "delta": delta or {}, "finish_reason": finish},
        ],
    }
    if usage is not None:
        payload["usage"] = {
            "prompt_tokens": usage["input_tokens"],
            "completion_tokens": usage["output_tokens"],
            "total_tokens": usage["total_tokens"],
        }
    return payload


class ScriptedSSEServer:
    def __init__(self, api: str, turns: list[Turn] | None = None) -> None:
        if api not in {"chat_completions", "responses"}:
            raise ValueError(api)
        self.api = api
        self._turns: deque[Turn] = deque(turns or [])
        self._lock = threading.Lock()
        self.connections: list[Connection] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._request_seen = threading.Condition(self._lock)
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                server._serve(self, body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="sse-test-server", daemon=True)

    # -- lifecycle -----------------------------------------------------------------
    def __enter__(self) -> "ScriptedSSEServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._stop.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(5)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}/v1"

    def add(self, *turns: Turn) -> None:
        with self._lock:
            self._turns.extend(turns)

    def replace_turns(self, *turns: Turn) -> None:
        """Drop unconsumed turns, e.g. those a cancelled run never requested."""
        with self._lock:
            self._turns.clear()
            self._turns.extend(turns)

    # -- observation ----------------------------------------------------------------
    def wait_for_connection(self, index: int, timeout: float = 5.0) -> Connection:
        deadline = time.monotonic() + timeout
        with self._request_seen:
            while len(self.connections) <= index:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"model request #{index} never arrived")
                self._request_seen.wait(remaining)
            return self.connections[index]

    def open_connections(self) -> list[Connection]:
        with self._lock:
            return [item for item in self.connections if not item.closed.is_set()]

    # -- serving --------------------------------------------------------------------
    def _serve(self, handler: BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
        expected = "/v1/chat/completions" if self.api == "chat_completions" else "/v1/responses"
        connection = Connection(handler.path, body, time.monotonic())
        with self._request_seen:
            number = len(self.connections)
            self.connections.append(connection)
            turn = self._turns.popleft() if self._turns else None
            self._request_seen.notify_all()
        if handler.path != expected or turn is None:
            self.errors.append(f"unexpected request {handler.path} (turn available: {turn is not None})")
            handler.send_error(500, "Unexpected model request")
            connection.closed.set()
            return
        if turn.status != 200:
            handler.send_response(turn.status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Retry-After", turn.retry_after)
            handler.end_headers()
            handler.wfile.write(b'{"error":{"message":"scripted request failure","type":"server_error"}}')
            connection.closed.set()
            return
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        if turn.mode == "transport_disconnect":
            handler.send_header("Content-Length", "999999")
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        handler.wfile.flush()
        sock = handler.connection
        try:
            # Real endpoints return a new id per response; LangChain uses it as
            # the message id, and equal ids would replace earlier answers.
            for index, event in enumerate(self._events(turn, body.get("model", "local"), f"local-{number}")):
                if turn.mode in {"disconnect", "transport_disconnect"} and index >= turn.chunks_before_hang:
                    # A clean TCP EOF without the required SSE completion event.
                    handler.close_connection = True
                    connection.closed.set()
                    return
                if event is _HANG:
                    connection.hanging.set()
                    self._watch_until_closed(
                        sock, connection, keepalive=turn.keepalive_interval, writer=handler.wfile,
                    )
                    return
                if isinstance(event, _Gap):
                    if self._sleep_or_closed(sock, connection, event.seconds):
                        return
                    continue
                handler.wfile.write(event)
                handler.wfile.flush()
                connection.chunks_sent += 1
                connection.first_chunk_sent.set()
                if turn.interval and self._sleep_or_closed(sock, connection, turn.interval):
                    return
            connection.finished_at = time.monotonic()
        except (BrokenPipeError, ConnectionResetError):
            self._mark_closed(connection)
            return
        connection.closed.set()

    def _mark_closed(self, connection: Connection) -> None:
        if connection.closed_by_client_at is None:
            connection.closed_by_client_at = time.monotonic()
        connection.closed.set()

    def _client_closed(self, sock: socket.socket) -> bool:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return False
        try:
            return sock.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def _sleep_or_closed(self, sock: socket.socket, connection: Connection, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return True
            readable, _, _ = select.select([sock], [], [], min(0.02, max(0.0, deadline - time.monotonic())))
            if readable and self._client_closed(sock):
                self._mark_closed(connection)
                return True
        return False

    def _watch_until_closed(
        self, sock: socket.socket, connection: Connection, *,
        keepalive: float = 0.0, writer: Any = None,
    ) -> None:
        next_keepalive = time.monotonic() + keepalive if keepalive > 0 and writer is not None else None
        while not self._stop.is_set():
            timeout = 0.02
            if next_keepalive is not None:
                timeout = min(timeout, max(0.0, next_keepalive - time.monotonic()))
            readable, _, _ = select.select([sock], [], [], timeout)
            if readable and self._client_closed(sock):
                self._mark_closed(connection)
                return
            if next_keepalive is not None and time.monotonic() >= next_keepalive:
                try:
                    writer.write(b": keepalive\n\n")
                    writer.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self._mark_closed(connection)
                    return
                connection.keepalives_sent += 1
                next_keepalive = time.monotonic() + keepalive
        connection.closed.set()

    def _events(self, turn: Turn, model: str, rid: str):
        if self.api == "chat_completions":
            yield from self._chat_events(turn, model, rid)
        else:
            yield from self._responses_events(turn, rid)

    def _deltas(self, turn: Turn):
        """(kind, text) deltas; "forever" mode never ends."""
        if turn.reasoning:
            yield "reasoning", turn.reasoning
        if turn.gap_before_text:
            yield "gap", turn.gap_before_text
        if turn.mode == "forever":
            for index in itertools.count():
                yield "text", f"tok{index} "
        sent = 0
        pieces = turn.text.split(" ") if turn.text else []
        for index, piece in enumerate(pieces):
            if turn.mode == "hang" and sent >= turn.chunks_before_hang:
                yield "hang", ""
                return
            yield "text", piece + (" " if index < len(pieces) - 1 else "")
            sent += 1
        if turn.mode == "hang":
            yield "hang", ""

    def _chat_events(self, turn: Turn, model: str, rid: str):
        def data(payload: dict[str, Any]) -> bytes:
            return f"data: {json.dumps(payload)}\n\n".encode()
        first = True
        for kind, text in self._deltas(turn):
            if kind == "hang":
                yield _HANG
                return
            if kind == "gap":
                yield _Gap(float(text))
                continue
            delta: dict[str, Any] = {"role": "assistant"} if first else {}
            first = False
            delta["reasoning_content" if kind == "reasoning" else "content"] = text
            yield data(_chat_payload(model, delta, rid=rid))
        for index, call in enumerate(turn.tool_calls):
            arguments = json.dumps(call["args"])
            if turn.mode in {"disconnect_tool", "hang_tool"}:
                arguments = arguments[:5]
            delta = {"tool_calls": [{"index": index, "id": call["id"], "type": "function", "function": {
                "name": call["name"], "arguments": arguments}}]}
            if first:
                delta["role"] = "assistant"
                first = False
            yield data(_chat_payload(model, delta, rid=rid))
            if turn.mode == "disconnect_tool":
                return
            if turn.mode == "hang_tool":
                yield _HANG
                return
        yield data(_chat_payload(model, {}, finish="tool_calls" if turn.tool_calls else "stop", rid=rid))
        if turn.usage is not None:
            yield data(_chat_payload(model, usage=turn.usage, rid=rid))
        yield b"data: [DONE]\n\n"

    def _responses_events(self, turn: Turn, rid: str):
        sequence = itertools.count()
        def data(payload: dict[str, Any]) -> bytes:
            payload = {**payload, "sequence_number": next(sequence)}
            return f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n".encode()
        yield data({"type": "response.created", "response": _response_body("in_progress", None, rid)})
        for kind, text in self._deltas(turn):
            if kind == "hang":
                yield _HANG
                return
            if kind == "gap":
                yield _Gap(float(text))
                continue
            if kind == "reasoning":
                yield data({"type": "response.reasoning_text.delta", "output_index": 0,
                            "item_id": f"rs_{rid}", "content_index": 0, "delta": text})
            else:
                yield data({"type": "response.output_text.delta", "output_index": 1,
                            "content_index": 0, "item_id": f"msg_{rid}", "delta": text})
        for offset, call in enumerate(turn.tool_calls):
            index = 2 + offset
            item = {"type": "function_call", "id": f"fc_{call['id']}", "call_id": call["id"],
                    "name": call["name"], "arguments": "", "status": "in_progress"}
            yield data({"type": "response.output_item.added", "output_index": index, "item": item})
            yield data({"type": "response.function_call_arguments.delta", "output_index": index,
                        "item_id": item["id"], "delta": (json.dumps(call["args"])[:5]
                            if turn.mode in {"disconnect_tool", "hang_tool"} else json.dumps(call["args"]))})
            if turn.mode == "disconnect_tool":
                return
            if turn.mode == "hang_tool":
                yield _HANG
                return
        yield data({"type": "response.completed", "response": _response_body("completed", turn.usage, rid)})


def _response_body(status: str, usage: dict[str, int] | None, rid: str) -> dict[str, Any]:
    return {"id": f"resp_{rid}", "object": "response", "created_at": 0, "status": status,
            "model": "local", "output": [], "usage": usage}


class _Gap:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds


_HANG = object()
