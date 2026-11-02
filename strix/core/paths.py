"""Run directory path helpers."""

from __future__ import annotations

from pathlib import Path


RUNS_DIR_NAME = "strix_runs"
RUNTIME_STATE_DIR_NAME = ".state"
RUN_RECORD_FILENAME = "run.json"


def run_dir_for(run_name: str, *, cwd: Path | None = None) -> Path:
    base = cwd or Path.cwd()
    return base / RUNS_DIR_NAME / run_name


def runtime_state_dir(run_dir: Path) -> Path:
    return run_dir / RUNTIME_STATE_DIR_NAME


def run_record_path(run_dir: Path) -> Path:
    return run_dir / RUN_RECORD_FILENAME


def runs_base_dir(*, cwd: Path | None = None) -> Path:
    base = cwd or Path.cwd()
    return base / RUNS_DIR_NAME


def latest_run_dir(*, cwd: Path | None = None) -> Path | None:
    base = runs_base_dir(cwd=cwd)
    if not base.is_dir():
        return None
    # run.json is rewritten on status/end changes, so its mtime tracks activity
    # more reliably than the directory mtime (a live run sorts to the top).
    newest: Path | None = None
    newest_mtime = float("-inf")
    try:
        children = list(base.iterdir())
    except OSError:
        return None
    for child in children:
        record = run_record_path(child)
        try:
            if not record.is_file():
                continue
            modified = record.stat().st_mtime
        except OSError:
            # Deleted, or replaced, between the check and the stat: not a candidate.
            continue
        if modified > newest_mtime:
            newest, newest_mtime = child, modified
    return newest
