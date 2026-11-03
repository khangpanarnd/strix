"""Listing runs must survive a run's ``run.json`` vanishing mid-listing.

``/api/capabilities`` asks for the newest run on every page load. The listing checks
``is_file()`` and then ``stat()``s the same path a moment later; a run deleted (or its
record rewritten) in between used to raise ``FileNotFoundError``, which the server turned
into HTTP 500. The page reads capabilities exactly once and swallows a failure, so one
unlucky load left "New scan" out of the menu.

The race is simulated deterministically: the record of one run reports "exists" and then
fails to ``stat()``, which is exactly the observable behaviour of a file removed in between.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from strix.core import paths
from strix.interface.viewer import server


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class _VanishingRecord:
    """A run.json that passes ``is_file()`` and is gone by the time it is ``stat()``-ed."""

    def is_file(self) -> bool:
        return True

    def stat(self) -> os.stat_result:
        raise FileNotFoundError("run.json vanished between is_file() and stat()")


def _make_run(base: Path, name: str, mtime: float) -> Path:
    run_dir = base / name
    run_dir.mkdir(parents=True)
    record = run_dir / "run.json"
    record.write_text(json.dumps({"run_name": name}), encoding="utf-8")
    os.utime(record, (mtime, mtime))
    return run_dir


def _patch_record_path(monkeypatch: pytest.MonkeyPatch, module: object, vanishing: str) -> None:
    real = paths.run_record_path

    def run_record_path(run_dir: Path) -> object:
        return _VanishingRecord() if run_dir.name == vanishing else real(run_dir)

    monkeypatch.setattr(module, "run_record_path", run_record_path)


def test_viewer_listing_skips_a_run_whose_record_vanishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = _make_run(tmp_path, "older", 1_000)
    newer = _make_run(tmp_path, "newer", 2_000)
    _make_run(tmp_path, "gone", 3_000)
    _patch_record_path(monkeypatch, server, "gone")

    assert server._iter_run_dirs(tmp_path) == [newer, older]
    assert server._latest_run_dir(tmp_path) == newer


def test_viewer_listing_with_only_a_vanishing_run_is_empty_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_run(tmp_path, "gone", 1_000)
    _patch_record_path(monkeypatch, server, "gone")

    assert server._iter_run_dirs(tmp_path) == []
    assert server._latest_run_dir(tmp_path) is None


def test_viewer_listing_of_a_missing_folder_is_empty(tmp_path: Path) -> None:
    assert server._iter_run_dirs(tmp_path / "does-not-exist") == []


def test_viewer_listing_keeps_newest_first_for_healthy_runs(tmp_path: Path) -> None:
    a = _make_run(tmp_path, "a", 1_000)
    b = _make_run(tmp_path, "b", 3_000)
    c = _make_run(tmp_path, "c", 2_000)

    assert server._iter_run_dirs(tmp_path) == [b, c, a]


def test_latest_run_dir_skips_a_run_whose_record_vanishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / paths.RUNS_DIR_NAME
    _make_run(base, "older", 1_000)
    newer = _make_run(base, "newer", 2_000)
    _make_run(base, "gone", 3_000)
    _patch_record_path(monkeypatch, paths, "gone")

    assert paths.latest_run_dir(cwd=tmp_path) == newer


def test_latest_run_dir_with_only_a_vanishing_run_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / paths.RUNS_DIR_NAME
    _make_run(base, "gone", 1_000)
    _patch_record_path(monkeypatch, paths, "gone")

    assert paths.latest_run_dir(cwd=tmp_path) is None


def test_latest_run_dir_prefers_the_first_of_equal_timestamps(tmp_path: Path) -> None:
    """Behaviour kept from ``max()``: on a tie the first candidate wins."""
    base = tmp_path / paths.RUNS_DIR_NAME
    _make_run(base, "x", 5_000)
    _make_run(base, "y", 5_000)
    expected = next(child for child in base.iterdir() if (child / "run.json").is_file())

    assert paths.latest_run_dir(cwd=tmp_path) == expected
