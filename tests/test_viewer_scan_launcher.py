"""Tests for the viewer ScanLauncher (Step 5; FR-16..FR-28, BR-18..BR-31).

Everything is driven through injected seams: a fake Popen (no real process), a
fake monotonic clock, a stub resolver and classifier (no DNS, no network) and a
recording kill_tree. The real ``run.json`` recency path is exercised with files
on ``tmp_path``.
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, cast

import pytest

from strix.interface.viewer.launch_errors import LaunchError
from strix.interface.viewer.launch_request import LaunchLimits, LaunchRequest
from strix.interface.viewer.scan_launcher import ProcessHandle, ScanLauncher, ScanState


if TYPE_CHECKING:
    from pathlib import Path


LIMITS = LaunchLimits()


def _ok_resolver(_host: str, _timeout: float) -> list[str]:
    # A public, non-private, non-metadata address so validation always passes.
    return ["93.184.216.34"]


def _web_classifier(_target: str) -> str:
    return "web_application"


class FakePopen:
    """A stand-in for subprocess.Popen the launcher can drive without a real child.

    ``exit_code`` is None while "alive"; call ``finish(code)`` to make ``poll``
    return it. ``send_signal`` and ``kill`` are recorded; ``stdout`` is a text
    stream pre-seeded with the child's output.
    """

    _next_pid = 10_000

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.argv = argv
        output = kwargs.pop("_output", "")
        self.kwargs = kwargs
        FakePopen._next_pid += 1
        self.pid = FakePopen._next_pid
        self._exit_code: int | None = None
        self.signals: list[int] = []
        self.killed = False
        self.stdout: io.StringIO | None = io.StringIO(output)

    def finish(self, code: int) -> None:
        self._exit_code = code

    def poll(self) -> int | None:
        return self._exit_code

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        if self._exit_code is None:
            self._exit_code = -9
        return self._exit_code

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def kill(self) -> None:
        self.killed = True
        if self._exit_code is None:
            self._exit_code = -9


class FakeClock:
    """A manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _request(**overrides: object) -> LaunchRequest:
    base: dict[str, Any] = {
        "target": "example.com",
        "scan_mode": "quick",
        "max_budget_usd": 5.0,
        "instruction": None,
        "authorized": True,
        "allow_private": False,
    }
    base.update(overrides)
    return LaunchRequest(**base)


def _make_launcher(
    runs_dir: Path,
    *,
    processes: list[FakePopen] | None = None,
    clock: FakeClock | None = None,
    kills: list[FakePopen] | None = None,
) -> tuple[ScanLauncher, list[FakePopen]]:
    spawned: list[FakePopen] = []
    pending = list(processes or [])

    def popen(argv: list[str], **kwargs: Any) -> FakePopen:
        proc = pending.pop(0) if pending else FakePopen(argv, **kwargs)
        proc.argv = argv
        proc.kwargs = kwargs
        spawned.append(proc)
        return proc

    def kill_tree(proc: ProcessHandle) -> None:
        if kills is not None:
            kills.append(cast("FakePopen", proc))
        proc.kill()

    launcher = ScanLauncher(
        runs_dir,
        LIMITS,
        popen=popen,
        clock=clock or FakeClock(),
        resolver=_ok_resolver,
        classifier=_web_classifier,
        kill_tree=kill_tree,
        environ={"PATH": "/usr/bin"},
    )
    return launcher, spawned


def _write_run_json(runs_dir: Path, name: str, *, status: str, age_seconds: float = 0.0) -> Path:
    run_dir = runs_dir / name
    run_dir.mkdir(parents=True, exist_ok=True)
    record = {"run_name": name, "status": status, "end_time": None}
    record_path = run_dir / "run.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    if age_seconds:
        when = time.time() - age_seconds
        os.utime(record_path, (when, when))
    return run_dir


# --- argv exactness ---------------------------------------------------------


def test_argv_uses_c_boot_and_equals_joined_single_args(tmp_path: Path) -> None:
    launcher, spawned = _make_launcher(tmp_path / "strix_runs")
    (tmp_path / "strix_runs").mkdir()

    run_name = launcher.launch(_request(instruction="focus on login"))

    assert len(spawned) == 1
    argv = spawned[0].argv
    assert argv[0] == sys.executable
    assert argv[1] == "-c"
    assert "from strix.interface.main import main; main()" in argv[2]
    assert "-n" in argv
    # Every value is one argv element attached with '='.
    assert "--target=https://example.com" in argv
    assert "--scan-mode=quick" in argv
    assert "--max-budget=5" in argv
    assert f"--run-name={run_name}" in argv
    assert "--instruction=focus on login" in argv
    # shell=False, no shell string anywhere.
    assert spawned[0].kwargs["shell"] is False


def test_argv_hostile_text_stays_one_opaque_argument(tmp_path: Path) -> None:
    launcher, spawned = _make_launcher(tmp_path / "strix_runs")
    (tmp_path / "strix_runs").mkdir()

    # A hostile instruction cannot introduce a new flag; it rides as one element.
    launcher.launch(_request(instruction="x --max-budget 999 ; rm -rf /"))

    argv = spawned[0].argv
    hostile = "--instruction=x --max-budget 999 ; rm -rf /"
    assert hostile in argv
    # The hostile budget flag never became its own element.
    assert "999" not in [a for a in argv if a != hostile]


def test_argv_omits_instruction_when_absent(tmp_path: Path) -> None:
    launcher, spawned = _make_launcher(tmp_path / "strix_runs")
    (tmp_path / "strix_runs").mkdir()
    launcher.launch(_request(instruction=None))
    assert not any(a.startswith("--instruction=") for a in spawned[0].argv)


def test_run_name_is_web_prefixed_hex(tmp_path: Path) -> None:
    launcher, _ = _make_launcher(tmp_path / "strix_runs")
    (tmp_path / "strix_runs").mkdir()
    run_name = launcher.launch(_request())
    assert run_name.startswith("web-")
    assert len(run_name) == len("web-") + 12


# --- active-scan check ------------------------------------------------------


def test_scan_active_from_live_child(tmp_path: Path) -> None:
    launcher, _ = _make_launcher(tmp_path / "strix_runs")
    (tmp_path / "strix_runs").mkdir()
    first = launcher.launch(_request())
    # The first child is still alive (poll None), so a second launch is 409.
    with pytest.raises(LaunchError) as exc:
        launcher.launch(_request())
    assert exc.value.code == "scan_active"
    assert exc.value.status == 409
    assert exc.value.extra["run"] == first


def test_live_child_check_never_reads_run_json(tmp_path: Path) -> None:
    runs = tmp_path / "strix_runs"
    launcher, spawned = _make_launcher(runs)
    runs.mkdir()
    run_name = launcher.launch(_request())
    # No run.json exists for the just-launched child; the table handles it.
    assert not (runs / run_name / "run.json").exists()
    assert launcher.active_run() == run_name
    assert spawned[0].poll() is None


def test_scan_active_from_fresh_running_run_json(tmp_path: Path) -> None:
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    _write_run_json(runs, "cli-run", status="running", age_seconds=10.0)
    launcher, _ = _make_launcher(runs)
    assert launcher.active_run() == "cli-run"
    with pytest.raises(LaunchError) as exc:
        launcher.launch(_request())
    assert exc.value.code == "scan_active"


def test_stale_running_run_json_is_ignored(tmp_path: Path) -> None:
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    # Older than the 300 s default window -> not active.
    _write_run_json(runs, "old-run", status="running", age_seconds=10_000.0)
    launcher, _ = _make_launcher(runs)
    assert launcher.active_run() is None
    run_name = launcher.launch(_request())
    assert run_name.startswith("web-")


def test_unreadable_run_json_is_skipped(tmp_path: Path) -> None:
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    bad = runs / "broken"
    bad.mkdir()
    (bad / "run.json").write_text("{not json", encoding="utf-8")
    launcher, _ = _make_launcher(runs)
    assert launcher.active_run() is None


def test_two_simultaneous_launches_start_exactly_one_child(tmp_path: Path) -> None:
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    launcher, spawned = _make_launcher(runs)

    results: list[str] = []
    errors: list[LaunchError] = []
    barrier = threading.Barrier(2)

    def attempt() -> None:
        barrier.wait()
        try:
            results.append(launcher.launch(_request()))
        except LaunchError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(spawned) == 1
    assert len(results) == 1
    assert len(errors) == 1
    assert errors[0].code == "scan_active"


# --- rate limiting ----------------------------------------------------------


def test_rate_limit_five_per_minute(tmp_path: Path) -> None:
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    clock = FakeClock()
    # Each launch after the first is a 409 (one child stays live), yet every
    # attempt still counts toward the rate window.
    launcher, _ = _make_launcher(runs, clock=clock)
    launcher.launch(_request())  # attempt 1 spawns
    for _ in range(4):  # attempts 2-5 hit scan_active but still count
        with pytest.raises(LaunchError) as exc:
            launcher.launch(_request())
        assert exc.value.code == "scan_active"
    # The 6th within the window is rate-limited before validation.
    with pytest.raises(LaunchError) as exc:
        launcher.launch(_request())
    assert exc.value.code == "rate_limited"
    assert exc.value.status == 429

    # After the window slides, attempts are allowed again.
    clock.advance(61.0)
    with pytest.raises(LaunchError) as exc:
        launcher.launch(_request())
    assert exc.value.code == "scan_active"


# --- status / exit-code mapping ---------------------------------------------


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [(0, ScanState.FINISHED), (2, ScanState.FINISHED), (1, ScanState.FAILED)],
)
def test_exit_code_mapping(tmp_path: Path, exit_code: int, expected: ScanState) -> None:
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    launcher, spawned = _make_launcher(runs)
    run_name = launcher.launch(_request())
    spawned[0].finish(exit_code)
    status = launcher.status(run_name)
    assert status["state"] == expected.value
    assert status["exit_code"] == exit_code
    assert status["stoppable"] is False


def test_status_unknown_for_non_launched_run(tmp_path: Path) -> None:
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    launcher, _ = _make_launcher(runs)
    status = launcher.status("cli-run")
    assert status == {
        "state": "unknown",
        "exit_code": None,
        "stoppable": False,
        "error_tail": None,
    }


def test_running_child_is_stoppable(tmp_path: Path) -> None:
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    launcher, _ = _make_launcher(runs)
    run_name = launcher.launch(_request())
    status = launcher.status(run_name)
    assert status["stoppable"] is True
    assert status["state"] in ("starting", "running")


def test_failure_tail_is_redacted_and_bounded(tmp_path: Path) -> None:
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    leaking = FakePopen([], _output="connecting with sk-ABCDEFGHIJKLMNOPQRSTUVWX\nboom\n")
    launcher, spawned = _make_launcher(runs, processes=[leaking])
    run_name = launcher.launch(_request())
    # Let the daemon reader drain the seeded output.
    time.sleep(0.1)
    spawned[0].finish(1)
    status = launcher.status(run_name)
    assert status["state"] == "failed"
    tail = status["error_tail"]
    assert tail is not None
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in tail
    assert "[REDACTED]" in tail
    assert len(tail) <= 4 * 1024


# --- stop -------------------------------------------------------------------


def test_stop_graceful_signals_then_exits(tmp_path: Path) -> None:
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    kills: list[FakePopen] = []
    clock = FakeClock()
    launcher, spawned = _make_launcher(runs, clock=clock, kills=kills)
    run_name = launcher.launch(_request())

    result = launcher.stop(run_name)
    assert result == {"state": "stopping"}

    proc = spawned[0]
    # The child exits gracefully before the 30 s grace, so it is never killed.
    proc.finish(0)
    time.sleep(0.2)
    assert not proc.killed
    if sys.platform == "win32":
        # Windows delivers CTRL_BREAK_EVENT via send_signal.
        assert signal.CTRL_BREAK_EVENT in proc.signals


def test_stop_forced_kill_reconciles_run_json_after_reap(tmp_path: Path) -> None:
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    kills: list[FakePopen] = []
    clock = FakeClock()
    launcher, spawned = _make_launcher(runs, clock=clock, kills=kills)
    run_name = launcher.launch(_request())
    # The child wrote a 'running' run.json and then ignores the stop signal.
    _write_run_json(runs, run_name, status="running")

    launcher.stop(run_name)
    # Drive past the 30 s grace so the watcher force-kills.
    for _ in range(40):
        clock.advance(1.0)
        time.sleep(0.01)
    # Give the watcher thread time to kill + reconcile.
    deadline = time.time() + 3.0
    while time.time() < deadline and not kills:
        time.sleep(0.02)

    assert kills == [spawned[0]]
    assert spawned[0].killed is True
    record = json.loads((runs / run_name / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "interrupted"
    assert record["end_time"]


def test_stop_unknown_run_is_not_found(tmp_path: Path) -> None:
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    launcher, _ = _make_launcher(runs)
    with pytest.raises(LaunchError) as exc:
        launcher.stop("cli-run")
    assert exc.value.code == "not_found"
    assert exc.value.status == 404


@pytest.mark.skipif(sys.platform != "win32", reason="send_signal OSError path is Windows-specific")
def test_stop_send_failure_falls_through_to_kill(tmp_path: Path) -> None:
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    kills: list[FakePopen] = []
    clock = FakeClock()

    class RefusingPopen(FakePopen):
        def send_signal(self, sig: int) -> None:  # noqa: ARG002 - matches the callee signature
            raise OSError("no console to deliver the event")

    refuser = RefusingPopen([])
    launcher, spawned = _make_launcher(runs, processes=[refuser], clock=clock, kills=kills)
    run_name = launcher.launch(_request())

    launcher.stop(run_name)
    # With the graceful signal refused, grace is 0 and the kill is immediate.
    deadline = time.time() + 3.0
    while time.time() < deadline and not kills:
        time.sleep(0.02)
    assert spawned[0].killed is True


def test_shutdown_stops_all_children_in_parallel(tmp_path: Path) -> None:
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    kills: list[FakePopen] = []
    clock = FakeClock()
    launcher, spawned = _make_launcher(runs, clock=clock, kills=kills)

    # Launch one, finish it, launch another so two children exist with one live.
    first = launcher.launch(_request())
    spawned[0].finish(0)
    launcher.status(first)  # fold into FINISHED so the slot frees
    launcher.launch(_request())
    assert len(spawned) == 2

    # The second child ignores the signal; shutdown must force-kill it.
    def driver() -> None:
        for _ in range(40):
            clock.advance(1.0)
            time.sleep(0.01)

    thread = threading.Thread(target=driver)
    thread.start()
    launcher.shutdown()
    thread.join()

    assert spawned[1].killed is True


# --- audit line -------------------------------------------------------------


def test_audit_line_excludes_instruction(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    launcher, _ = _make_launcher(runs)
    with caplog.at_level("INFO", logger="strix.interface.viewer.scan_launcher"):
        launcher.launch(_request(instruction="SECRET-INSTRUCTION-TEXT"))
    audit = "\n".join(record.message for record in caplog.records)
    assert "viewer launch" in audit
    assert "SECRET-INSTRUCTION-TEXT" not in audit
    # The audit line records the resolved address (NFR-7, BR-31).
    assert "93.184.216.34" in audit


class _NeverReapsPopen(FakePopen):
    """A child that survives kill(): wait() times out like a stuck process."""

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired("fake", timeout or 0.0)


def test_stop_forced_kill_skips_reconcile_when_child_not_reaped(tmp_path: Path) -> None:
    # BR-28: the run record is rewritten only after the child is reaped; a child
    # that is still alive may still be writing it, so it must be left alone.
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    stuck = _NeverReapsPopen(["x"])
    clock = FakeClock()
    launcher, _ = _make_launcher(runs, processes=[stuck], clock=clock)
    run_name = launcher.launch(_request())
    _write_run_json(runs, run_name, status="running")

    launcher.stop(run_name)
    deadline = time.time() + 3.0
    while time.time() < deadline and not stuck.killed:
        clock.advance(5.0)
        time.sleep(0.02)
    time.sleep(0.2)

    assert stuck.killed is True
    record = json.loads((runs / run_name / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "running"


# --- Step 3.1: child_env_builder + pre_launch_check hooks (FR-16..FR-18, NFR-5) ---


def _make_launcher_with_hooks(
    runs_dir: Path,
    *,
    child_env_builder: Any = None,
    pre_launch_check: Any = None,
    processes: list[FakePopen] | None = None,
    clock: FakeClock | None = None,
) -> tuple[ScanLauncher, list[FakePopen]]:
    """Like ``_make_launcher`` but also wires the two settings-store hooks."""
    spawned: list[FakePopen] = []
    pending = list(processes or [])

    def popen(argv: list[str], **kwargs: Any) -> FakePopen:
        proc = pending.pop(0) if pending else FakePopen(argv, **kwargs)
        proc.argv = argv
        proc.kwargs = kwargs
        spawned.append(proc)
        return proc

    launcher = ScanLauncher(
        runs_dir,
        LIMITS,
        popen=popen,
        clock=clock or FakeClock(),
        resolver=_ok_resolver,
        classifier=_web_classifier,
        environ={"PATH": "/usr/bin", "STRIX_LLM": "shell-model", "LLM_API_KEY": "shell-key"},
        child_env_builder=child_env_builder,
        pre_launch_check=pre_launch_check,
    )
    return launcher, spawned


def test_child_env_builder_called_per_spawn_sees_change_between_launches(tmp_path: Path) -> None:
    # FR-17/NFR-5: the builder is evaluated at EACH spawn, so a settings change
    # made between two launches is picked up by the next one with no restart.
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    state = {"MODEL": "first"}
    calls: list[dict[str, str]] = []

    def builder(_base: Any) -> dict[str, str]:
        # Snapshot the mutable state at call time; this is what proves the
        # builder runs per spawn, not once at start-up.
        env = {"PATH": "/usr/bin", "STRIX_LLM": state["MODEL"]}
        calls.append(env)
        return env

    launcher, spawned = _make_launcher_with_hooks(runs, child_env_builder=builder)

    first = launcher.launch(_request())
    spawned[0].finish(0)
    launcher.status(first)  # fold into FINISHED so the slot frees

    state["MODEL"] = "second"  # a save happened between the two launches
    launcher.launch(_request())

    assert len(calls) == 2
    assert spawned[0].kwargs["env"]["STRIX_LLM"] == "first"
    assert spawned[1].kwargs["env"]["STRIX_LLM"] == "second"


def test_child_env_builder_stripped_vars_absent_from_child_env(tmp_path: Path) -> None:
    # FR-16: a managed builder removes the LLM variables; the fake child's env
    # (what we passed to Popen) must not contain them. The builder receives the
    # live os.environ, so we seed and strip a probe variable on it.
    runs = tmp_path / "strix_runs"
    runs.mkdir()

    def builder(base: Any) -> dict[str, str]:
        strip = {"STRIX_LLM", "LLM_API_KEY", "STRIX_STRIP_PROBE"}
        return {k: v for k, v in base.items() if k.upper() not in strip}

    launcher, spawned = _make_launcher_with_hooks(runs, child_env_builder=builder)
    os.environ["STRIX_STRIP_PROBE"] = "leak-me"
    os.environ["STRIX_KEPT_PROBE"] = "keep-me"
    try:
        launcher.launch(_request())
    finally:
        os.environ.pop("STRIX_STRIP_PROBE", None)
        os.environ.pop("STRIX_KEPT_PROBE", None)

    env = spawned[0].kwargs["env"]
    assert "STRIX_LLM" not in env
    assert "LLM_API_KEY" not in env
    assert "STRIX_STRIP_PROBE" not in env
    assert env["STRIX_KEPT_PROBE"] == "keep-me"


def test_child_env_builder_reads_os_environ_at_call_time(tmp_path: Path) -> None:
    # The builder receives os.environ (read at the spawn), not the launcher's
    # start-up snapshot, so it can strip whatever the process currently holds.
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    seen: list[str] = []

    def builder(base: Any) -> dict[str, str]:
        seen.append(base.get("STRIX_HOOK_PROBE", "<unset>"))
        return {"PATH": base.get("PATH", "")}

    launcher, _ = _make_launcher_with_hooks(runs, child_env_builder=builder)
    os.environ["STRIX_HOOK_PROBE"] = "live-value"
    try:
        launcher.launch(_request())
    finally:
        os.environ.pop("STRIX_HOOK_PROBE", None)

    assert seen == ["live-value"]


def test_no_builder_passes_through_start_up_environ(tmp_path: Path) -> None:
    # FR-18: with no builder the child gets the launcher's start-up environ
    # exactly as before this feature.
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    launcher, spawned = _make_launcher_with_hooks(runs, child_env_builder=None)
    launcher.launch(_request())

    env = spawned[0].kwargs["env"]
    assert env["STRIX_LLM"] == "shell-model"
    assert env["LLM_API_KEY"] == "shell-key"
    assert env["PATH"] == "/usr/bin"


def test_pre_launch_check_refusal_means_no_spawn_and_no_active_child(tmp_path: Path) -> None:
    # DD-9/BR-36: a refused pre-launch check raises its LaunchError unchanged and
    # nothing is spawned, so no child is registered or left active.
    runs = tmp_path / "strix_runs"
    runs.mkdir()

    def refuse() -> None:
        raise LaunchError("llm_base_blocked", "the saved base resolves to metadata", status=400)

    launcher, spawned = _make_launcher_with_hooks(runs, pre_launch_check=refuse)

    with pytest.raises(LaunchError) as exc:
        launcher.launch(_request())
    assert exc.value.code == "llm_base_blocked"
    assert exc.value.status == 400
    assert spawned == []
    assert launcher.active_run() is None


def test_pre_launch_check_refusal_does_not_consume_a_rate_slot(tmp_path: Path) -> None:
    # The check runs before the rate limit is recorded, so repeated refusals do
    # not exhaust the window: a later allowed launch still succeeds.
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    blocked = {"on": True}

    def maybe_refuse() -> None:
        if blocked["on"]:
            raise LaunchError("llm_base_blocked", "blocked", status=400)

    launcher, spawned = _make_launcher_with_hooks(runs, pre_launch_check=maybe_refuse)

    # Ten refusals: if any had consumed a rate slot, the 6th+ would turn into
    # rate_limited instead of llm_base_blocked.
    for _ in range(10):
        with pytest.raises(LaunchError) as exc:
            launcher.launch(_request())
        assert exc.value.code == "llm_base_blocked"

    blocked["on"] = False
    run_name = launcher.launch(_request())
    assert run_name.startswith("web-")
    assert len(spawned) == 1


def test_pre_launch_check_runs_before_the_spawn(tmp_path: Path) -> None:
    # A passing check does not block a normal launch, and it is invoked exactly
    # once per launch.
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    calls = {"n": 0}

    def ok() -> None:
        calls["n"] += 1

    launcher, spawned = _make_launcher_with_hooks(runs, pre_launch_check=ok)
    launcher.launch(_request())
    assert calls["n"] == 1
    assert len(spawned) == 1


def test_builder_exception_fails_launch_with_spawn_failed(tmp_path: Path) -> None:
    # Documented decision: a builder failure FAILS the launch (spawn_failed, 500)
    # rather than falling back to the unstripped viewer environment, which would
    # leak the very secrets the strip set removes (FR-16).
    runs = tmp_path / "strix_runs"
    runs.mkdir()

    def broken_builder(_base: Any) -> dict[str, str]:
        raise RuntimeError("store read blew up")

    launcher, spawned = _make_launcher_with_hooks(runs, child_env_builder=broken_builder)

    with pytest.raises(LaunchError) as exc:
        launcher.launch(_request())
    assert exc.value.code == "spawn_failed"
    assert exc.value.status == 500
    assert spawned == []
    assert launcher.active_run() is None


def test_builder_launch_error_propagates_unchanged(tmp_path: Path) -> None:
    # A LaunchError the store raises (e.g. config_unreadable) keeps its own code
    # and status; it is not re-wrapped as spawn_failed.
    runs = tmp_path / "strix_runs"
    runs.mkdir()

    def builder(_base: Any) -> dict[str, str]:
        raise LaunchError("config_unreadable", "existing file is not valid JSON", status=409)

    launcher, spawned = _make_launcher_with_hooks(runs, child_env_builder=builder)

    with pytest.raises(LaunchError) as exc:
        launcher.launch(_request())
    assert exc.value.code == "config_unreadable"
    assert exc.value.status == 409
    assert spawned == []
