"""Regression tests for issue #725: local --target hangs on file-by-file import.

Root cause: a local directory ``--target`` was handed to the SDK ``LocalDir``
manifest entry, which copies the tree into the sandbox file-by-file at
``session.start()`` — hours-long on large repos. The fix bind-mounts local
targets read-only by default (fast, applied at container-create time).

The bug-condition exploration test asserts the *negation* of the bug condition
C(X): a local_code target must resolve to a bind mount, not a copied LocalDir
entry. It fails on the unfixed code (confirming the bug) and passes after the
fix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agents.sandbox.entries import LocalDir

from strix.interface.utils import collect_local_sources
from strix.runtime.session_manager import build_session_entries


if TYPE_CHECKING:
    from pathlib import Path


def _local_target(target_path: str, workspace_subdir: str = "repo") -> dict[str, Any]:
    """A plain ``--target <dir>`` target_info (no explicit --mount)."""
    return {
        "type": "local_code",
        "details": {"target_path": target_path, "workspace_subdir": workspace_subdir},
        "original": target_path,
    }


def test_bug_condition_local_target_is_bind_mounted_not_copied(tmp_path: Path) -> None:
    """BUG-CONDITION EXPLORATION (issue #725).

    A local directory target must be configured as a bind mount, NOT a
    file-by-file LocalDir copy. Expected to FAIL on unfixed code, where a plain
    --target defaults to a LocalDir copy.
    """
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")

    sources = collect_local_sources([_local_target(str(tmp_path))])
    entries, bind_mounts, _staged = build_session_entries(sources)

    # Negation of C(X): bind-mounted, and NOT a copied LocalDir entry.
    assert entries == {}, "local --target must not be a file-by-file LocalDir copy"
    assert [m["target"] for m in bind_mounts] == ["/workspace/repo"]
    assert all(not isinstance(v, LocalDir) for v in entries.values())


def test_local_target_bind_mount_is_read_only(tmp_path: Path) -> None:
    sources = collect_local_sources([_local_target(str(tmp_path))])
    _entries, bind_mounts, _staged = build_session_entries(sources)

    assert bind_mounts == [
        {
            "source": str(tmp_path.resolve()),
            "target": "/workspace/repo",
            "read_only": True,
        }
    ]


def test_explicit_copy_still_supported(tmp_path: Path) -> None:
    """An explicit mount=False local source is still copied (escape hatch preserved)."""
    target = _local_target(str(tmp_path))
    target["details"]["mount"] = False

    sources = collect_local_sources([target])
    entries, bind_mounts, _staged = build_session_entries(sources)

    assert bind_mounts == []
    assert isinstance(entries["repo"], LocalDir)


def test_repository_source_is_unaffected(tmp_path: Path) -> None:
    """FR2: cloned repositories retain their copy behavior (not bind-mounted)."""
    repo = {
        "type": "repository",
        "details": {"cloned_repo_path": str(tmp_path), "workspace_subdir": "clone"},
    }
    sources = collect_local_sources([repo])
    entries, bind_mounts, _staged = build_session_entries(sources)

    assert bind_mounts == []
    assert isinstance(entries["clone"], LocalDir)
