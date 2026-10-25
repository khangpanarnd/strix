"""The isolated connection-test child process (FR-28, BR-29..BR-34, DD-5).

This module is the entry point run as a *fresh* Python process by
:class:`strix.interface.viewer.llm_connection_test.ConnectionTester`. It must
never run inside the viewer process, because the model preflight calls
``configure_sdk_model_defaults``, which writes ``os.environ['OPENAI_BASE_URL']``
and provider key variables and sets SDK-wide defaults — side effects that later
viewer-launched scans would inherit (DD-5).

The ordering in :func:`main` is deliberate and load-bearing, because
``strix.config.loader.load_settings`` is memoised and ``StrixProvider.get_model``
calls it internally:

1. read the one JSON line from standard input *before importing any Strix module*;
2. set ``os.environ`` for the five typed names (removing any whose value is null);
3. import ``strix.config.loader``, point it at a temporary EMPTY override file so
   the user's real ``~/.strix/cli-config.json`` cannot interfere (BR-30), assert
   the settings cache is cold, call ``load_settings()`` once and self-check that
   the resolved model equals the typed model;
4. only then import ``strix.interface.scan_setup`` lazily and run the preflight.

The child prints exactly one machine-readable line, LAST, prefixed with
``@@STRIX_TEST_RESULT@@``, carrying only ``{ok, category, status, latency_ms}``.
It never prints exception text, response bodies, headers, or the API key
(BR-32). ``preflight`` and ``loader`` are test seams so tests never import the
heavy LLM modules.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# The marker that prefixes the single result line the parent parses (BR-33).
RESULT_MARKER = "@@STRIX_TEST_RESULT@@"

# The five environment names the payload may set on the child. ``STRIX_LLM`` and
# the preflight timeout are always meaningful; the key/base/effort are optional.
_PAYLOAD_ENV_NAMES: dict[str, str] = {
    "model": "STRIX_LLM",
    "api_key": "LLM_API_KEY",
    "api_base": "LLM_API_BASE",
    "reasoning_effort": "STRIX_REASONING_EFFORT",
    "timeout": "LLM_PREFLIGHT_TIMEOUT",
}

# The closed category set (domain-entities TestResult). ``ok`` is signalled by
# ``ok: true`` with ``category: null`` rather than a category string.
_CATEGORY_AUTH = "auth_failed"
_CATEGORY_MODEL = "model_not_found"
_CATEGORY_UNREACHABLE = "unreachable"
_CATEGORY_TIMEOUT = "timeout"
_CATEGORY_INVALID = "invalid_request"
_CATEGORY_UNKNOWN = "unknown"


def _read_payload(stdin: IO[str]) -> dict[str, Any]:
    """Read and parse the single JSON line of typed values from standard input.

    Done before any Strix import so the heavy modules never load for a malformed
    payload. A non-object or unparseable line yields an empty dict, which the
    self-check below turns into an ``unknown`` result.
    """
    line = stdin.readline()
    try:
        parsed = json.loads(line)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _apply_payload_env(payload: dict[str, Any]) -> None:
    """Set os.environ for the five typed names; remove any whose value is null.

    Values are coerced to ``str`` because the config loader and LiteLLM read
    environment variables as strings. A ``None`` value means "not typed", so the
    name is removed rather than set to the literal string ``"None"``.
    """
    for field_name, env_name in _PAYLOAD_ENV_NAMES.items():
        value = payload.get(field_name)
        if value is None:
            os.environ.pop(env_name, None)
        else:
            os.environ[env_name] = str(value)


def _categorise(exc: BaseException) -> tuple[str, int | None]:
    """Map a preflight exception to a (category, http_status) pair (section 7 table).

    The classification inspects the exception's class name and any
    ``status_code`` attribute WITHOUT importing litellm (DD-4/BR-29): the viewer
    side must never pull that heavy, possibly-networked module in. Exception text
    and response bodies are never read, so nothing from the target leaks.
    """
    if isinstance(exc, TimeoutError):
        return _CATEGORY_TIMEOUT, None

    status = _status_code_of(exc)
    if status is not None:
        if status in (401, 403):
            return _CATEGORY_AUTH, status
        if status == 404:
            return _CATEGORY_MODEL, status
        if status in (400, 422):
            return _CATEGORY_INVALID, status

    name = type(exc).__name__.lower()
    # Walk the base classes too, so a provider subclass of a known error maps.
    class_names = {base.__name__.lower() for base in type(exc).__mro__}

    if any("authentication" in n or "permissiondenied" in n for n in class_names):
        return _CATEGORY_AUTH, status
    if any("notfound" in n for n in class_names) or "model not found" in name:
        return _CATEGORY_MODEL, status
    if _looks_unreachable(class_names):
        return _CATEGORY_UNREACHABLE, status
    if any("badrequest" in n or "unprocessable" in n for n in class_names):
        return _CATEGORY_INVALID, status
    return _CATEGORY_UNKNOWN, status


def _status_code_of(exc: BaseException) -> int | None:
    """Read an int ``status_code``/``status`` attribute if the exception carries one."""
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _looks_unreachable(class_names: set[str]) -> bool:
    """True when the exception class family names a connection/DNS/TLS failure."""
    needles = (
        "connection",
        "connect",
        "ssl",
        "tls",
        "certificate",
        "resolve",
        "dns",
        "socket",
        "apiconnection",
    )
    return any(any(needle in n for needle in needles) for n in class_names)


def _emit(
    stdout: IO[str], *, ok: bool, category: str | None, status: int | None, latency_ms: int | None
) -> None:
    """Print the single result line LAST, with only the four allowed fields (BR-32)."""
    payload = {"ok": ok, "category": category, "status": status, "latency_ms": latency_ms}
    stdout.write(f"{RESULT_MARKER} {json.dumps(payload)}\n")
    stdout.flush()


async def _run_preflight(
    preflight: Callable[..., Awaitable[None]],
    model: str,
    settings: Any,
) -> None:
    """Await the injected (or real) preflight for ``model`` with ``settings``."""
    await preflight(model, settings=settings)


def main(
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    *,
    preflight: Callable[..., Awaitable[None]] | None = None,
    loader: Any = None,
) -> int:
    """Run the connection test and print one result line; return a process exit code.

    ``preflight`` and ``loader`` are injected by tests so no heavy module loads;
    in production they default to the real ``preflight_model_connection`` and the
    real ``strix.config.loader`` module, both imported lazily and only after the
    environment has been prepared (step 3 above).

    Returns 0 on a clean run (including a categorised preflight failure, which is
    a successful *test* that reports a problem) and 1 only when the child could
    not produce a valid result (self-check failure or a crash before emit).
    """
    in_stream = stdin if stdin is not None else sys.stdin
    out_stream = stdout if stdout is not None else sys.stdout

    # Step 1: read the payload before importing any Strix module.
    payload = _read_payload(in_stream)
    model = payload.get("model")

    # Step 2: set the typed values on this child's own environment.
    _apply_payload_env(payload)

    # Step 3: isolate the loader from the user's real file, then self-check.
    loader_module = loader if loader is not None else _import_loader()
    with tempfile.TemporaryDirectory(prefix="strix-test-child-") as tmp_dir:
        empty_override = Path(tmp_dir) / "empty-config.json"
        empty_override.write_text("{}", encoding="utf-8")
        loader_module.apply_config_override(empty_override)

        # The override must have invalidated any memoised settings, or a stale
        # cache from an accidental earlier load would mask the typed values.
        if loader_module._cached is not None:  # documented invariant: cache must be cold
            _emit(out_stream, ok=False, category=_CATEGORY_UNKNOWN, status=None, latency_ms=None)
            return 1

        settings = loader_module.load_settings()

        # Self-check: the resolved model must be exactly the typed model; if the
        # loader disagrees the environment was not applied as intended and we must
        # not run a preflight against the wrong route (BR-30).
        resolved_model = getattr(getattr(settings, "llm", None), "model", None)
        if not isinstance(model, str) or resolved_model != model:
            _emit(out_stream, ok=False, category=_CATEGORY_UNKNOWN, status=None, latency_ms=None)
            return 1

        # Step 4: import the preflight lazily and run it, timing the attempt.
        preflight_fn = preflight if preflight is not None else _import_preflight()
        started = time.monotonic()
        try:
            asyncio.run(_run_preflight(preflight_fn, model, settings))
        except BaseException as exc:  # noqa: BLE001 - every outcome maps to a category
            latency_ms = int((time.monotonic() - started) * 1000)
            category, status = _categorise(exc)
            _emit(out_stream, ok=False, category=category, status=status, latency_ms=latency_ms)
            return 0

        latency_ms = int((time.monotonic() - started) * 1000)
        _emit(out_stream, ok=True, category=None, status=None, latency_ms=latency_ms)
        return 0


def _import_loader() -> Any:
    """Import the real config loader module (production path only)."""
    import strix.config.loader as loader_module

    return loader_module


def _import_preflight() -> Callable[..., Awaitable[None]]:
    """Import the real model preflight lazily (production path only)."""
    from strix.interface.scan_setup import preflight_model_connection

    return preflight_model_connection


# The string handed to ``python -c`` by the ConnectionTester. It imports this
# module and exits with ``main()``'s return code. Kept tiny so nothing but the
# entry runs before stdin is read (DD-5, same pattern as the scan launcher's boot).
BOOT_SOURCE = "import sys; from strix.interface.viewer.llm_test_child import main; sys.exit(main())"


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["BOOT_SOURCE", "RESULT_MARKER", "main"]
