"""Tests for ``strix view`` launch mode (Step 7; FR-1, FR-2, FR-28, NFR-6).

``serve`` and the launcher are stubbed so no real server or scan starts. The
tests drive ``run_view`` and assert which mode it enters, the arguments it
passes to ``serve``, the non-loopback warning, and that shutdown stops children.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from strix.interface.viewer import cli as viewer_cli


if TYPE_CHECKING:
    from pathlib import Path


class FakeServer:
    """Stands in for the ThreadingHTTPServer returned by serve()."""

    def __init__(self) -> None:
        self.shutdown_calls = 0
        self.closed = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def server_close(self) -> None:
        self.closed += 1


class FakeLauncher:
    def __init__(self) -> None:
        self.shutdowns = 0

    def shutdown(self) -> None:
        self.shutdowns += 1


def _make_run(base: Path, name: str, *, status: str = "completed") -> Path:
    run_dir = base / "strix_runs" / name
    run_dir.mkdir(parents=True)
    record = {"run_name": name, "status": status, "end_time": "2026-01-01T00:00:00Z"}
    (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")
    return run_dir


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    *,
    server: FakeServer,
    launcher: FakeLauncher | None = None,
    stop_loop: bool = True,
) -> dict[str, Any]:
    """Patch serve(), the ScanLauncher, telemetry and the blocking loop.

    Returns a dict capturing the serve() call so tests can inspect run_dir,
    runs_dir and launcher.
    """
    captured: dict[str, Any] = {}

    def fake_serve(run_dir: Any, **kwargs: Any) -> tuple[FakeServer, str, str]:
        captured["run_dir"] = run_dir
        captured["kwargs"] = kwargs
        return server, "http://127.0.0.1:8080", "tok"

    monkeypatch.setattr(viewer_cli, "serve", fake_serve)
    monkeypatch.setattr(viewer_cli, "bundle_is_built", lambda: True)
    monkeypatch.setattr("strix.telemetry.posthog.viewer_opened", lambda **_kw: None)

    if launcher is not None:
        monkeypatch.setattr(viewer_cli, "ScanLauncher", lambda *_a, **_kw: launcher)

    if stop_loop:
        # Make the blocking loop exit at once by raising KeyboardInterrupt from
        # the first sleep.
        def _raise(_seconds: float) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr("strix.interface.viewer.cli.time.sleep", _raise)

    return captured


def test_no_run_starts_launch_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    server = FakeServer()
    launcher = FakeLauncher()
    captured = _patch_common(monkeypatch, server=server, launcher=launcher)

    viewer_cli.run_view([])

    # No run exists -> launch mode: serve() gets run_dir=None and a launcher.
    assert captured["run_dir"] is None
    assert captured["kwargs"]["launcher"] is launcher
    assert captured["kwargs"]["runs_dir"] == tmp_path / "strix_runs"


def test_plain_view_opens_latest_run_when_one_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    run_dir = _make_run(tmp_path, "existing")
    server = FakeServer()
    captured = _patch_common(monkeypatch, server=server)

    viewer_cli.run_view([])

    # A run exists and --launch is absent -> fixed-run mode (no launcher).
    assert captured["run_dir"] == run_dir
    assert "launcher" not in captured["kwargs"]


def test_unknown_named_run_still_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path, "existing")
    server = FakeServer()
    _patch_common(monkeypatch, server=server)

    with pytest.raises(SystemExit) as exc:
        viewer_cli.run_view(["does-not-exist"])
    assert exc.value.code == 1


def test_launch_flag_forces_launch_mode_even_with_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path, "existing")
    server = FakeServer()
    launcher = FakeLauncher()
    captured = _patch_common(monkeypatch, server=server, launcher=launcher)

    viewer_cli.run_view(["--launch"])

    assert captured["run_dir"] is None
    assert captured["kwargs"]["launcher"] is launcher


def test_non_loopback_warning_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    server = FakeServer()
    launcher = FakeLauncher()
    _patch_common(monkeypatch, server=server, launcher=launcher)

    viewer_cli.run_view(["--launch", "--host", "0.0.0.0"])

    out = capsys.readouterr().out
    assert "reachable over the network" in out


def test_loopback_bind_has_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    server = FakeServer()
    launcher = FakeLauncher()
    _patch_common(monkeypatch, server=server, launcher=launcher)

    viewer_cli.run_view(["--launch"])

    out = capsys.readouterr().out
    assert "reachable over the network" not in out


def test_shutdown_stops_launcher_before_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    server = FakeServer()
    launcher = FakeLauncher()
    _patch_common(monkeypatch, server=server, launcher=launcher)

    viewer_cli.run_view(["--launch"])

    # The launcher's children are stopped, and the server is torn down.
    assert launcher.shutdowns == 1
    assert server.shutdown_calls == 1
    assert server.closed == 1


def test_fixed_run_shutdown_does_not_touch_a_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path, "existing")
    server = FakeServer()
    _patch_common(monkeypatch, server=server)

    viewer_cli.run_view([])

    # Fixed-run mode has no launcher; the server still tears down cleanly.
    assert server.shutdown_calls == 1
    assert server.closed == 1


def test_view_help_includes_launch(capsys: pytest.CaptureFixture[str]) -> None:
    try:
        viewer_cli.run_view(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("--help should exit")
    help_text = capsys.readouterr().out
    assert "--launch" in help_text
    assert "--host HOST" in help_text
    assert "0.0.0.0" in help_text
