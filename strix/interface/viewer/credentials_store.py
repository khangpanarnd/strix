"""Persisted viewer account, stored in SQLite.

The viewer's sign-in account (username, password hash, first-login flag) lives in
a small SQLite database at ``~/.strix/viewer-users.db`` (file mode 0600). SQLite
is used rather than a flat file so the store can grow into real user management
later without changing its home or callers; today it holds exactly one account.

On first run the ``users`` table is empty, so :func:`load_or_seed` inserts the
default account ``admin`` / ``admin`` with ``must_change=1``. The default
password is intentionally well known so a fresh install can be opened; the
``must_change`` flag forces the operator to replace it at first sign-in, and no
data endpoint unlocks until they do.

There are no credential environment variables any more: the account is defined
entirely by this database. ``STRIX_VIEWER_LOGIN=off`` (handled in ``login.py``)
still disables sign-in altogether.

Only a salted PBKDF2-SHA256 hash is stored, never a password.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from strix.utils.secret_files import SECRET_FILE_MODE

from .login import (
    DEFAULT_SEED_PASSWORD,
    DEFAULT_USERNAME,
    MAX_FIELD_CHARS,
    hash_password,
    parse_hash,
)


if TYPE_CHECKING:
    from collections.abc import Iterator


logger = logging.getLogger(__name__)

#: Where the account database lives. Sibling of the other ``~/.strix`` state.
CREDENTIALS_PATH = Path.home() / ".strix" / "viewer-users.db"

#: Shortest password the operator may set when replacing the default.
MIN_PASSWORD_CHARS = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    must_change   INTEGER NOT NULL DEFAULT 0,
    role          TEXT NOT NULL DEFAULT 'admin',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class StoredAccount:
    """The account persisted in the database. Holds a hash, never a password."""

    username: str
    password_hash: str
    #: True until the operator replaces the shipped default at first sign-in.
    must_change: bool

    def __repr__(self) -> str:  # keep the hash out of any stray log of the object
        return (
            f"StoredAccount(username={self.username!r}, "
            f"password_hash=<hidden>, must_change={self.must_change})"
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


@contextlib.contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Open the users DB, creating the parent dir and schema, with 0600 perms.

    The file is created with owner-only permissions the first time it appears,
    matching the old flat-file behaviour; SQLite's later writes keep the mode.
    """
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    newly_created = not CREDENTIALS_PATH.exists()
    conn = sqlite3.connect(str(CREDENTIALS_PATH))
    try:
        if newly_created:
            with contextlib.suppress(OSError):
                CREDENTIALS_PATH.chmod(SECRET_FILE_MODE)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row_to_account(row: tuple[str, str, int]) -> StoredAccount | None:
    username, password_hash, must_change = row
    if not isinstance(username, str) or not username:
        return None
    if not isinstance(password_hash, str) or parse_hash(password_hash) is None:
        return None
    return StoredAccount(
        username=username, password_hash=password_hash, must_change=bool(must_change)
    )


def read() -> StoredAccount | None:
    """Return the single stored account, or None when the table is empty/invalid.

    "The account" is the first row by creation time; the schema holds one today.
    A structurally invalid row (blank name, unparseable hash) reads as None, the
    same fail-safe the flat file had.
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT username, password_hash, must_change "
                "FROM users ORDER BY created_at, username LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        logger.warning("could not read the viewer users database", exc_info=True)
        return None
    return _row_to_account(row) if row else None


def write(account: StoredAccount) -> None:
    """Persist ``account`` as the single stored user (upsert by username)."""
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (username, password_hash, must_change, role, created_at, updated_at)
            VALUES (?, ?, ?, 'admin', ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                password_hash = excluded.password_hash,
                must_change   = excluded.must_change,
                updated_at    = excluded.updated_at
            """,
            (account.username, account.password_hash, int(account.must_change), now, now),
        )


def seed_default() -> StoredAccount:
    """The shipped default account: ``admin`` / ``admin``, change required."""
    return StoredAccount(
        username=DEFAULT_USERNAME,
        password_hash=hash_password(DEFAULT_SEED_PASSWORD),
        must_change=True,
    )


def load_or_seed() -> StoredAccount:
    """Return the stored account, creating the default one on first run.

    A readable existing account is returned as-is. When the table is empty (or
    the file cannot be read) it is seeded with the default ``admin`` / ``admin``
    account and ``must_change=true`` so a fresh install can be opened and is then
    forced to pick a real password.
    """
    existing = read()
    if existing is not None:
        return existing
    account = seed_default()
    _try_write(account)
    return account


def set_password(account: StoredAccount, new_password: str) -> StoredAccount:
    """Return a copy of ``account`` with a new password hash and the flag cleared.

    Also persists it. The caller is expected to have already verified the current
    password and validated the new one (see :func:`validate_new_password`).
    """
    updated = replace(account, password_hash=hash_password(new_password), must_change=False)
    write(updated)
    return updated


def validate_new_password(new_password: object) -> str | None:
    """Return an error code for an unacceptable new password, or None if it is fine."""
    if not isinstance(new_password, str):
        return "invalid_password"
    if len(new_password) < MIN_PASSWORD_CHARS:
        return "password_too_short"
    if len(new_password) > MAX_FIELD_CHARS:
        return "password_too_long"
    if new_password == DEFAULT_SEED_PASSWORD:
        return "password_unchanged"
    return None


def _try_write(account: StoredAccount) -> None:
    """Persist the seed, but do not abort startup if the disk refuses.

    A read-only ``~/.strix`` must not stop the viewer: the in-memory default
    account still works for this run; only the forced change cannot be
    remembered across restarts.
    """
    try:
        write(account)
    except (OSError, sqlite3.Error):
        logger.warning(
            "could not write %s; the default account works for this run but a "
            "password change will not persist",
            CREDENTIALS_PATH,
        )


__all__ = [
    "CREDENTIALS_PATH",
    "MIN_PASSWORD_CHARS",
    "StoredAccount",
    "load_or_seed",
    "read",
    "seed_default",
    "set_password",
    "validate_new_password",
    "write",
]
