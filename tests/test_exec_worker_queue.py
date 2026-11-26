"""Worker stdin queue ordering: pending Ctrl+C must keep priority over normal writes."""

from __future__ import annotations

from agent.execution import worker


def test_partial_normal_write_requeues_behind_pending_interrupt() -> None:
    """EAGAIN after a partial write must not reinsert normal data ahead of Ctrl+C."""
    queue = worker._InputQueue("pty")
    queue.items.append(("write", "write-1", b"payload", 0))
    queue.queued_bytes = len(b"payload")

    # drain_input() pops the active write; Ctrl+C arrives while it is in flight.
    active = queue.items.popleft()
    kind, write_id, payload, already_written = active
    with worker._running_lock:
        worker._inputs[101] = queue
    try:
        worker._interrupt(101, "interrupt-1")
    finally:
        with worker._running_lock:
            worker._inputs.pop(101, None)
    assert [item[0] for item in queue.items] == ["interrupt"]

    # The active write delivers one byte, then hits EAGAIN and is re-queued.
    written = 1
    queue.queued_bytes = max(0, queue.queued_bytes - written)
    queue.requeue_partial(
        (kind, write_id, payload[written:], already_written + written),
    )

    assert [item[0] for item in queue.items] == ["interrupt", "write"]
    assert queue.items[0] == ("interrupt", "interrupt-1", b"\x03", 0)
    assert queue.items[1] == ("write", "write-1", b"ayload", 1)
    assert queue.queued_bytes == len(b"\x03") + len(b"ayload")

    # The single stdin writer drains VINTR first, then the remaining normal bytes.
    head = queue.items.popleft()
    assert head[0] == "interrupt" and head[2] == b"\x03"
    tail = queue.items.popleft()
    assert tail[0] == "write" and tail[2] == b"ayload"
    assert not queue.items


def test_requeue_keeps_normal_write_fifo_behind_pending_interrupt() -> None:
    """A partially written normal write returns before later normal writes, after Ctrl+C."""
    queue = worker._InputQueue("pty")
    queue.items.append(("write", "write-1", b"one", 0))
    queue.items.append(("write", "write-2", b"two", 0))
    queue.items.popleft()  # write-1 is the active 3-byte write.

    with worker._running_lock:
        worker._inputs[102] = queue
    try:
        worker._interrupt(102, "interrupt-1")
    finally:
        with worker._running_lock:
            worker._inputs.pop(102, None)
    queue.requeue_partial(("write", "write-1", b"ne", 1))

    assert [item[1] for item in queue.items] == [
        "interrupt-1", "write-1", "write-2",
    ]


def test_blocked_interrupt_requeues_at_front() -> None:
    """An interrupt that itself hits EAGAIN stays ahead of queued normal writes."""
    queue = worker._InputQueue("pty")
    queue.items.append(("write", "write-1", b"one", 0))

    queue.requeue_partial(("interrupt", "interrupt-1", b"\x03", 0))

    assert [item[1] for item in queue.items] == ["interrupt-1", "write-1"]
