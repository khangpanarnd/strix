"""Secret redaction for viewer-launched scan output and audit text (BR-30, NFR-5).

Child-process output is shown to the browser and written to logs, so it must
never leak the viewer's own credentials. This module masks known secret-shaped
substrings and the values of secret-named environment variables before any of
that text leaves the process.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


REPLACEMENT = "[REDACTED]"

# Shortest value worth masking: shorter env values (and short secret lines) are
# too likely to be common words, so masking them would corrupt clean output.
_MIN_SECRET_LEN = 6
_MAX_FRAGMENTS = 100

# Environment variables whose *name* ends in one of these are treated as secret.
_SECRET_NAME_SUFFIXES = ("KEY", "TOKEN", "SECRET", "PASSWORD")

# Secret-shaped literals independent of any environment variable.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"Bearer\s+\S+"),
)


def _secret_values(env: Mapping[str, str]) -> list[str]:
    """Collect the maskable fragments contributed by secret-named env vars.

    For a multi-line value, both the whole value and each individual line of at
    least ``_MIN_SECRET_LEN`` characters are returned, so a secret split across
    lines in output is still caught (BR-30).
    """
    fragments: list[str] = []
    for name, value in env.items():
        if not value or not name.upper().endswith(_SECRET_NAME_SUFFIXES):
            continue
        if len(value) >= _MIN_SECRET_LEN:
            fragments.append(value)
        fragments.extend(line for line in value.splitlines() if len(line) >= _MIN_SECRET_LEN)
    # Longest first so a whole multi-line value is replaced before its lines,
    # and so overlapping fragments don't leave a partial secret behind.
    # Longest first so overlapping fragments don't leave a partial secret behind;
    # capped so a pathological multi-thousand-line env value cannot make every
    # failure tail cost O(lines x output).
    return sorted(set(fragments), key=len, reverse=True)[:_MAX_FRAGMENTS]


def make_redactor(env: Mapping[str, str] | None = None) -> Callable[[str], str]:
    """Build a redaction function whose secret list is computed once.

    ``redact`` rebuilds that list on every call, which is wasteful for a batch
    such as a streamed log. This does the same masking, with the fragments
    gathered up front. ``env`` defaults to ``os.environ`` read now.
    """
    environment = os.environ if env is None else env
    fragments = _secret_values(environment)

    def _redact(text: str) -> str:
        if not text:
            return text
        redacted = text
        for value in fragments:
            redacted = redacted.replace(value, REPLACEMENT)
        for pattern in _PATTERNS:
            redacted = pattern.sub(REPLACEMENT, redacted)
        return redacted

    return _redact


def redact(text: str, env: Mapping[str, str] | None = None) -> str:
    """Return ``text`` with secret-named env values and secret patterns masked.

    ``env`` defaults to ``os.environ``. Replacement is always ``[REDACTED]``.
    """
    if not text:
        return text
    return make_redactor(env)(text)
