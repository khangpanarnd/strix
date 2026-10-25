"""The isolated connection-test runner (FR-27..FR-32, BR-29..BR-34, BR-38, DD-5).

:class:`ConnectionTester` runs the model preflight in a *fresh* child process
(:mod:`strix.interface.viewer.llm_test_child`), never in the viewer process,
because the preflight mutates process-wide SDK state later scans would inherit.
The tester:

* allows one test at a time (409 ``test_in_progress``) and at most six per
  rolling minute (429 ``rate_limited``) (FR-30);
* builds the child's environment by *removing* the nine LLM names, the provider
  key variable derived from the typed model, and every variable whose name ends
  in ``_API_KEY`` / ``_TOKEN`` / ``_SECRET`` / ``_PASSWORD`` — it never adds one
  (BR-29);
* passes the typed values on standard input, never on the command line, so the
  key is never in argv or the child's environment (BR-29);
* drains the child's merged stdout+stderr on a daemon thread, keeping only the
  last 64 KiB, which is never returned to the browser (BR-38);
* enforces a 90-second wall-clock limit on an injectable clock, killing the whole
  process tree with the scan launcher's own kill function on expiry (BR-31);
* parses only the last ``@@STRIX_TEST_RESULT@@`` line as strict JSON, mapping
  anything else to ``unknown`` (BR-33);
* builds the message from FIXED text per category plus ``HTTP <status>`` when
  known, never from child output, and passes it through :func:`redact` (BR-32).

Every OS seam (``popen``, ``clock``, ``kill_tree``, ``environ``, ``boot``) is
injected so tests spawn no real process and make no real network call (NFR-7).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from typing import IO, TYPE_CHECKING, Any

from strix.interface.viewer.launch_errors import LaunchError
from strix.interface.viewer.llm_test_child import BOOT_SOURCE, RESULT_MARKER
from strix.interface.viewer.redaction import redact
from strix.interface.viewer.scan_launcher import _default_kill_tree, _process_group_kwargs


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from strix.interface.viewer.scan_launcher import PopenLike


# Hard wall-clock limit for one test; the tree is killed on expiry (BR-31).
_TEST_TIMEOUT_SECONDS = 90.0

# Rate limit: at most six tests per rolling minute (FR-30).
_RATE_LIMIT = 6
_RATE_WINDOW_SECONDS = 60.0

# The merged output ring keeps only the last 64 KiB, so a noisy library cannot
# fill the pipe and hang the child; the kept bytes are never returned (BR-38).
_OUTPUT_TAIL_BYTES = 64 * 1024

# How long to wait for the reader thread to drain after the child exits/dies.
_READER_JOIN_TIMEOUT = 2.0

# How often the wait loop polls the child while counting against the injectable clock.
_POLL_INTERVAL_SECONDS = 0.05

# The nine strip-set names (canonical + aliases), upper-cased (section 2 vocab).
_STRIP_SET: frozenset[str] = frozenset(
    {
        "STRIX_LLM",
        "LLM_API_KEY",
        "LLM_API_BASE",
        "STRIX_REASONING_EFFORT",
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
        "LITELLM_BASE_URL",
        "OLLAMA_API_BASE",
    }
)

# Suffixes that mark any variable as a secret to drop from the child (BR-29).
_SECRET_NAME_SUFFIXES: tuple[str, ...] = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")

# Provider-prefix exceptions whose key variable does not follow <PROVIDER>_API_KEY (DD-3).
_PROVIDER_KEY_EXCEPTIONS: dict[str, str] = {
    "TOGETHER_AI": "TOGETHERAI_API_KEY",
    "PERPLEXITY": "PERPLEXITYAI_API_KEY",
    "MISTRALAI": "MISTRAL_API_KEY",
}

# Prefixes that are meta-routers to strip before deriving the provider (DD-3).
_META_PREFIXES: tuple[str, ...] = ("litellm/", "any-llm/")

# Prefixes already covered by the strip set (OpenAI) or with no provider variable.
_NO_PROVIDER_VAR_PREFIXES: frozenset[str] = frozenset({"openai", "chatgpt", "ollama"})

# The closed category set echoed from the child, plus the fixed human text (BR-32).
_FIXED_MESSAGES: dict[str, str] = {
    "ok": "The connection succeeded.",
    "auth_failed": "The provider rejected the API key.",
    "model_not_found": "The provider did not recognise the model.",
    "unreachable": "The API base URL could not be reached.",
    "timeout": "The connection test timed out.",
    "invalid_request": "The provider rejected the request.",
    "unknown": "The connection test could not be completed.",
}
_VALID_CATEGORIES: frozenset[str] = frozenset(_FIXED_MESSAGES) - {"ok"}

# The exact key set the child's result line must carry — no more, no fewer (BR-33).
_RESULT_KEYS: frozenset[str] = frozenset({"ok", "category", "status", "latency_ms"})


class ConnectionTester:
    """Runs one isolated model-preflight child at a time and maps its result.

    Thread-safe. The only mutable state is the busy flag and the rate-limit
    window, both guarded by ``self._lock``; nothing lives at module scope.
    """

    def __init__(
        self,
        *,
        popen: Callable[..., PopenLike] | None = None,
        clock: Callable[[], float] | None = None,
        kill_tree: Callable[[PopenLike], None] | None = None,
        environ: Mapping[str, str] | None = None,
        boot: str | None = None,
    ) -> None:
        self._popen = popen or subprocess.Popen
        self._clock = clock or time.monotonic
        self._kill_tree = kill_tree or _default_kill_tree
        self._environ = dict(environ) if environ is not None else None
        self._boot = boot if boot is not None else BOOT_SOURCE

        self._lock = threading.Lock()
        self._busy = False
        self._attempts: list[float] = []

    def run(self, values: Mapping[str, str | None]) -> dict[str, Any]:
        """Run one connection test for the effective ``values``; return a TestResult.

        ``values`` carries the five already-resolved fields (``model``,
        ``api_key``, ``api_base``, ``reasoning_effort``, ``timeout``); resolving
        omitted fields against saved settings and the base-change rule happens in
        the server layer before this is called. Raises :class:`LaunchError` for a
        concurrent test (409) or a rate-limit breach (429).
        """
        self._acquire_slot()
        try:
            return self._run_child(values)
        finally:
            with self._lock:
                self._busy = False

    # -- admission control -------------------------------------------------

    def _acquire_slot(self) -> None:
        """Enforce one-at-a-time and the rolling rate limit under the lock (FR-30)."""
        now = self._clock()
        with self._lock:
            cutoff = now - _RATE_WINDOW_SECONDS
            self._attempts = [t for t in self._attempts if t > cutoff]
            if self._busy:
                raise LaunchError(
                    "test_in_progress", "another connection test is running", status=409
                )
            if len(self._attempts) >= _RATE_LIMIT:
                raise LaunchError(
                    "rate_limited",
                    "too many connection tests; try again shortly",
                    status=429,
                    extra={"retry_after": int(_RATE_WINDOW_SECONDS)},
                )
            self._attempts.append(now)
            self._busy = True

    # -- child execution ---------------------------------------------------

    def _run_child(self, values: Mapping[str, str | None]) -> dict[str, Any]:
        """Spawn the child, feed it the payload, time it, and map the outcome."""
        base_env = self._environ if self._environ is not None else None
        child_env = _build_child_env(values.get("model"), base_env)
        argv = self._build_argv()
        payload = _build_payload(values)

        process = self._popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            env=child_env,
            text=True,
            **_process_group_kwargs(),
        )

        buffer = _TailBuffer(_OUTPUT_TAIL_BYTES)
        reader = threading.Thread(
            target=_drain, args=(process.stdout, buffer), name="llm-test-reader", daemon=True
        )
        reader.start()

        self._write_payload(process, payload)
        timed_out = self._await_child(process)
        if timed_out:
            self._kill_tree(process)
            self._join_reader(reader)
            return _standalone_result("timeout", None, None)

        self._join_reader(reader)
        return _parse_result(buffer.text())

    def _build_argv(self) -> list[str]:
        """``[python, -c, BOOT]`` (or the frozen executable), no shell (DD-5)."""
        if getattr(sys, "frozen", False):
            # A frozen build cannot run ``-c``; a future packaging step wires a
            # dedicated entry. Until then the boot string rides as a marker arg.
            return [sys.executable, "--strix-llm-test-child"]
        return [sys.executable, "-c", self._boot]

    def _write_payload(self, process: PopenLike, payload: str) -> None:
        """Write the JSON payload to the child's stdin and close it (key never in argv)."""
        stdin = getattr(process, "stdin", None)
        if stdin is None:
            return
        try:
            stdin.write(payload)
            stdin.flush()
        except (OSError, ValueError):
            # The child may have exited before reading; the wait/parse path handles it.
            pass
        finally:
            with contextlib.suppress(OSError, ValueError):
                stdin.close()

    def _await_child(self, process: PopenLike) -> bool:
        """Wait up to the hard limit on the injectable clock; return True on timeout."""
        deadline = self._clock() + _TEST_TIMEOUT_SECONDS
        while self._clock() < deadline:
            if process.poll() is not None:
                return False
            time.sleep(_POLL_INTERVAL_SECONDS)
        # One final check so a child that finished exactly at the deadline is not
        # needlessly killed.
        return process.poll() is None

    def _join_reader(self, reader: threading.Thread) -> None:
        if reader.is_alive():
            reader.join(timeout=_READER_JOIN_TIMEOUT)


# -- module helpers --------------------------------------------------------


def _parse_result(text: str) -> dict[str, Any]:
    """Parse the last marker line as strict JSON; map anything else to ``unknown``.

    The accepted shape is EXACTLY the four keys ``{ok, category, status,
    latency_ms}`` with ``ok: bool``, ``category: str|null``, ``status: int|null``,
    ``latency_ms: int|null`` (BR-33). A missing key, an extra key, a wrong type,
    or an unknown category all map to ``unknown``. A valid line with ``ok: true``
    becomes a success result; a valid failure line is mapped through the fixed
    message table.
    """
    parsed = _extract_marker_json(text)
    if parsed is None:
        return _standalone_result("unknown", None, None)

    # Strict key set: exactly the four expected keys, no more, no fewer (BR-33).
    if set(parsed) != _RESULT_KEYS:
        return _standalone_result("unknown", None, None)

    ok = parsed.get("ok")
    status = parsed.get("status")
    latency = parsed.get("latency_ms")
    category = parsed.get("category")

    # ``bool`` is a subclass of ``int`` in Python, so a boolean must not be
    # allowed to masquerade as the integer ``status`` / ``latency_ms`` fields.
    if not isinstance(ok, bool):
        return _standalone_result("unknown", None, None)
    if not _is_int_or_none(status):
        return _standalone_result("unknown", None, None)
    if not _is_int_or_none(latency):
        return _standalone_result("unknown", None, None)
    if category is not None and not isinstance(category, str):
        return _standalone_result("unknown", None, None)

    if ok:
        return _standalone_result(None, status, latency, ok=True)
    if not isinstance(category, str) or category not in _VALID_CATEGORIES:
        return _standalone_result("unknown", status, latency)
    return _standalone_result(category, status, latency)


def _is_int_or_none(value: Any) -> bool:
    """True when ``value`` is ``None`` or a real ``int`` (not a ``bool``)."""
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def _standalone_result(
    category: str | None, status: int | None, latency_ms: int | None, *, ok: bool = False
) -> dict[str, Any]:
    """Assemble the TestResult dict with fixed, redacted message text (BR-32).

    The message is FIXED per category plus ``HTTP <status>`` when known — never
    built from child output. ``redact`` runs defensively so a future edit that
    interpolates a value can never leak a secret.
    """
    key = "ok" if ok else (category or "unknown")
    text = _FIXED_MESSAGES.get(key, _FIXED_MESSAGES["unknown"])
    if status is not None:
        text = f"{text} (HTTP {status})"
    return {
        "ok": ok,
        "latency_ms": latency_ms,
        "category": None if ok else category,
        "status": status,
        "message": redact(text),
    }


def _extract_marker_json(text: str) -> dict[str, Any] | None:
    """Return the JSON object from the LAST marker-prefixed line, or ``None``.

    Only lines beginning with the marker are considered, so arbitrary noise from
    a chatty library before the result is ignored; the last such line wins so a
    retry or duplicate marker does not confuse the parser (BR-33).
    """
    last: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith(RESULT_MARKER):
            continue
        body = line[len(RESULT_MARKER) :].strip()
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            last = parsed
    return last


def _build_payload(values: Mapping[str, str | None]) -> str:
    """One JSON line with exactly the five fields the child expects on stdin."""
    payload = {
        "model": values.get("model"),
        "api_key": values.get("api_key"),
        "api_base": values.get("api_base"),
        "reasoning_effort": values.get("reasoning_effort"),
        "timeout": values.get("timeout"),
    }
    return json.dumps(payload) + "\n"


def _build_child_env(model: str | None, base: Mapping[str, str] | None) -> dict[str, str]:
    """Build the child's environment: viewer env MINUS every secret-shaped name (BR-29).

    Removes the nine strip-set names, the provider key variable derived from the
    typed model, and any variable whose upper-cased name ends with one of the
    secret suffixes. It never adds a variable — the typed values travel on stdin.
    """
    source = base if base is not None else os.environ
    provider_var = _provider_key_var(model)

    result: dict[str, str] = {}
    for name, value in source.items():
        upper = name.upper()
        if upper in _STRIP_SET:
            continue
        if provider_var is not None and upper == provider_var:
            continue
        if upper.endswith(_SECRET_NAME_SUFFIXES):
            continue
        result[name] = value
    return result


def _provider_key_var(model: str | None) -> str | None:
    """Derive ``<PROVIDER>_API_KEY`` from a ``provider/name`` model, or ``None`` (DD-3).

    Meta-router prefixes (``litellm/``, ``any-llm/``) are stripped first. Bare
    names and the OpenAI/ChatGPT/Ollama families return ``None`` (OpenAI is
    already in the strip set; the others have no provider variable). A small
    exception table covers providers whose variable breaks the pattern.
    """
    if not model:
        return None
    route = model.strip()
    for prefix in _META_PREFIXES:
        if route.lower().startswith(prefix):
            route = route[len(prefix) :]
            break
    if "/" not in route:
        return None
    provider = route.split("/", 1)[0]
    if not provider or provider.lower() in _NO_PROVIDER_VAR_PREFIXES:
        return None
    token = provider.upper().replace("-", "_")
    if token in _PROVIDER_KEY_EXCEPTIONS:
        return _PROVIDER_KEY_EXCEPTIONS[token]
    candidate = f"{token}_API_KEY"
    # Defence in depth: only return a well-formed <PROVIDER>_API_KEY name.
    if not _is_valid_key_var(candidate):
        return None
    return candidate


def _is_valid_key_var(name: str) -> bool:
    """True when ``name`` matches ``^[A-Z0-9_]+_API_KEY$`` (DD-3)."""
    if not name.endswith("_API_KEY"):
        return False
    stem = name[: -len("_API_KEY")]
    return bool(stem) and all(ch.isalnum() or ch == "_" for ch in name) and name.isupper()


class _TailBuffer:
    """A thread-safe byte-bounded tail of the child's merged output (BR-38)."""

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._chunks: list[str] = []
        self._size = 0
        self._lock = threading.Lock()

    def append(self, chunk: str) -> None:
        """Append ``chunk`` and trim the oldest content past the byte budget."""
        with self._lock:
            self._chunks.append(chunk)
            self._size += len(chunk)
            while self._size > self._max_bytes and len(self._chunks) > 1:
                removed = self._chunks.pop(0)
                self._size -= len(removed)

    def text(self) -> str:
        """The current tail joined, trimmed to the last ``max_bytes`` characters."""
        with self._lock:
            joined = "".join(self._chunks)
        if len(joined) > self._max_bytes:
            joined = joined[-self._max_bytes :]
        return joined


def _drain(stream: IO[str] | None, buffer: _TailBuffer) -> None:
    """Daemon reader: copy the child's merged output into the bounded tail buffer.

    Reads line by line so a huge single stream is bounded continuously and never
    blocks the child; ends by itself at end-of-file when the child exits.
    """
    if stream is None:
        return
    try:
        for line in iter(stream.readline, ""):
            if not line:
                break
            buffer.append(line)
    except (OSError, ValueError):
        pass


__all__ = ["ConnectionTester"]
