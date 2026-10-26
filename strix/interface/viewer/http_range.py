"""Parse an HTTP ``Range`` request header (RFC 9110, section 14) for one file.

Browsers need byte ranges to seek inside a ``<video>``: without a 206 answer the
chapter buttons on the landing page cannot jump to a part of the video that has
not been downloaded yet. Only the single-range ``bytes=`` form is supported,
which is all a browser sends for media. Anything else is treated as "no range"
and the whole file is sent, which the RFC allows.
"""

from __future__ import annotations

import re
from typing import Final, Literal


#: Result when the header asks for bytes the file does not have (answer 416).
UNSATISFIABLE: Final = "unsatisfiable"

#: ASCII digits only. ``\d`` would also accept other scripts' digits (e.g. fullwidth).
_RANGE_RE = re.compile(r"^bytes=([0-9]*)-([0-9]*)$")

RangeResult = tuple[int, int] | Literal["unsatisfiable"] | None


def _suffix_range(count: int, size: int) -> RangeResult:
    """``bytes=-N``: the last ``count`` bytes."""
    if count == 0 or size == 0:
        return UNSATISFIABLE
    return max(0, size - count), size - 1


def _bounded_range(first: int, last_text: str, size: int) -> RangeResult:
    """``bytes=A-`` or ``bytes=A-B``."""
    if first >= size:
        return UNSATISFIABLE
    if not last_text:
        return first, size - 1
    last = int(last_text)
    if last < first:
        return None  # an invalid range is ignored, not an error (RFC 9110)
    return first, min(last, size - 1)


def parse_byte_range(header: str | None, size: int) -> RangeResult:
    """Resolve ``header`` against a file of ``size`` bytes.

    Returns:
        ``None`` when there is no usable range (absent, another unit, several
        ranges, or malformed): send the whole file with 200.
        ``(first, last)`` inclusive byte offsets to send with 206.
        ``UNSATISFIABLE`` when the range lies wholly outside the file (416).
    """
    match = _RANGE_RE.match(header.strip()) if header else None
    if match is None:
        return None
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        return None
    if not start_text:
        return _suffix_range(int(end_text), size)
    return _bounded_range(int(start_text), end_text, size)


__all__ = ["UNSATISFIABLE", "parse_byte_range"]
