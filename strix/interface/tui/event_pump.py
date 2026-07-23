"""Thread-safe SDK-event pump for the TUI.

The scan runs on a separate thread and produces SDK events at a high rate during
LLM streaming. Delivering each event to the Textual UI with a blocking
``App.call_from_thread`` per event blocks the scan thread and floods the UI
message pump, starving input handling. Instead the scan thread enqueues events
(non-blocking) and the UI loop drains them in bounded batches via
:func:`drain_queue`, keeping the UI responsive.

``drain_queue`` is a pure function (no Textual/UI dependency) so it can be unit
tested without a live terminal app.
"""

from __future__ import annotations

import logging
import queue
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Callable


logger = logging.getLogger(__name__)

# Default number of events ingested per UI tick. Bounds UI work per frame so a
# burst of events cannot monopolize the event loop.
DEFAULT_MAX_DRAIN = 100


def drain_queue[T](
    q: queue.Queue[T],
    sink: Callable[[T], None],
    *,
    max_items: int = DEFAULT_MAX_DRAIN,
) -> int:
    """Drain up to ``max_items`` items from ``q`` (FIFO) into ``sink``.

    Returns the number of items processed. Remaining items are left on the queue
    for the next call (never dropped). A ``sink`` error on one item is logged and
    does not stop the drain.
    """
    processed = 0
    for _ in range(max_items):
        try:
            item = q.get_nowait()
        except queue.Empty:
            break
        try:
            sink(item)
        except Exception:
            logger.exception("SDK event ingest failed for one item; continuing")
        processed += 1
    return processed
