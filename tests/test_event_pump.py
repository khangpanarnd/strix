"""Tests for the TUI SDK-event pump (bounded, ordered, lossless drain)."""

from __future__ import annotations

import queue

from strix.interface.tui.event_pump import DEFAULT_MAX_DRAIN, drain_queue


def test_drain_empty_queue_returns_zero() -> None:
    q: queue.Queue[int] = queue.Queue()
    sink: list[int] = []
    assert drain_queue(q, sink.append) == 0
    assert sink == []


def test_drain_preserves_fifo_order() -> None:
    q: queue.Queue[int] = queue.Queue()
    for i in range(5):
        q.put(i)
    sink: list[int] = []
    processed = drain_queue(q, sink.append)
    assert processed == 5
    assert sink == [0, 1, 2, 3, 4]
    assert q.empty()


def test_drain_bounded_defers_remaining() -> None:
    q: queue.Queue[int] = queue.Queue()
    for i in range(10):
        q.put(i)
    sink: list[int] = []

    first = drain_queue(q, sink.append, max_items=4)
    assert first == 4
    assert sink == [0, 1, 2, 3]
    # Remaining items are NOT dropped — they stay for the next drain.
    assert q.qsize() == 6

    second = drain_queue(q, sink.append, max_items=4)
    assert second == 4
    assert sink == [0, 1, 2, 3, 4, 5, 6, 7]
    assert q.qsize() == 2


def test_drain_continues_after_sink_error() -> None:
    q: queue.Queue[int] = queue.Queue()
    for i in range(3):
        q.put(i)
    seen: list[int] = []

    def sink(item: int) -> None:
        if item == 1:
            raise ValueError("boom")
        seen.append(item)

    processed = drain_queue(q, sink)
    # All three are consumed (error on item 1 is swallowed), not left stuck.
    assert processed == 3
    assert seen == [0, 2]
    assert q.empty()


def test_default_max_drain_is_bounded() -> None:
    q: queue.Queue[int] = queue.Queue()
    for i in range(DEFAULT_MAX_DRAIN + 25):
        q.put(i)
    sink: list[int] = []
    processed = drain_queue(q, sink.append)
    assert processed == DEFAULT_MAX_DRAIN
    assert q.qsize() == 25
