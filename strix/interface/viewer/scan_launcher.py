"""The viewer's launch service: spawn, watch, stop and reconcile scan children.

``ScanLauncher`` owns the in-memory table of children this viewer process
started, the single launch lock that serialises the active-scan check with the
spawn (so two near-simultaneous requests cannot both start a scan, FR-24), the
sliding-window rate limiter (NFR-4), and the graceful-then-force stop sequence
(FR-26, FR-28). Every operating-system seam the design names (``popen``,
``clock``, ``resolver``, ``classifier``, ``kill_tree``, ``environ``) is
injected so the tests spawn no real process and perform no real DNS or network
I/O (NFR-11).

The live table is authoritative for a viewer-launched child: the active check
consults process handles first and never reads ``run.json`` for them (BR-18),
so a crashed child frees the slot at once. ``run.json`` recency is consulted
only for runs this viewer did not launch (BR-19), honouring the project rule
that a ``running`` status is never trusted on its own.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Protocol

from strix.core.paths import run_record_path
from strix.interface.viewer.launch_errors import LaunchError
from strix.interface.viewer.redaction import redact
from strix.interface.viewer.target_validation import (
    Classifier,
    Resolver,
    validate_target,
)


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from strix.interface.viewer.launch_request import LaunchLimits, LaunchRequest


logger = logging.getLogger(__name__)

# The boot string run via ``python -c`` so the child starts as ``strix`` without
# the runpy warning ``-m`` would print to stderr and pollute the failure tail
# (DD-9). ``sys.argv[0]`` is rewritten so the program name reads ``strix``.
_BOOT = "import sys; sys.argv[0] = 'strix'; from strix.interface.main import main; main()"

# Output ring-buffer bounds: the last 50 lines or 4 KiB, whichever is smaller,
# of the child's combined stdout+stderr (BR-24).
_RING_MAX_LINES = 50
_RING_MAX_BYTES = 4 * 1024

# Rate limit: at most 5 launch attempts per rolling 60 seconds per process (BR-4).
_RATE_LIMIT = 5
_RATE_WINDOW_SECONDS = 60.0

# Graceful-stop grace period before a force kill (FR-26, BR-26).
_STOP_GRACE_SECONDS = 30.0

# Active-scan recency window for runs this viewer did not launch (BR-19). The
# default is 300 s; the override floor is 30 s so a mistyped tiny value cannot
# make every quiet-but-healthy scan look inactive.
_DEFAULT_ACTIVE_WINDOW_SECONDS = 300.0
_MIN_ACTIVE_WINDOW_SECONDS = 30.0

# Run-name scheme: ``web-`` + 12 hex chars, retried on the rare collision (section 5).
_RUN_NAME_RETRIES = 5
_RUN_NAME_HEX_BYTES = 6

# How long to wait for the reader thread to drain at reap/shutdown before
# abandoning it (the pipe closes at child exit, so this is a safety bound).
_READER_JOIN_TIMEOUT = 2.0


class ScanState(StrEnum):
    """Lifecycle of a viewer-launched child (domain-entities ScanState)."""

    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FINISHED = "finished"
    FAILED = "failed"
    STOPPED = "stopped"


_TERMINAL_STATES = frozenset({ScanState.FINISHED, ScanState.FAILED, ScanState.STOPPED})


@dataclass(frozen=True)
class ScanSnapshot:
    """A point-in-time, read-only view of one launched child (for observers).

    Carries no instruction text and no process handle, so the notification
    service can hold it without being able to affect or leak the scan.
    """

    run_name: str
    target: str
    mode: str
    state: str
    exit_code: int | None


class ProcessHandle(Protocol):
    """The subprocess surface the launcher uses; lets tests inject a fake Popen.

    Only the members the launcher touches are declared, so a test double needs
    just these — no real process is required (NFR-11).
    """

    @property
    def pid(self) -> int: ...

    @property
    def stdout(self) -> IO[str] | None: ...

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = ...) -> int: ...

    def send_signal(self, sig: int) -> None: ...

    def kill(self) -> None: ...


# Alias kept for readability at call sites and in the injected-seam signatures.
PopenLike = ProcessHandle


@dataclass
class ScanChild:
    """One launched child and everything the status/stop paths need about it."""

    run_name: str
    process: PopenLike
    target: str
    mode: str
    started_at: float
    state: ScanState = ScanState.STARTING
    stop_requested: bool = False
    _output: deque[str] = field(default_factory=lambda: deque(maxlen=_RING_MAX_LINES))
    _reader: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # Guards ``state`` / ``stop_requested`` transitions, which the status
    # endpoint, the stop request and the stop watcher thread can all drive.
    state_lock: threading.RLock = field(default_factory=threading.RLock)

    def append_output(self, line: str) -> None:
        """Record one output line, trimming the ring to its byte budget too."""
        with self._lock:
            self._output.append(line)
            # Trim oldest lines until the joined buffer fits the byte budget.
            while len(self._output) > 1 and sum(len(ln) for ln in self._output) > _RING_MAX_BYTES:
                self._output.popleft()

    def output_tail(self) -> str:
        """The current ring contents joined, trimmed to the byte budget, redacted."""
        with self._lock:
            text = "".join(self._output)
        if len(text) > _RING_MAX_BYTES:
            text = text[-_RING_MAX_BYTES:]
        return redact(text)


class ScanLauncher:
    """Owns launched scan children for one viewer process (section 1, BR-18..BR-28)."""

    def __init__(
        self,
        runs_dir: Path,
        limits: LaunchLimits,
        *,
        popen: Callable[..., PopenLike] | None = None,
        clock: Callable[[], float] | None = None,
        resolver: Resolver | None = None,
        classifier: Classifier | None = None,
        kill_tree: Callable[[PopenLike], None] | None = None,
        environ: Mapping[str, str] | None = None,
        child_env_builder: Callable[[Mapping[str, str]], Mapping[str, str]] | None = None,
        pre_launch_check: Callable[[], None] | None = None,
    ) -> None:
        self._runs_dir = runs_dir
        self._limits = limits
        self._popen = popen or subprocess.Popen
        self._clock = clock or time.monotonic
        self._resolver = resolver
        self._classifier = classifier
        self._kill_tree = kill_tree or _default_kill_tree
        self._environ = dict(environ) if environ is not None else dict(os.environ)
        # The settings store hands us these at wiring time (cli.py). ``child_env_builder``
        # is evaluated at EACH spawn (FR-17) so a save between two launches is seen by
        # the next one with no restart; ``pre_launch_check`` runs the managed base-URL
        # DNS-rebinding guard (DD-9/BR-36) before any lock or rate-limit side effect.
        self._child_env_builder = child_env_builder
        self._pre_launch_check = pre_launch_check

        self._children: dict[str, ScanChild] = {}
        self._lock = threading.Lock()
        self._attempts: deque[float] = deque()
        self._watchers: list[threading.Thread] = []
        self._active_window = _resolve_active_window(self._environ)

    @property
    def limits(self) -> LaunchLimits:
        """The launch limits this launcher enforces (read by the server's form API)."""
        return self._limits

    # -- launch ------------------------------------------------------------

    def launch(self, request: LaunchRequest) -> str:
        """Validate, check for an active scan, and spawn one child; return its run name.

        Target validation and DNS happen *before* the launch lock so a slow
        lookup never holds it (section 2). The active check and the spawn run
        under the lock, and the child is registered *before* the lock is
        released, so a second request waiting on the lock sees it even though
        its ``run.json`` does not exist yet (FR-24).

        Order chosen for ``pre_launch_check`` (the managed base-URL rebinding
        guard, DD-9/BR-36): it runs *first*, before the rate limit is consumed
        and before the launch lock is taken. A request that is refused for a
        blocked base therefore does not burn a rate slot and never holds the
        lock across its 3-second DNS bound. Any :class:`LaunchError` it raises
        propagates unchanged. It is called exactly once per ``launch`` and not
        at all for a request rejected earlier (the rate-limit 429), because that
        rejection happens after this point.

        Lock order: the launch lock (``self._lock``) is the OUTER lock; the
        settings-store lock, taken inside ``child_env_builder`` during
        ``_spawn``, is the INNER lock. The store never takes the launch lock
        (BR-35), so the two can never deadlock.
        """
        if self._pre_launch_check is not None:
            # Runs before any side effect (rate limit, lock) so a blocked base
            # neither consumes a slot nor holds the lock; LaunchError propagates.
            self._pre_launch_check()

        self._record_attempt()

        validated = validate_target(
            request.target,
            allow_private=request.allow_private,
            resolver=self._resolver,
            classifier=self._classifier,
        )

        with self._lock:
            # Lock order: this launch lock is OUTER. ``_spawn`` below calls the
            # child-env builder, which takes the settings-store lock (INNER). The
            # store never acquires this launch lock, so the nesting is deadlock-free
            # (BR-35).
            active = self._active_run_locked()
            if active is not None:
                raise LaunchError(
                    "scan_active",
                    "another scan is already running",
                    status=409,
                    extra={"run": active},
                )
            run_name = self._choose_run_name()
            child = self._spawn(run_name, validated.normalized, request)
            self._children[run_name] = child

        # The audit line is written after the child exists but carries no
        # instruction text (BR-31). Outside the lock: it is pure logging.
        budget_display = (
            f"{request.max_budget_usd:g}" if request.max_budget_usd is not None else "none"
        )
        logger.info(
            "viewer launch run=%s target=%s addresses=%s mode=%s budget=%s allow_private=%s",
            run_name,
            validated.normalized,
            ",".join(validated.addresses),
            request.scan_mode,
            budget_display,
            request.allow_private,
        )
        return run_name

    def _spawn(self, run_name: str, target: str, request: LaunchRequest) -> ScanChild:
        """Build the argv and start the child; raise ``spawn_failed`` on OS refusal.

        The child's environment is taken from ``child_env_builder`` when one was
        supplied, evaluated here at *every* spawn (never from a start-up snapshot)
        so a settings save between two launches is picked up by the next one with
        no restart (FR-17). The builder is called with ``os.environ`` read now, and
        it takes the settings-store lock (the INNER lock; this runs while the launch
        lock is held — see ``launch`` for the order).

        Builder-exception decision: a builder failure FAILS the launch with
        ``spawn_failed`` rather than falling back to the raw viewer environment.
        For a managed launch the builder is what strips the viewer's own LLM
        secrets from the child (FR-16); silently using the unstripped environment
        would leak exactly what the strip set is meant to remove, so refusing is
        the safe choice.
        """
        if self._child_env_builder is not None:
            try:
                env: Mapping[str, str] = dict(self._child_env_builder(os.environ))
            except LaunchError:
                # A LaunchError from the store (e.g. config_unreadable) already
                # carries a stable code/status; let it surface unchanged.
                raise
            except Exception as exc:
                raise LaunchError(
                    "spawn_failed",
                    "the scan environment could not be prepared",
                    status=500,
                ) from exc
        else:
            env = self._environ
        argv = self._build_argv(run_name, target, request)
        try:
            process = self._popen(
                argv,
                cwd=str(self._runs_dir.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                env=env,
                text=True,
                **_process_group_kwargs(),
            )
        except OSError as exc:
            raise LaunchError(
                "spawn_failed", "the scan process could not be started", status=500
            ) from exc

        child = ScanChild(
            run_name=run_name,
            process=process,
            target=target,
            mode=request.scan_mode,
            started_at=self._clock(),
        )
        reader = threading.Thread(
            target=self._drain_output,
            args=(child,),
            name=f"viewer-reader-{run_name}",
            daemon=True,
        )
        child._reader = reader
        reader.start()
        return child

    def _build_argv(self, run_name: str, target: str, request: LaunchRequest) -> list[str]:
        """Compose the exact child command (section 6).

        Every value is attached to its flag with ``=`` in one argv element, so a
        value that begins with ``-`` can never be read as a new option (FR-17).
        """
        scan_args = [
            "-n",
            f"--target={target}",
            f"--scan-mode={request.scan_mode}",
        ]
        # A None budget means "no limit": omit the flag so the child runs
        # uncapped, exactly as the CLI does when --max-budget is not given.
        if request.max_budget_usd is not None:
            scan_args.append(f"--max-budget={request.max_budget_usd:g}")
        scan_args.append(f"--run-name={run_name}")
        if request.instruction:
            scan_args.append(f"--instruction={request.instruction}")

        if getattr(sys, "frozen", False):
            # A frozen (PyInstaller) build is itself the strix entry point.
            return [sys.executable, *scan_args]
        return [sys.executable, "-c", _BOOT, *scan_args]

    def _choose_run_name(self) -> str:
        """``web-<12 hex>``, confirmed not to exist on disk, retried on collision."""
        for _ in range(_RUN_NAME_RETRIES):
            candidate = f"web-{secrets.token_hex(_RUN_NAME_HEX_BYTES)}"
            if candidate in self._children:
                continue
            if not (self._runs_dir / candidate).exists():
                return candidate
        raise LaunchError("spawn_failed", "could not allocate a unique run name", status=500)

    # -- active check ------------------------------------------------------

    def active_run(self) -> str | None:
        """The name of the active scan, or None. Acquires the launch lock."""
        with self._lock:
            return self._active_run_locked()

    def _active_run_locked(self) -> str | None:
        """Active-scan check (section 4). Caller must hold ``self._lock``.

        Rule 1: a live child in the table wins immediately and ``run.json`` is
        never read for it (BR-18). Rule 2: only then is any other run judged by
        ``run.json`` status + recency (BR-19).
        """
        for name, child in self._children.items():
            if child.process.poll() is None:
                return name
        return self._active_run_from_disk()

    def _active_run_from_disk(self) -> str | None:
        """A non-launched run counts as active on ``running`` + recent mtime (BR-19)."""
        if not self._runs_dir.is_dir():
            return None
        now = time.time()
        for run_dir in self._runs_dir.iterdir():
            record_path = run_record_path(run_dir)
            try:
                raw = record_path.read_text(encoding="utf-8")
                record = json.loads(raw)
                mtime = record_path.stat().st_mtime
            except (OSError, ValueError):
                # Unreadable or malformed run.json is skipped, never an error.
                continue
            if not isinstance(record, dict) or record.get("status") != "running":
                continue
            if now - mtime <= self._active_window:
                return str(record.get("run_name") or run_dir.name)
        return None

    # -- status ------------------------------------------------------------

    def status(self, run_name: str) -> dict[str, Any]:
        """The status response for a run (domain-entities §3, section 7).

        A run this viewer never launched (including every CLI-started run)
        returns the ``unknown``, non-stoppable shape so the UI falls back to the
        ordinary ``/api/run`` data.
        """
        child = self._children.get(run_name)
        if child is None:
            return {"state": "unknown", "exit_code": None, "stoppable": False, "error_tail": None}

        exit_code = self._reap(child)
        stoppable = child.state in (ScanState.STARTING, ScanState.RUNNING)
        error_tail = child.output_tail() if child.state is ScanState.FAILED else None
        return {
            "state": child.state.value,
            "exit_code": exit_code,
            "stoppable": stoppable,
            "error_tail": error_tail,
        }

    def snapshot(self) -> list[ScanSnapshot]:
        """A read-only view of every child this viewer launched, with states brought up to date.

        Used by the notification service to notice starts and exits without any
        browser polling ``/api/scans/status``. Each child is reaped first (the
        same idempotent fold the status endpoint performs), so a child that
        exited since the last look is reported in its terminal state. The launch
        lock is held only to copy the table, never while reaping.
        """
        with self._lock:
            children = list(self._children.values())
        snapshots: list[ScanSnapshot] = []
        for child in children:
            exit_code = self._reap(child)
            snapshots.append(
                ScanSnapshot(
                    run_name=child.run_name,
                    target=child.target,
                    mode=child.mode,
                    state=child.state.value,
                    exit_code=exit_code,
                )
            )
        return snapshots

    def _reap(self, child: ScanChild) -> int | None:
        """Fold a finished process into a terminal state; return its exit code or None.

        Exit code 0 or 2 maps to FINISHED (2 is the CLI's "vulnerabilities
        found" contract). Any other code is FAILED unless a stop was requested,
        in which case it is STOPPED (BR-23).
        """
        with child.state_lock:
            if child.state in _TERMINAL_STATES:
                return child.process.poll()
            exit_code = child.process.poll()
            if exit_code is None:
                # Still alive: STARTING advances to RUNNING once run.json is readable.
                if (
                    child.state is ScanState.STARTING
                    and run_record_path(self._runs_dir / child.run_name).is_file()
                ):
                    child.state = ScanState.RUNNING
                return None
            if child.stop_requested or child.state is ScanState.STOPPING:
                child.state = ScanState.STOPPED
            elif exit_code in (0, 2):
                child.state = ScanState.FINISHED
            else:
                child.state = ScanState.FAILED
            return exit_code

    # -- stop and shutdown -------------------------------------------------

    def stop(self, run_name: str) -> dict[str, str]:
        """Request a graceful stop of a live child; 404 for anything else (FR-27, BR-25).

        Returns ``{"state": "stopping"}`` and runs the grace/force/reconcile
        sequence on a watcher thread, so the HTTP call returns at once.
        """
        child = self._children.get(run_name)
        if child is None or child.process.poll() is not None:
            raise LaunchError("not_found", "no live scan with that name", status=404)
        with child.state_lock:
            if child.state is ScanState.STOPPING:
                return {"state": "stopping"}
            child.stop_requested = True
            child.state = ScanState.STOPPING
        delivered = self._signal_graceful(child)
        # A refused graceful signal (no console to deliver it) skips the grace
        # wait and force-kills immediately (section 8 step 4).
        grace = _STOP_GRACE_SECONDS if delivered else 0.0

        watcher = threading.Thread(
            target=self._await_stop,
            args=(child,),
            kwargs={"grace": grace},
            name=f"viewer-stop-{run_name}",
            daemon=True,
        )
        self._watchers.append(watcher)
        watcher.start()
        return {"state": "stopping"}

    def _signal_graceful(self, child: ScanChild) -> bool:
        """Send the Ctrl-C-equivalent to the child's own group (section 8 step 4).

        Returns False when the OS refuses to deliver the signal (for example the
        viewer has no console on Windows); the caller then skips the grace wait
        and force-kills. A refusal is not an error.
        """
        try:
            if sys.platform == "win32":
                child.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(child.process.pid), signal.SIGINT)
        except OSError:
            logger.debug("graceful stop signal refused for %s; forcing", child.run_name)
            return False
        return True

    def _await_stop(self, child: ScanChild, *, grace: float | None = None) -> None:
        """Wait the grace period, force-kill if still alive, reconcile after reap."""
        deadline = self._clock() + (grace if grace is not None else _STOP_GRACE_SECONDS)
        # If the graceful signal was refused, grace is 0 and we fall straight
        # through to the kill.
        while self._clock() < deadline:
            if child.process.poll() is not None:
                break
            time.sleep(0.05)

        if child.process.poll() is None:
            self._kill_tree(child.process)

        # Reconcile ONLY after the child has been reaped, so the scan process
        # (the only writer of its own run.json) can no longer overwrite us. If
        # the wait times out the child may still be alive and writing, so the
        # run record is left alone rather than risk clobbering a real status.
        reaped = True
        try:
            child.process.wait(timeout=_STOP_GRACE_SECONDS)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            reaped = False
            logger.warning("child %s did not reap cleanly after kill", child.run_name)
        self._reap(child)
        self._join_reader(child)
        if reaped:
            self._reconcile_run_json(child.run_name)

    def shutdown(self) -> None:
        """Stop every live child in parallel, then join (FR-28, BR-27)."""
        with self._lock:
            live = [c for c in self._children.values() if c.process.poll() is None]

        threads: list[threading.Thread] = []
        for child in live:
            with child.state_lock:
                child.stop_requested = True
                child.state = ScanState.STOPPING
            delivered = self._signal_graceful(child)
            grace = _STOP_GRACE_SECONDS if delivered else 0.0
            thread = threading.Thread(
                target=self._await_stop,
                args=(child,),
                kwargs={"grace": grace},
                name=f"viewer-shutdown-{child.run_name}",
                daemon=True,
            )
            threads.append(thread)
            thread.start()

        # The watchers run in parallel, so total time is ~one grace period, not
        # one per child (section 8).
        for thread in threads:
            thread.join(timeout=_STOP_GRACE_SECONDS + _READER_JOIN_TIMEOUT + 5.0)

    # -- output reader + reconcile ----------------------------------------

    def _drain_output(self, child: ScanChild) -> None:
        """Daemon reader: copy the child's combined output into its ring buffer.

        Ends by itself at end-of-file when the child exits; never blocks the
        child (the pipe is drained continuously).
        """
        stream = child.process.stdout
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                child.append_output(line)
        except (OSError, ValueError):
            logger.debug("reader for %s ended on a closed pipe", child.run_name)

    def _join_reader(self, child: ScanChild) -> None:
        reader = child._reader
        if reader is not None and reader.is_alive():
            reader.join(timeout=_READER_JOIN_TIMEOUT)

    def _reconcile_run_json(self, run_name: str) -> None:
        """Rewrite a force-killed run's ``run.json`` to ``interrupted`` (BR-28, DD-3).

        Only when it still reads ``running`` and only after the child is reaped
        (the caller guarantees the latter). Uses a temp file plus atomic
        ``os.replace``. Skipped if the file is unreadable.
        """
        record_path = run_record_path(self._runs_dir / run_name)
        try:
            raw = record_path.read_text(encoding="utf-8")
            record = json.loads(raw)
        except (OSError, ValueError):
            return
        if not isinstance(record, dict) or record.get("status") != "running":
            return
        record["status"] = "interrupted"
        record["end_time"] = _utc_now_iso()
        tmp_path = record_path.with_suffix(record_path.suffix + f".tmp-{secrets.token_hex(4)}")
        try:
            tmp_path.write_text(json.dumps(record), encoding="utf-8")
            tmp_path.replace(record_path)
        except OSError:
            logger.warning("could not reconcile run.json for %s", run_name)
            with contextlib.suppress(OSError):
                tmp_path.unlink()

    # -- rate limit --------------------------------------------------------

    def _record_attempt(self) -> None:
        """Count this attempt against the sliding window; raise 429 when full (BR-4)."""
        now = self._clock()
        cutoff = now - _RATE_WINDOW_SECONDS
        while self._attempts and self._attempts[0] <= cutoff:
            self._attempts.popleft()
        if len(self._attempts) >= _RATE_LIMIT:
            raise LaunchError(
                "rate_limited",
                "too many launch attempts; try again shortly",
                status=429,
                extra={"retry_after": int(_RATE_WINDOW_SECONDS)},
            )
        self._attempts.append(now)


# -- module helpers --------------------------------------------------------


def _resolve_active_window(environ: Mapping[str, str]) -> float:
    """The active-scan recency window, overridable but floored at 30 s (BR-19)."""
    raw = environ.get("STRIX_VIEWER_ACTIVE_WINDOW_SECONDS")
    if raw is None:
        return _DEFAULT_ACTIVE_WINDOW_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_ACTIVE_WINDOW_SECONDS
    if value < _MIN_ACTIVE_WINDOW_SECONDS:
        return _MIN_ACTIVE_WINDOW_SECONDS
    return value


def _process_group_kwargs() -> dict[str, Any]:
    """New-process-group kwargs so a stop signal reaches only the child (section 6)."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:  # noqa: RET505 - explicit branch keeps each arm directly sys.platform-guarded
        # POSIX: a new session detaches the child so SIGINT to its group is isolated.
        return {"start_new_session": True}


def _default_kill_tree(process: PopenLike) -> None:
    """Force-kill the child and its whole tree (section 8 step 6).

    POSIX kills the process group with SIGKILL; Windows runs ``taskkill /T /F``
    as an argv list (never a shell string). Failures are swallowed — the child
    may already be gone.
    """
    if sys.platform == "win32":
        taskkill = _windows_taskkill_path()
        with contextlib.suppress(OSError):
            subprocess.run(  # noqa: S603 - fixed argv, no shell, pid from our own handle
                [taskkill, "/T", "/F", "/PID", str(process.pid)],
                check=False,
                capture_output=True,
            )
    else:
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)


def _windows_taskkill_path() -> str:
    """Absolute path to ``taskkill.exe`` so process termination never relies on PATH.

    Prefers ``%SYSTEMROOT%\\System32\\taskkill.exe``; falls back to the bare name
    only if that directory is unknown (it always is on a real Windows host).
    """
    system_root = os.environ.get("SYSTEMROOT")
    if system_root:
        candidate = Path(system_root) / "System32" / "taskkill.exe"
        if candidate.is_file():
            return str(candidate)
    return "taskkill"


def _utc_now_iso() -> str:
    """Current UTC time as an ISO 8601 string with a ``Z`` suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = ["ProcessHandle", "ScanChild", "ScanLauncher", "ScanSnapshot", "ScanState"]
