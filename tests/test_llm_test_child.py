"""Tests for the isolated connection-test child (Step 4.1; FR-28, BR-29..BR-34).

The child's heavy seams — the config loader and the model preflight — are
injected as fakes, so no real LLM module loads and no real network call is made.
The child is driven in-process through :func:`main` with ``io.StringIO`` for
stdin and stdout; the real subprocess boundary is covered by the ConnectionTester
tests.
"""

from __future__ import annotations

import io
import json
import os
from typing import TYPE_CHECKING, Any

import pytest

from strix.interface.viewer import llm_test_child
from strix.interface.viewer.llm_test_child import RESULT_MARKER, main


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


class _FakeLlm:
    def __init__(self, model: str | None) -> None:
        self.model = model


class _FakeSettings:
    def __init__(self, model: str | None) -> None:
        self.llm = _FakeLlm(model)


class _FakeLoader:
    """A stand-in for strix.config.loader that never reads the user's real file.

    ``resolved_model`` is what ``load_settings().llm.model`` returns; set it
    different from the typed model to exercise the self-check failure path.
    ``config_override`` records the path the child points the loader at (BR-30).
    """

    def __init__(self, resolved_model: str | None) -> None:
        self._resolved_model = resolved_model
        self._cached: Any = None
        self.config_override: Path | None = None

    def apply_config_override(self, path: Path) -> None:
        self.config_override = path
        self._cached = None

    def load_settings(self) -> _FakeSettings:
        self._cached = _FakeSettings(self._resolved_model)
        return self._cached


def _preflight_ok() -> Callable[..., Awaitable[None]]:
    async def _preflight(_model: str, *, settings: Any) -> None:  # noqa: ARG001
        return None

    return _preflight


def _preflight_raising(exc: BaseException) -> Callable[..., Awaitable[None]]:
    async def _preflight(_model: str, *, settings: Any) -> None:  # noqa: ARG001
        raise exc

    return _preflight


def _run_child(
    payload: dict[str, Any],
    *,
    resolved_model: str | None,
    preflight: Callable[..., Awaitable[None]],
) -> tuple[int, dict[str, Any], _FakeLoader]:
    """Drive ``main`` in-process and return (exit code, parsed result, loader)."""
    stdin = io.StringIO(json.dumps(payload) + "\n")
    stdout = io.StringIO()
    loader = _FakeLoader(resolved_model)
    code = main(stdin, stdout, preflight=preflight, loader=loader)
    result = _parse_last_marker(stdout.getvalue())
    return code, result, loader


def _parse_last_marker(text: str) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(RESULT_MARKER):
            last = json.loads(line[len(RESULT_MARKER) :].strip())
    return last


# --- happy path & category mapping ------------------------------------------


def test_success_emits_ok_with_latency() -> None:
    code, result, _ = _run_child(
        {"model": "anthropic/claude", "api_key": "k", "timeout": 30},
        resolved_model="anthropic/claude",
        preflight=_preflight_ok(),
    )
    assert code == 0
    assert result["ok"] is True
    assert result["category"] is None
    assert result["status"] is None
    assert isinstance(result["latency_ms"], int)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "auth_failed"),
        (403, "auth_failed"),
        (404, "model_not_found"),
        (400, "invalid_request"),
        (422, "invalid_request"),
    ],
)
def test_http_status_maps_to_category(status: int, expected: str) -> None:
    class _HttpError(Exception):
        def __init__(self) -> None:
            super().__init__("provider said no")
            self.status_code = status

    code, result, _ = _run_child(
        {"model": "m", "timeout": 30},
        resolved_model="m",
        preflight=_preflight_raising(_HttpError()),
    )
    assert code == 0
    assert result["ok"] is False
    assert result["category"] == expected
    assert result["status"] == status


def test_authentication_error_class_maps_to_auth_failed() -> None:
    class AuthenticationError(Exception):
        pass

    _, result, _ = _run_child(
        {"model": "m", "timeout": 30},
        resolved_model="m",
        preflight=_preflight_raising(AuthenticationError("bad key")),
    )
    assert result["category"] == "auth_failed"


def test_not_found_class_maps_to_model_not_found() -> None:
    class NotFoundError(Exception):
        pass

    _, result, _ = _run_child(
        {"model": "m", "timeout": 30},
        resolved_model="m",
        preflight=_preflight_raising(NotFoundError("model not found")),
    )
    assert result["category"] == "model_not_found"


def test_connection_error_class_maps_to_unreachable() -> None:
    class APIConnectionError(Exception):
        pass

    _, result, _ = _run_child(
        {"model": "m", "timeout": 30},
        resolved_model="m",
        preflight=_preflight_raising(APIConnectionError("dns failed")),
    )
    assert result["category"] == "unreachable"


def test_timeout_maps_to_timeout() -> None:
    _, result, _ = _run_child(
        {"model": "m", "timeout": 30},
        resolved_model="m",
        preflight=_preflight_raising(TimeoutError("too slow")),
    )
    assert result["category"] == "timeout"
    assert result["status"] is None


def test_unknown_exception_maps_to_unknown() -> None:
    _, result, _ = _run_child(
        {"model": "m", "timeout": 30},
        resolved_model="m",
        preflight=_preflight_raising(RuntimeError("something odd")),
    )
    assert result["category"] == "unknown"


# --- self-check & isolation --------------------------------------------------


def test_self_check_failure_prints_unknown_and_exits_one() -> None:
    # The loader resolves a DIFFERENT model than typed: the environment was not
    # applied as intended, so the child must refuse to run a preflight (BR-30).
    code, result, _ = _run_child(
        {"model": "typed-model", "timeout": 30},
        resolved_model="some-other-model",
        preflight=_preflight_ok(),
    )
    assert code == 1
    assert result["category"] == "unknown"
    assert result["ok"] is False


def test_child_points_loader_at_a_temp_empty_file() -> None:
    # BR-30: the child overrides the loader with a temporary empty config so the
    # user's real file cannot interfere.
    _, _, loader = _run_child(
        {"model": "m", "timeout": 30},
        resolved_model="m",
        preflight=_preflight_ok(),
    )
    assert loader.config_override is not None
    assert loader.config_override.name == "empty-config.json"


def test_child_sets_and_clears_payload_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Step 2: typed values are set on the child's own environment; a null value
    # removes the name rather than setting the literal string "None".
    monkeypatch.setenv("LLM_API_BASE", "http://stale")
    _run_child(
        {"model": "m", "api_key": "secret-key", "api_base": None, "timeout": 30},
        resolved_model="m",
        preflight=_preflight_ok(),
    )
    assert os.environ["STRIX_LLM"] == "m"
    assert os.environ["LLM_API_KEY"] == "secret-key"
    # api_base was null in the payload, so its name must have been removed.
    assert "LLM_API_BASE" not in os.environ
    # Clean up the names the child set on the real environment.
    for name in ("STRIX_LLM", "LLM_API_KEY", "LLM_PREFLIGHT_TIMEOUT"):
        os.environ.pop(name, None)


def test_result_line_carries_only_the_four_keys() -> None:
    _, result, _ = _run_child(
        {"model": "m", "timeout": 30},
        resolved_model="m",
        preflight=_preflight_ok(),
    )
    assert set(result) == {"ok", "category", "status", "latency_ms"}


def test_malformed_payload_is_unknown_without_loading() -> None:
    # A non-JSON stdin line yields an empty payload -> self-check fails -> unknown.
    stdin = io.StringIO("not json at all\n")
    stdout = io.StringIO()
    loader = _FakeLoader("m")
    code = main(stdin, stdout, preflight=_preflight_ok(), loader=loader)
    assert code == 1
    assert _parse_last_marker(stdout.getvalue())["category"] == "unknown"


def test_key_never_in_emitted_output() -> None:
    stdin = io.StringIO(
        json.dumps({"model": "m", "api_key": "sk-SECRET-VALUE", "timeout": 30}) + "\n"
    )
    stdout = io.StringIO()
    loader = _FakeLoader("m")
    main(stdin, stdout, preflight=_preflight_ok(), loader=loader)
    assert "sk-SECRET-VALUE" not in stdout.getvalue()
    for name in ("STRIX_LLM", "LLM_API_KEY", "LLM_PREFLIGHT_TIMEOUT"):
        os.environ.pop(name, None)


def test_boot_source_is_a_short_runnable_string() -> None:
    assert "from strix.interface.viewer.llm_test_child import main" in llm_test_child.BOOT_SOURCE
    assert "sys.exit(main())" in llm_test_child.BOOT_SOURCE
