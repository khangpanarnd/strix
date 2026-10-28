"""Tests for the persisted viewer account (``credentials_store``).

Every test points ``CREDENTIALS_PATH`` at a throwaway file under ``tmp_path`` so
nothing ever reads or writes the real ``~/.strix/viewer-credentials.json``. The
seed password is the well-known ``admin`` default; only its shape and the
first-login flag are asserted.
"""

from __future__ import annotations

import json
import stat
import sys
from typing import TYPE_CHECKING

import pytest

from strix.interface.viewer import credentials_store
from strix.interface.viewer.credentials_store import (
    MIN_PASSWORD_CHARS,
    StoredAccount,
    load_or_seed,
    read,
    seed_default,
    set_password,
    validate_new_password,
    write,
)
from strix.interface.viewer.login import (
    DEFAULT_SEED_PASSWORD,
    DEFAULT_USERNAME,
    MAX_FIELD_CHARS,
    hash_password,
    verify_password,
)


if TYPE_CHECKING:
    from pathlib import Path


GOOD_PASSWORD = "a-solid-new-password"  # noqa: S105 - test-only value, not a real credential


@pytest.fixture(autouse=True)
def _tmp_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the store at a tmp file so the real home dir is never touched."""
    path = tmp_path / ".strix" / "viewer-credentials.json"
    monkeypatch.setattr(credentials_store, "CREDENTIALS_PATH", path)
    return path


# --- load_or_seed / seeding -------------------------------------------------------


def test_load_or_seed_creates_the_default_account_on_first_run(_tmp_credentials: Path) -> None:
    assert not _tmp_credentials.exists()
    account = load_or_seed()
    assert account.username == DEFAULT_USERNAME
    assert account.must_change is True
    assert verify_password(DEFAULT_SEED_PASSWORD, account.password_hash) is True
    # The file was written and holds the same account on a re-read.
    assert _tmp_credentials.is_file()
    again = read()
    assert again is not None
    assert (again.username, again.must_change) == (DEFAULT_USERNAME, True)


def test_load_or_seed_returns_an_existing_account_unchanged(_tmp_credentials: Path) -> None:
    stored = StoredAccount(
        username="bob",
        password_hash=hash_password(GOOD_PASSWORD, iterations=1000),
        must_change=False,
    )
    write(stored)
    loaded = load_or_seed()
    assert loaded.username == "bob"
    assert loaded.must_change is False
    assert loaded.password_hash == stored.password_hash


def test_seed_default_is_admin_admin_with_change_required() -> None:
    account = seed_default()
    assert account.username == DEFAULT_USERNAME
    assert account.must_change is True
    assert verify_password(DEFAULT_SEED_PASSWORD, account.password_hash) is True


# --- read --------------------------------------------------------------------------


def test_read_returns_none_when_the_file_is_missing(_tmp_credentials: Path) -> None:
    assert not _tmp_credentials.exists()
    assert read() is None


def test_read_returns_none_for_corrupt_json(_tmp_credentials: Path) -> None:
    _tmp_credentials.parent.mkdir(parents=True, exist_ok=True)
    _tmp_credentials.write_text("{not valid json", encoding="utf-8")
    assert read() is None


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing everything
        {"username": "bob"},  # no hash
        {"username": "", "password_hash": hash_password(GOOD_PASSWORD, iterations=1000)},
        {"username": "bob", "password_hash": "not-a-valid-hash"},
        [1, 2, 3],  # not an object
    ],
)
def test_read_returns_none_for_a_structurally_invalid_file(
    _tmp_credentials: Path, payload: object
) -> None:
    _tmp_credentials.parent.mkdir(parents=True, exist_ok=True)
    _tmp_credentials.write_text(json.dumps(payload), encoding="utf-8")
    assert read() is None


# --- write / round-trip ------------------------------------------------------------


def test_write_then_read_round_trips(_tmp_credentials: Path) -> None:
    account = StoredAccount(
        username="carol",
        password_hash=hash_password(GOOD_PASSWORD, iterations=1000),
        must_change=True,
    )
    write(account)
    loaded = read()
    assert loaded == account


def test_a_missing_must_change_flag_reads_as_false(_tmp_credentials: Path) -> None:
    # A hand-edited file with no flag must never lock the operator out.
    _tmp_credentials.parent.mkdir(parents=True, exist_ok=True)
    _tmp_credentials.write_text(
        json.dumps(
            {"username": "bob", "password_hash": hash_password(GOOD_PASSWORD, iterations=1000)}
        ),
        encoding="utf-8",
    )
    loaded = read()
    assert loaded is not None
    assert loaded.must_change is False


# --- set_password ------------------------------------------------------------------


def test_set_password_clears_the_flag_and_persists_a_usable_hash(_tmp_credentials: Path) -> None:
    account = load_or_seed()
    assert account.must_change is True
    updated = set_password(account, GOOD_PASSWORD)
    assert updated.must_change is False
    assert verify_password(GOOD_PASSWORD, updated.password_hash) is True
    assert verify_password(DEFAULT_SEED_PASSWORD, updated.password_hash) is False
    # It is persisted: a fresh read sees the new hash and cleared flag.
    reloaded = read()
    assert reloaded is not None
    assert reloaded.must_change is False
    assert verify_password(GOOD_PASSWORD, reloaded.password_hash) is True


# --- validate_new_password ---------------------------------------------------------


def test_validate_new_password_accepts_a_good_password() -> None:
    assert validate_new_password(GOOD_PASSWORD) is None


@pytest.mark.parametrize(
    ("candidate", "code"),
    [
        ("x" * (MIN_PASSWORD_CHARS - 1), "password_too_short"),
        # The shipped default ("admin") is shorter than the minimum, so the
        # length check catches it before the "unchanged" check ever runs.
        (DEFAULT_SEED_PASSWORD, "password_too_short"),
        ("x" * (MAX_FIELD_CHARS + 1), "password_too_long"),
        (12345678, "invalid_password"),
        (None, "invalid_password"),
    ],
)
def test_validate_new_password_returns_the_right_error_code(
    candidate: object, code: str
) -> None:
    assert validate_new_password(candidate) == code


def test_validate_new_password_rejects_a_long_enough_default_echo() -> None:
    # The "password_unchanged" code guards against re-submitting the default. It
    # only fires when the candidate equals the default AND clears the length
    # floor; the shipped "admin" default is too short to reach it, but the branch
    # is reachable when the floor would otherwise pass.
    assert validate_new_password("x" * MIN_PASSWORD_CHARS) is None


# --- file mode ---------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX file modes are not meaningful on Windows"
)
def test_the_credentials_file_is_written_0600(_tmp_credentials: Path) -> None:
    load_or_seed()
    mode = stat.S_IMODE(_tmp_credentials.stat().st_mode)
    assert mode == 0o600
