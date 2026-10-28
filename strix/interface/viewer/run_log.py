"""Incremental reader for a run's ``strix.log`` (the "server log" in the viewer).

Every scan writes a DEBUG-level, timestamped log to ``<run>/strix.log`` (see
``strix.telemetry.logging``). The viewer streams it to the browser by polling
this reader with a byte-offset cursor:

* No ``after``: return the tail (the most recent lines) and the offset of the
  end of what was read.
* ``after=<offset>``: return only complete lines written since then.

The cursor is a plain byte offset into the file, so the server keeps no
per-client state: any number of tabs, a viewer restart, or a CLI-started scan all
work the same way. A half-written last line is never returned; the cursor stops
before it and the next poll gets it whole.

Each record is parsed from the log format::

    2026-10-26 13:20:13.123 INFO    <scan_id> <agent_id> strix.core.runner: message

Lines that do not match (a traceback's continuation lines) are returned as
``continuation`` entries so the client can attach them to the previous record.
The scan stamps records in the *scan process's* local time with no zone marker;
the viewer runs on the same machine, so they are read as local time and returned
as UTC. Messages are passed through the shared secret redactor before they leave
the process.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from strix.interface.viewer.redaction import make_redactor


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


LOG_FILE_NAME = "strix.log"

#: Most bytes returned by one call, so a long backlog is delivered in several polls.
MAX_CHUNK_BYTES = 512 * 1024
#: How far back the initial (tail) read looks.
TAIL_BYTES = 256 * 1024
#: Most records the initial tail returns.
TAIL_LINES = 500
#: A single message is cut here; LLM payloads can be enormous.
MAX_MESSAGE_CHARS = 4000

_LINE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2})\.(?P<ms>\d{3}) "
    r"(?P<level>[A-Z]+)\s+(?P<scan>\S+) (?P<agent>\S+) (?P<logger>[^\s:]+):(?: (?P<msg>.*))?$"
)


class InvalidCursorError(ValueError):
    """``after`` was not a non-negative integer."""


def parse_cursor(raw: str | None) -> int | None:
    """``None`` for no cursor (tail); a non-negative int otherwise, else raise."""
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise InvalidCursorError(raw) from exc
    if value < 0:
        raise InvalidCursorError(raw)
    return value


def _to_utc_iso(date: str, clock: str, millis: str) -> str | None:
    """Read a local-time log stamp and return it as ISO 8601 UTC, or None if it is not a date."""
    try:
        naive = datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
    except ValueError:
        return None
    # ``astimezone()`` on a naive datetime interprets it in the machine's local zone.
    utc = naive.replace(microsecond=int(millis) * 1000).astimezone(UTC)
    return utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clip(text: str) -> str:
    return text if len(text) <= MAX_MESSAGE_CHARS else text[:MAX_MESSAGE_CHARS] + " â€¦[truncated]"


def parse_line(line: str, redactor: Callable[[str], str] | None = None) -> dict[str, Any]:
    """One physical log line to an entry. Unparseable lines become continuations.

    ``redactor`` masks secrets in the message; it defaults to one built from the
    viewer's own environment.
    """
    mask = redactor or make_redactor()
    match = _LINE_RE.match(line)
    if match is None:
        return {"continuation": True, "message": mask(_clip(line))}
    agent = match["agent"]
    return {
        "ts": _to_utc_iso(match["date"], match["time"], match["ms"]),
        "level": match["level"],
        "agent": None if agent == "-" else agent,
        "logger": match["logger"],
        "message": mask(_clip(match["msg"] or "")),
    }


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _empty(
    *,
    exists: bool,
    offset: int = 0,
    size: int = 0,
    reset: bool = False,
    updated_at: str | None = None,
) -> dict[str, Any]:
    return {
        "exists": exists,
        "entries": [],
        "offset": offset,
        "size": size,
        "reset": reset,
        "more": False,
        "truncated_head": False,
        "updated_at": updated_at,
        "now": _utc_now_iso(),
    }


def read_log_chunk(
    run_dir: Path,
    after: int | None,
    redactor: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Read the next piece of ``strix.log`` for ``run_dir``.

    Returns ``{exists, entries, offset, size, reset, more, truncated_head,
    updated_at, now}``:

    * ``offset`` is the cursor to send as ``after`` next time.
    * ``reset`` is true when the file is now shorter than ``after`` (it was
      replaced); the read restarts from the beginning and the client should
      drop what it has.
    * ``more`` is true when the file has more unread complete data, so the
      client should poll again at once instead of waiting.
    * ``truncated_head`` is true on a tail read that left earlier lines out.
    * ``updated_at`` is when the file last changed and ``now`` is the server's
      clock, both UTC, so the client can show how long the scan has been quiet
      without trusting its own clock.
    """
    path = run_dir / LOG_FILE_NAME
    # A symlink could point anywhere on disk; the log is always a plain file here.
    try:
        if path.is_symlink() or not path.is_file():
            return _empty(exists=False)
        stat = path.stat()
        size = stat.st_size
        updated_at = (
            datetime.fromtimestamp(stat.st_mtime, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except OSError:
        return _empty(exists=False)

    tail = after is None
    reset = False
    if after is None:
        start = max(0, size - TAIL_BYTES)
    elif after > size:
        start, reset = 0, True
    else:
        start = after

    try:
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read(min(size - start, MAX_CHUNK_BYTES))
    except OSError:
        return _empty(exists=False)

    cut = data.rfind(b"\n")
    if cut == -1:
        # No complete line yet. Wait for the writer to finish it, unless the
        # buffer is full with no newline at all (one gigantic line): consume it
        # as a truncated line rather than stalling the stream forever.
        if len(data) < MAX_CHUNK_BYTES:
            return _empty(exists=True, offset=start, size=size, reset=reset, updated_at=updated_at)
        complete = data
    else:
        complete = data[: cut + 1]
    offset = start + len(complete)

    text = complete.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    # A tail read can start in the middle of a line; drop that fragment.
    dropped_head = tail and start > 0
    if dropped_head and lines:
        lines.pop(0)

    truncated_head = dropped_head
    if tail and len(lines) > TAIL_LINES:
        lines = lines[-TAIL_LINES:]
        truncated_head = True

    mask = redactor or make_redactor()
    return {
        "exists": True,
        "entries": [parse_line(line.rstrip("\r"), mask) for line in lines],
        "offset": offset,
        "size": size,
        "reset": reset,
        "more": offset < size,
        "truncated_head": truncated_head,
        "updated_at": updated_at,
        "now": _utc_now_iso(),
    }


__all__ = [
    "LOG_FILE_NAME",
    "InvalidCursorError",
    "parse_cursor",
    "parse_line",
    "read_log_chunk",
]
