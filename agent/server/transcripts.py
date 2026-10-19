"""Read saved CLI conversations without creating a Runner or executing its graph."""
from datetime import datetime, timezone
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.message import add_messages

from agent.session import _strict_serde, messages_to_transcript, settle_restored_tools
from agent.server.store import serializable

TEXT_LIMIT = 32000


class TranscriptRequestError(ValueError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class _ReadOnlySaver(SqliteSaver):
    def setup(self) -> None:
        # Existing checkpoint tables are opened mode=ro; never initialize or migrate them.
        pass


def _stamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _reduce_messages(state, writes):
    for value in writes:
        state = add_messages(state, value)
    return state


def _messages(saver, saved):
    value = saved.checkpoint['channel_values'].get('messages')
    channel = DeltaChannel(_reduce_messages, list)
    if value is not None:
        return channel.from_checkpoint(value).get()
    history = saver.get_delta_channel_history(config=saved.config, channels=['messages']).get('messages', {})
    restored = channel.from_checkpoint(history.get('seed', []))
    restored.replay_writes(history.get('writes', []))
    return restored.get()


def saved_transcript(path: Path, session_id: str, *, before: str | None = None,
                     limit: int = 50, cursor: str | None = None) -> dict:
    empty = {'blocks': [], 'next_cursor': None}
    if not 1 <= limit <= 100:
        raise TranscriptRequestError('历史数量必须在 1 到 100 之间', 422)
    checkpoint_id = None
    end = None
    if cursor:
        match = re.fullmatch(r'([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}):([0-9]{1,9})', cursor)
        if not match:
            raise TranscriptRequestError('无效的历史分页游标', 422)
        checkpoint_id, raw_end = match.groups()
        end = int(raw_end)
    if not path.is_file():
        return empty
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, check_same_thread=False)) as conn:
            conn.execute('BEGIN')
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoints'").fetchone():
                return empty
            saver = _ReadOnlySaver(conn, serde=_strict_serde())
            config = {'configurable': {'thread_id': session_id, 'checkpoint_ns': ''}}
            def at(identifier):
                return saver.get_tuple({'configurable': {**config['configurable'], 'checkpoint_id': identifier}})
            if checkpoint_id:
                saved = at(checkpoint_id)
                if not saved or (before and _stamp(saved.checkpoint['ts']) >= _stamp(before)):
                    raise TranscriptRequestError('历史分页游标已失效', 422)
            elif before:
                # Pin legacy history to the state before the first HTTP run, so
                # CLI turns remain visible without duplicating Web run messages.
                identifiers = [row[0] for row in conn.execute(
                    "SELECT checkpoint_id FROM checkpoints WHERE thread_id=? AND checkpoint_ns='' ORDER BY checkpoint_id",
                    (session_id,),
                )]
                low, high = 0, len(identifiers)
                boundary = _stamp(before)
                while low < high:
                    middle = (low + high) // 2
                    candidate = at(identifiers[middle])
                    if candidate and _stamp(candidate.checkpoint['ts']) < boundary:
                        low = middle + 1
                    else:
                        high = middle
                saved = at(identifiers[low - 1]) if low else None
            else:
                saved = saver.get_tuple(config)
            if not saved:
                return empty
            blocks = messages_to_transcript(_messages(saver, saved))
            settle_restored_tools(blocks)
            identifier = saved.checkpoint['id']
            end = len(blocks) if end is None else min(end, len(blocks))
            start = max(0, end - limit)
            result = []
            for index in range(start, end):
                block = blocks[index]
                arguments = json.dumps(serializable(block.arguments), ensure_ascii=False) if block.arguments else ''
                result.append({
                    'id': f'{identifier}:{index}', 'kind': block.kind,
                    'content': block.content[:TEXT_LIMIT], 'thinking': block.thinking[:TEXT_LIMIT],
                    'name': block.name, 'arguments': arguments[:TEXT_LIMIT], 'status': block.status,
                    'is_error': block.is_error, 'attachments': [ref.filename for ref in block.attachments],
                    'truncated': any(len(value) > TEXT_LIMIT for value in (block.content, block.thinking, arguments)),
                })
            return {'blocks': result, 'next_cursor': f'{identifier}:{start}' if start else None}
    except TranscriptRequestError:
        raise
    except Exception as exc:
        raise TranscriptRequestError('无法读取保存的会话历史', 503) from exc
