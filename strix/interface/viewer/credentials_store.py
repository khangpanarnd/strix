"""Persisted viewer account: username, password hash, and a first-login flag.

The viewer no longer uses a URL token as a first factor. The username/password
is the ONLY gate, so the credential has to live somewhere durable rather than in
an environment variable the operator must set every time. This module owns that
file.

State lives in ``~/.strix/viewer-credentials.json`` (0600), holding::

    {"username": "...", "password_hash": "pbkdf2_sha256$...", "must_change": true}

On first run the file does not exist, so :func:`load_or_seed` creates it with the
default account ``admin`` / ``admin`` and ``must_change=true``. The default
password is intentionally well known so a fresh install can be opened; the
``must_change`` flag forces the operator to replace it at first sign-in, and no
data endpoint unlocks until they do.

Environment overrides still win over this file (see ``login.LoginConfig``): an
operator who sets ``STRIX_VIEWER_USERNAME`` + ``STRIX_VIEWER_PASSWORD_HASH`` is
trusted and never asked to change anything.

The password itself is never written here, only a salted PBKDF2-SHA256 hash.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path

from strix.utils.secret_files import write_secret_text

from .login import (
    DEFAULT_SEED_PASSWORD,
    DEFAULT_USERNAME,
    MAX_FIELD_CHARS,
    hash_password,
    parse_hash,
)


logger = logging.getLogger(__name__)

#: Where the account is stored. Sibling of the other ``~/.strix`` state files.
CREDENTIALS_PATH = Path.home() / ".strix" / "viewer-credentials.json"


@dataclass(frozen=True)
class StoredAccount:
    """The account persisted on disk. Holds a hash, never a password."""

    username: str
    password_hash: str
    #: True until the operator replaces the shipped default at first sign-in.
    must_change: bool

    def __repr__(self) -> str:  # keep the hash out of any stray log of the object
        return (
            f"StoredAccount(username={self.username!r}, "
            f"password_hash=<hidden>, must_change={self.must_change})"
        )


def _parse(data: object) -> StoredAccount | None:
    """Validate a decoded JSON object into a :class:`StoredAccount`, or None."""
    if not isinstance(data, dict):
        return None
    username = data.get("username")
    password_hash = data.get("password_hash")
    if not isinstance(username, str) or not username:
        return None
    if not isinstance(password_hash, str) or parse_hash(password_hash) is None:
        return None
    # A missing/invalid flag is read as "no change required": only an explicit
    # ``true`` keeps forcing the prompt, so a hand-edited file never locks out.
    must_change = data.get("must_change") is True
    return StoredAccount(
        username=username, password_hash=password_hash, must_change=must_change
    )


def read() -> StoredAccount | None:
    """Return the stored account, or None when the file is absent or unreadable."""
    try:
        raw = CREDENTIALS_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return _parse(json.loads(raw))
    except json.JSONDecodeError:
        logger.warning("viewer credentials file is not valid JSON; ignoring it")
        return None


def write(account: StoredAccount) -> None:
    """Atomically persist the account with 0600 permissions."""
    payload = json.dumps(
        {
            "username": account.username,
            "password_hash": account.password_hash,
            "must_change": account.must_change,
        }
    )
    write_secret_text(CREDENTIALS_PATH, payload)


def seed_default() -> StoredAccount:
    """The shipped default account: ``admin`` / ``admin``, change required."""
    return StoredAccount(
        username=DEFAULT_USERNAME,
        password_hash=hash_password(DEFAULT_SEED_PASSWORD),
        must_change=True,
    )


def load_or_seed() -> StoredAccount:
    """Return the stored account, creating the default one on first run.

    A readable existing file is returned as-is. When the file is missing or
    corrupt it is (re)written with the default ``admin`` / ``admin`` account and
    ``must_change=true`` so a fresh install can be opened and is then forced to
    pick a real password.
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
    updated = replace(
        account, password_hash=hash_password(new_password), must_change=False
    )
    write(updated)
    return updated


#: Shortest password the operator may set when replacing the default.
MIN_PASSWORD_CHARS = 8


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
    with contextlib.suppress(OSError):
        write(account)
        return
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
