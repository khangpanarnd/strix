"""Tests for the isolated connection-test runner (Step 4.3; FR-27..FR-32, BR-29..BR-34, BR-38).

Every operating-system seam the :class:`ConnectionTester` names is injected with
a fake, so these tests spawn no real process, make no real network call and never
touch the real ``os.environ``: a fake ``popen`` returns a :class:`FakePopen`
pre-seeded with the child's stdout, a fake ``clock`` is advanced by hand, a
recording ``kill_tree`` captures tree kills, an explicit ``environ`` mapping
stands in for the viewer's environment, and a tiny ``boot`` string replaces the
real ``python -c`` payload. The child process itself is covered by
``tests/test_llm_test_child.py``.
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from strix.interface.viewer.launch_errors import LaunchError
from strix.interface.viewer.llm_connection_test import ConnectionTester
from strix.interface.viewer.llm_test_child import RESULT_MARKER


if TYPE_CHECKING:
    from collections.abc import Mapping


# --- fakes ------------------------------------------------------------------


class _RecordingStdin:
    """A minimal write/flush/close stand-in that keeps everything written.

    The tester closes the child's stdin after sending the payload; a plain
    ``io.StringIO`` would raise on a later ``getvalue()``, so this records the
    text and keeps it readable after ``close()``.
    """

    def __init__(self) -> None:
        self._parts: list[str] = []
        self.closed = False

    def write(self, text: str) -> int:
        self._parts.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def getvalue(self) -> str:
        return "".join(self._parts)


class FakePopen:
    """A stand-in for ``subprocess.Popen`` the tester can drive with no real child.

    ``stdout`` is a text stream pre-seeded with whatever the child would have
    printed (the reader thread drains it to EOF). ``stdin`` records everything
    written so a test can assert the payload arrived there. ``exit_code`` is
    ``None`` while "alive"; ``finish(code)`` makes ``poll`` return it. A process
    seeded "already finished" starts with a non-None code so the wait loop
    returns at once.
    """

    _next_pid = 20_000

    def __init__(
        self, argv: list[str], *, output: str = "", exit_code: int | None = 0, **kwargs: Any
    ) -> None:
        self.argv = argv
        self.kwargs = kwargs
        FakePopen._next_pid += 1
        self.pid = FakePopen._next_pid
        self._exit_code = exit_code
        self.stdout: io.StringIO | None = io.StringIO(output)
        self.stdin = _RecordingStdin()
        self.killed = False

    def finish(self, code: int) -> None:
        self._exit_code = code

    def poll(self) -> int | None:
        return self._exit_code

    def kill(self) -> None:
        self.killed = True
        if self._exit_code is None:
            self._exit_code = -9


class NeverExitsPopen(FakePopen):
    """A child that never exits on its own: ``poll`` stays ``None`` until killed."""

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        kwargs.pop("exit_code", None)
        super().__init__(argv, exit_code=None, **kwargs)


class FakeClock:
    """A monotonic clock that advances a fixed step on every read.

    The tester reads the clock in a poll loop; a per-read step lets a
    never-exiting child drive straight past the 90 s deadline without any real
    sleeping, so the timeout path is deterministic and fast.
    """

    def __init__(self, step: float = 0.0, start: float = 1_000.0) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _result_line(**fields: Any) -> str:
    """One marker-prefixed child result line with the given JSON fields."""
    return f"{RESULT_MARKER} {json.dumps(fields)}\n"


def _ok_line(latency_ms: int = 12) -> str:
    return _result_line(ok=True, category=None, status=None, latency_ms=latency_ms)


def _fail_line(category: str, status: int | None = None, latency_ms: int = 34) -> str:
    return _result_line(ok=False, category=category, status=status, latency_ms=latency_ms)


def _make_tester(
    *,
    output: str = "",
    exit_code: int | None = 0,
    process: FakePopen | None = None,
    clock: FakeClock | None = None,
    environ: Mapping[str, str] | None = None,
    kills: list[FakePopen] | None = None,
) -> tuple[ConnectionTester, list[FakePopen]]:
    """Build a tester whose single spawn yields a controlled :class:`FakePopen`."""
    spawned: list[FakePopen] = []
    seeded = process

    def popen(argv: list[str], **kwargs: Any) -> FakePopen:
        proc = seeded if seeded is not None else FakePopen(argv, output=output, exit_code=exit_code)
        proc.argv = argv
        proc.kwargs = kwargs
        spawned.append(proc)
        return proc

    def kill_tree(proc: Any) -> None:
        if kills is not None:
            kills.append(proc)
        proc.kill()

    tester = ConnectionTester(
        popen=popen,
        clock=clock or FakeClock(),
        kill_tree=kill_tree,
        environ=environ if environ is not None else {"PATH": "/usr/bin"},
        boot="pass",
    )
    return tester, spawned


_VALUES: dict[str, str | None] = {
    "model": "anthropic/claude",
    "api_key": "sk-SECRET-VALUE",
    "api_base": None,
    "reasoning_effort": "high",
    "timeout": "30",
}


# --- category mapping from the child's result line --------------------------


def test_success_line_maps_to_ok_result() -> None:
    tester, _ = _make_tester(output=_ok_line(latency_ms=42))
    result = tester.run(dict(_VALUES))
    assert result["ok"] is True
    assert result["category"] is None
    assert result["latency_ms"] == 42
    assert result["message"] == "The connection succeeded."


@pytest.mark.parametrize(
    ("category", "status"),
    [
        ("auth_failed", 401),
        ("model_not_found", 404),
        ("unreachable", None),
        ("invalid_request", 400),
    ],
)
def test_failure_categories_map_through_fixed_table(category: str, status: int | None) -> None:
    tester, _ = _make_tester(output=_fail_line(category, status=status))
    result = tester.run(dict(_VALUES))
    assert result["ok"] is False
    assert result["category"] == category
    assert result["status"] == status
    # The message is fixed per category; a status, when present, is appended.
    if status is not None:
        assert f"HTTP {status}" in result["message"]
    else:
        assert "HTTP" not in result["message"]


def test_timeout_category_from_child_is_honoured() -> None:
    # The child itself can report a timeout (TimeoutError in the preflight); that
    # is distinct from the parent's own 90 s kill path below.
    tester, spawned = _make_tester(output=_fail_line("timeout", status=None))
    result = tester.run(dict(_VALUES))
    assert result["category"] == "timeout"
    assert spawned[0].killed is False


def test_unknown_category_from_child_maps_to_unknown() -> None:
    tester, _ = _make_tester(output=_fail_line("teapot", status=418))
    result = tester.run(dict(_VALUES))
    assert result["category"] == "unknown"


def test_result_dict_has_the_exact_public_shape() -> None:
    tester, _ = _make_tester(output=_fail_line("auth_failed", status=401))
    result = tester.run(dict(_VALUES))
    assert set(result) == {"ok", "latency_ms", "category", "status", "message"}


# --- the parent's own 90 s timeout -> tree kill -----------------------------


def test_timeout_kills_tree_and_returns_timeout() -> None:
    # A child that never exits + a clock that advances past the 90 s deadline:
    # the parent kills the whole tree and returns a fixed timeout result.
    clock = FakeClock(step=50.0)  # two reads cross the 90 s deadline
    kills: list[FakePopen] = []
    proc = NeverExitsPopen([], output="noise but no marker\n")
    tester, spawned = _make_tester(process=proc, clock=clock, kills=kills)

    result = tester.run(dict(_VALUES))

    assert result["ok"] is False
    assert result["category"] == "timeout"
    assert result["message"] == "The connection test timed out."
    assert kills == [proc]
    assert proc.killed is True
    assert spawned == [proc]


# --- admission control: one at a time (409) and 6 per minute (429) ----------


def test_second_concurrent_run_is_rejected_409_without_spawning() -> None:
    # The first run blocks inside the child wait; a second overlapping run must
    # get 409 test_in_progress and must NOT spawn a process.
    release = threading.Event()
    spawned: list[FakePopen] = []

    class BlockingPopen(FakePopen):
        def poll(self) -> int | None:
            # Stay "alive" until the test releases the gate, so the first run is
            # still holding the busy slot when the second run is attempted.
            if not release.is_set():
                return None
            return 0

    proc = BlockingPopen([], output=_ok_line())

    def popen(argv: list[str], **kwargs: Any) -> FakePopen:  # noqa: ARG001
        spawned.append(proc)
        proc.argv = argv
        return proc

    tester = ConnectionTester(
        popen=popen,
        clock=FakeClock(step=0.0),
        kill_tree=lambda _p: None,
        environ={"PATH": "/usr/bin"},
        boot="pass",
    )

    first_result: dict[str, Any] = {}

    def first_run() -> None:
        first_result.update(tester.run(dict(_VALUES)))

    worker = threading.Thread(target=first_run)
    worker.start()
    try:
        # Wait until the first run has taken the slot and spawned its child.
        deadline = time.time() + 3.0
        while time.time() < deadline and not spawned:
            time.sleep(0.01)
        assert spawned, "first run never spawned its child"

        with pytest.raises(LaunchError) as exc:
            tester.run(dict(_VALUES))
        assert exc.value.code == "test_in_progress"
        assert exc.value.status == 409
        # The rejected run never spawned a second process.
        assert len(spawned) == 1
    finally:
        release.set()
        worker.join(timeout=3.0)

    assert first_result["ok"] is True


def test_rate_limit_after_six_runs_then_recovers_after_the_window() -> None:
    clock = FakeClock(step=0.0)
    calls = {"n": 0}

    def popen(argv: list[str], **kwargs: Any) -> FakePopen:  # noqa: ARG001
        calls["n"] += 1
        return FakePopen(argv, output=_ok_line(), exit_code=0)

    tester = ConnectionTester(
        popen=popen,
        clock=clock,
        kill_tree=lambda _p: None,
        environ={"PATH": "/usr/bin"},
        boot="pass",
    )

    # Six runs inside the window all succeed (each completes before the next).
    for _ in range(6):
        assert tester.run(dict(_VALUES))["ok"] is True

    # The seventh within the same minute is rejected before any spawn.
    with pytest.raises(LaunchError) as exc:
        tester.run(dict(_VALUES))
    assert exc.value.code == "rate_limited"
    assert exc.value.status == 429
    assert calls["n"] == 6  # the rejected attempt never spawned

    # After the rolling window slides, a further run is allowed again.
    clock.advance(61.0)
    assert tester.run(dict(_VALUES))["ok"] is True
    assert calls["n"] == 7


# --- malformed / missing / duplicated / extra-key result lines -> unknown ---


@pytest.mark.parametrize(
    "output",
    [
        "no marker at all\n",  # no result line
        f"{RESULT_MARKER} not json\n",  # marker but unparseable
        f"{RESULT_MARKER} [1, 2, 3]\n",  # marker but a list, not an object
        _result_line(ok=True, category=None, status=None),  # missing latency_ms key
        _result_line(ok=True, category=None, status=None, latency_ms=1, extra="x"),  # extra key
        _result_line(ok="yes", category=None, status=None, latency_ms=1),  # ok wrong type
        _result_line(ok=False, category="auth_failed", status="401", latency_ms=1),  # status str
        _result_line(ok=True, category=None, status=True, latency_ms=1),  # bool-as-int status
    ],
)
def test_malformed_result_lines_map_to_unknown(output: str) -> None:
    tester, _ = _make_tester(output=output)
    result = tester.run(dict(_VALUES))
    assert result["ok"] is False
    assert result["category"] == "unknown"
    assert result["message"] == "The connection test could not be completed."


def test_only_the_last_marker_line_is_parsed() -> None:
    # Two marker lines: a duplicate earlier line must be ignored; the LAST wins.
    output = _fail_line("auth_failed", status=401) + _ok_line(latency_ms=7)
    tester, _ = _make_tester(output=output)
    result = tester.run(dict(_VALUES))
    assert result["ok"] is True
    assert result["latency_ms"] == 7


# --- a huge noisy stream neither hangs nor leaks ----------------------------


def test_large_noise_on_merged_stream_does_not_hang_or_return() -> None:
    noise = "A" * (100 * 1024)  # 100 KiB of noise on the merged stdout+stderr
    output = noise + "\n" + _fail_line("unreachable", status=None)
    tester, _ = _make_tester(output=output)

    result = tester.run(dict(_VALUES))

    assert result["category"] == "unreachable"
    # None of the raw noise is ever surfaced to the browser.
    assert "AAAA" not in result["message"]


# --- the key and model travel on stdin, never in argv or the child env ------


def test_key_and_model_arrive_on_stdin_never_in_argv_or_env() -> None:
    environ = {"PATH": "/usr/bin", "HOME": "/home/u"}
    tester, spawned = _make_tester(output=_ok_line(), environ=environ)
    tester.run(dict(_VALUES))

    proc = spawned[0]
    argv_blob = "\n".join(proc.argv)
    env = proc.kwargs["env"]

    # Neither the key nor the model is anywhere in the command line.
    assert "sk-SECRET-VALUE" not in argv_blob
    assert "anthropic/claude" not in argv_blob
    # Nor in the child's environment passed to Popen.
    for value in env.values():
        assert "sk-SECRET-VALUE" not in value
    assert "STRIX_LLM" not in env
    assert "LLM_API_KEY" not in env

    # But both DO arrive on the child's stdin as the JSON payload.
    payload = json.loads(proc.stdin.getvalue())
    assert payload["model"] == "anthropic/claude"
    assert payload["api_key"] == "sk-SECRET-VALUE"
    assert payload["reasoning_effort"] == "high"
    assert payload["timeout"] == "30"


def test_child_env_strips_the_nine_names_and_secret_suffixes_keeps_path() -> None:
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "LANG": "en_US.UTF-8",
        # The nine strip-set names (canonical + aliases):
        "STRIX_LLM": "shell-model",
        "LLM_API_KEY": "shell-key",
        "LLM_API_BASE": "http://shell",
        "STRIX_REASONING_EFFORT": "low",
        "OPENAI_API_KEY": "oai",
        "OPENAI_API_BASE": "oai-base",
        "OPENAI_BASE_URL": "oai-url",
        "LITELLM_BASE_URL": "lite",
        "OLLAMA_API_BASE": "olla",
        # Secret-suffixed names of every shape:
        "ANTHROPIC_API_KEY": "a",
        "GITHUB_TOKEN": "t",
        "DB_SECRET": "s",
        "ADMIN_PASSWORD": "p",
        # The provider key variable derived from the model prefix:
        # (model is anthropic/claude -> ANTHROPIC_API_KEY, already covered above)
    }
    tester, spawned = _make_tester(output=_ok_line(), environ=environ)
    tester.run(dict(_VALUES))

    env = spawned[0].kwargs["env"]
    nine = {
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
    for name in nine:
        assert name not in env
    for name in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "DB_SECRET", "ADMIN_PASSWORD"):
        assert name not in env, f"{name} (secret-suffixed) must be stripped"
    # PATH-like, non-secret variables are kept untouched.
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/u"
    assert env["LANG"] == "en_US.UTF-8"


def test_provider_key_variable_is_stripped_even_without_secret_suffix_match() -> None:
    # The provider variable derived from the model (anthropic -> ANTHROPIC_API_KEY)
    # is removed; a case-different spelling is matched case-insensitively.
    environ = {"PATH": "/usr/bin", "anthropic_api_key": "lower-shell-key"}
    tester, spawned = _make_tester(output=_ok_line(), environ=environ)
    tester.run(dict(_VALUES))
    env = spawned[0].kwargs["env"]
    assert "anthropic_api_key" not in env


# --- fixed messages never carry child output --------------------------------


def test_fixed_message_never_contains_child_output() -> None:
    chatty = "ERROR: secret-base https://evil.example/v1 said sk-LEAK boom\n"
    output = chatty + _fail_line("invalid_request", status=422)
    tester, _ = _make_tester(output=output)
    result = tester.run(dict(_VALUES))
    assert result["category"] == "invalid_request"
    message = result["message"]
    for leaked in ("secret-base", "evil.example", "sk-LEAK", "boom"):
        assert leaked not in message
    assert message.startswith("The provider rejected the request.")
    assert "HTTP 422" in message


# --- isolation: the viewer's own environment is never touched ---------------


def test_viewer_os_environ_is_unchanged_after_a_run() -> None:
    before = dict(os.environ)
    tester, _ = _make_tester(output=_ok_line(), environ={"PATH": "/usr/bin"})
    tester.run(dict(_VALUES))
    assert dict(os.environ) == before


# --- thread-safety smoke ----------------------------------------------------


def test_thread_safety_smoke_two_threads_do_not_both_run() -> None:
    # Two threads hammer a tester whose children complete instantly. The lock
    # must keep the busy flag and the rate window consistent: every call either
    # returns a result or raises a LaunchError, and nothing raises anything else.
    def popen(argv: list[str], **kwargs: Any) -> FakePopen:  # noqa: ARG001
        return FakePopen(argv, output=_ok_line(), exit_code=0)

    tester = ConnectionTester(
        popen=popen,
        clock=FakeClock(step=0.0),
        kill_tree=lambda _p: None,
        environ={"PATH": "/usr/bin"},
        boot="pass",
    )

    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(10):
            try:
                tester.run(dict(_VALUES))
                tag = "ok"
            except LaunchError as exc:
                tag = exc.code
            with lock:
                outcomes.append(tag)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(outcomes) == 20
    assert set(outcomes) <= {"ok", "test_in_progress", "rate_limited"}
