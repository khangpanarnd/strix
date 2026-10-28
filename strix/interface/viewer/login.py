"""Username/password sign-in for the viewer, enforced on the server.

Username/password is the ONLY factor: a request is authorised when it carries a
valid login cookie, issued by ``POST /api/login`` to a caller that sent the right
credentials. There is no longer a URL token; the page is reachable directly and
the sign-in form is the gate.

The account is persisted (see ``credentials_store``). On a fresh install it is
the well-known default ``admin`` / ``admin`` with a "must change" flag, so the
viewer can be opened once and then forces a real password before any data is
shown. Environment variables still override the stored account.

Design points:

* The password is never stored. Only a salted PBKDF2-SHA256 hash is, in the
  ``pbkdf2_sha256$<iterations>$<salt>$<hash>`` form. Verification is
  constant-time, and runs even for an unknown username so response time does not
  reveal whether a username exists.
* Failed attempts are rate-limited (a rolling window with a lock-out), because
  a hash check is deliberately slow and the endpoint must not become a CPU sink.
* A login session is a random server-side token with an absolute lifetime. It
  lives only in memory, so restarting the viewer signs everyone out.
* Nothing here logs or returns a username, password, or hash.

Configuration (environment, read when the viewer starts):

* ``STRIX_VIEWER_LOGIN=off``           turn sign-in off entirely (no login at all).

That is the only environment switch. The account itself (username, password hash,
first-login flag) lives in the persisted credential store, not the environment;
on first run it is ``admin`` / ``admin`` with a forced change at first sign-in.
Change the password from the viewer's sign-in screen.
"""

from __future__ import annotations

import base64
import binascii
import getpass
import hashlib
import hmac
import logging
import secrets
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


logger = logging.getLogger(__name__)

_SCHEME = "pbkdf2_sha256"
#: OWASP's recommended work factor for PBKDF2-HMAC-SHA256.
DEFAULT_ITERATIONS = 600_000
_MIN_ITERATIONS = 1_000
_MAX_ITERATIONS = 10_000_000
_SALT_BYTES = 16
_HASH_BYTES = 32

#: Longest username/password accepted, so a request cannot make the server hash megabytes.
MAX_FIELD_CHARS = 256

#: The shipped default account. On first run the viewer seeds the persisted
#: credential store with this username and a hash of DEFAULT_SEED_PASSWORD, and
#: forces a password change at first sign-in. The default is intentionally
#: well known so a fresh install can be opened; the forced change is what keeps
#: it safe. Overridable through the environment (then no change is forced).
DEFAULT_USERNAME = "admin"
DEFAULT_SEED_PASSWORD = "admin"  # noqa: S105 - the shipped default, changed on first login

#: Rolling window for failed attempts, how many it takes to lock, and how long it lasts.
FAILURE_WINDOW_SECONDS = 300.0
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60.0

#: A login session's absolute lifetime and the most that are kept at once.
SESSION_TTL_SECONDS = 12 * 3600.0
MAX_SESSIONS = 20


# --- password hashing ----------------------------------------------------------


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def hash_password(
    password: str, *, iterations: int = DEFAULT_ITERATIONS, salt: bytes | None = None
) -> str:
    """Return ``pbkdf2_sha256$<iterations>$<salt>$<hash>`` for ``password``."""
    if not _MIN_ITERATIONS <= iterations <= _MAX_ITERATIONS:
        raise ValueError(f"iterations must be {_MIN_ITERATIONS}..{_MAX_ITERATIONS}")
    salt = secrets.token_bytes(_SALT_BYTES) if salt is None else salt
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, _HASH_BYTES)
    return f"{_SCHEME}${iterations}${_b64e(salt)}${_b64e(digest)}"


def parse_hash(encoded: object) -> tuple[int, bytes, bytes] | None:
    """Split an encoded hash into ``(iterations, salt, digest)``, or None if malformed."""
    if not isinstance(encoded, str):
        return None
    parts = encoded.split("$")
    if len(parts) != 4 or parts[0] != _SCHEME:
        return None
    try:
        iterations = int(parts[1])
        salt = _b64d(parts[2])
        digest = _b64d(parts[3])
    except (ValueError, binascii.Error):
        return None
    if not _MIN_ITERATIONS <= iterations <= _MAX_ITERATIONS or not salt or not digest:
        return None
    return iterations, salt, digest


def verify_password(password: str, encoded: str) -> bool:
    """True when ``password`` matches ``encoded``. Constant-time; False if malformed."""
    parsed = parse_hash(encoded)
    if parsed is None:
        return False
    iterations, salt, expected = parsed
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, len(expected)
    )
    return hmac.compare_digest(actual, expected)


# --- configuration ---------------------------------------------------------------


@dataclass(frozen=True)
class LoginConfig:
    """Who may sign in. Holds a hash, never a password."""

    username: str
    password_hash: str
    #: True while the shipped default must still be replaced (persisted account
    #: only; an env-configured account is never forced to change).
    must_change: bool = False

    def __repr__(self) -> str:  # keep the hash out of any stray log of the object
        return (
            f"LoginConfig(username={self.username!r}, "
            f"password_hash=<hidden>, must_change={self.must_change})"
        )


#: Sentinel returned by :func:`env_login_config` meaning "sign-in is on; use the
#: persisted credential store for the account" (the only non-off outcome now).
USE_STORE = object()


def env_login_config(environ: Mapping[str, str]) -> object | None:
    """Resolve how sign-in is configured from the environment.

    Returns:
      * ``None`` when ``STRIX_VIEWER_LOGIN=off`` -- sign-in is disabled entirely.
      * :data:`USE_STORE` otherwise -- the account comes from the persisted
        credential store (default ``admin`` / ``admin`` on first run).

    Credentials are no longer taken from the environment: there is no
    ``STRIX_VIEWER_USERNAME`` / ``STRIX_VIEWER_PASSWORD_HASH`` path.
    ``STRIX_VIEWER_LOGIN`` is the only switch, and it only turns sign-in off.
    """
    if environ.get("STRIX_VIEWER_LOGIN", "").strip().lower() in {"off", "0", "false", "no"}:
        return None
    return USE_STORE


# --- sessions and rate limiting ------------------------------------------------------


@dataclass(frozen=True)
class LoginResult:
    """Outcome of one sign-in attempt."""

    ok: bool
    token: str | None = None
    #: Set when the attempt was refused because of too many failures.
    retry_after: int | None = None


class LoginManager:
    """Verifies sign-ins, rate-limits failures, and tracks login sessions in memory."""

    def __init__(
        self,
        config: LoginConfig,
        *,
        clock: Callable[[], float] | None = None,
        session_ttl: float = SESSION_TTL_SECONDS,
        on_password_changed: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config
        self._must_change = config.must_change
        # Called with the NEW plaintext password after a successful change, so a
        # caller can persist it (the credential store hashes + writes). Optional:
        # an env-configured account passes nothing and simply cannot be changed.
        self._on_password_changed = on_password_changed
        self._clock = clock or time.monotonic
        self._ttl = session_ttl
        self._lock = threading.Lock()
        # Attempts run one at a time. Each hash check is deliberately slow, and
        # without this a burst of parallel requests would all pass the lock-out
        # check before any of them recorded a failure.
        self._attempt_lock = threading.Lock()
        self._failures: deque[float] = deque()
        self._locked_until = 0.0
        self._sessions: dict[str, float] = {}
        # A hash no password matches, at the SAME work factor as the real one, so
        # an unknown username costs exactly as much time as a known one.
        parsed = parse_hash(config.password_hash)
        if parsed is None:
            raise ValueError("the configured password hash is malformed")
        self._dummy_hash = hash_password("dummy-password-never-matches", iterations=parsed[0])

    # -- sign in ---------------------------------------------------------------

    def attempt(self, username: object, password: object) -> LoginResult:
        """Check credentials. On success returns a new session token."""
        with self._attempt_lock:
            now = self._clock()
            with self._lock:
                wait = self._lockout_remaining(now)
            if wait > 0:
                return LoginResult(ok=False, retry_after=max(1, int(wait + 0.999)))

            good = self._credentials_match(username, password)

            with self._lock:
                now = self._clock()
                if not good:
                    return self._record_failure(now)
                self._failures.clear()
                token = secrets.token_urlsafe(32)
                self._purge(now)
                while len(self._sessions) >= MAX_SESSIONS:
                    self._sessions.pop(min(self._sessions, key=self._sessions.__getitem__))
                self._sessions[token] = now + self._ttl
            return LoginResult(ok=True, token=token)

    def _credentials_match(self, username: object, password: object) -> bool:
        if (
            not isinstance(username, str)
            or not isinstance(password, str)
            or not 0 < len(username) <= MAX_FIELD_CHARS
            or not 0 < len(password) <= MAX_FIELD_CHARS
        ):
            # Same cost as a real check so malformed input is not a timing signal either.
            verify_password("x", self._dummy_hash)
            return False
        user_ok = hmac.compare_digest(
            username.encode("utf-8"), self._config.username.encode("utf-8")
        )
        # Always hash: against the real hash for the real user, a dummy otherwise.
        target = self._config.password_hash if user_ok else self._dummy_hash
        pass_ok = verify_password(password, target)
        return user_ok and pass_ok

    def _lockout_remaining(self, now: float) -> float:
        return max(0.0, self._locked_until - now)

    def _record_failure(self, now: float) -> LoginResult:
        cutoff = now - FAILURE_WINDOW_SECONDS
        while self._failures and self._failures[0] <= cutoff:
            self._failures.popleft()
        self._failures.append(now)
        logger.warning("viewer login failed (%d recent)", len(self._failures))
        if len(self._failures) >= MAX_FAILURES:
            self._locked_until = now + LOCKOUT_SECONDS
            self._failures.clear()
            return LoginResult(ok=False, retry_after=int(LOCKOUT_SECONDS))
        return LoginResult(ok=False)

    # -- sessions ----------------------------------------------------------------

    def is_valid(self, token: str | None) -> bool:
        """True for a live login-session token. Expired ones are dropped."""
        if not token:
            return False
        now = self._clock()
        with self._lock:
            expires = self._sessions.get(token)
            if expires is None:
                return False
            if expires <= now:
                del self._sessions[token]
                return False
            return True

    def logout(self, token: str | None) -> None:
        """End a login session. Unknown tokens are ignored."""
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def _purge(self, now: float) -> None:
        for token in [t for t, exp in self._sessions.items() if exp <= now]:
            del self._sessions[token]

    @property
    def username(self) -> str:
        return self._config.username

    @property
    def session_ttl(self) -> float:
        return self._ttl

    @property
    def must_change_password(self) -> bool:
        """True while the shipped default must still be replaced."""
        with self._lock:
            return self._must_change

    def change_password(self, token: str | None, current: object, new: str) -> LoginResult:
        """Replace the password for the session identified by ``token``.

        Requires a live login session (``token``) and the correct ``current``
        password. ``new`` is assumed already validated by the caller. On success
        the in-memory hash is updated, the ``must_change`` flag is cleared, the
        persistence callback (if any) is invoked with the new plaintext, and the
        caller's session stays valid. A wrong current password is rate-limited
        exactly like a failed sign-in.
        """
        if not self.is_valid(token):
            return LoginResult(ok=False)
        with self._attempt_lock:
            now = self._clock()
            with self._lock:
                wait = self._lockout_remaining(now)
            if wait > 0:
                return LoginResult(ok=False, retry_after=max(1, int(wait + 0.999)))
            # Always hash so a wrong-type/wrong password costs the same.
            if isinstance(current, str) and 0 < len(current) <= MAX_FIELD_CHARS:
                good = verify_password(current, self._config.password_hash)
            else:
                verify_password("x", self._dummy_hash)
                good = False
            with self._lock:
                now = self._clock()
                if not good:
                    return self._record_failure(now)
                self._failures.clear()
                self._config = replace(self._config, password_hash=hash_password(new))
                self._must_change = False
        if self._on_password_changed is not None:
            self._on_password_changed(new)
        return LoginResult(ok=True, token=token)


def _main() -> int:  # pragma: no cover - interactive helper
    """``python -m strix.interface.viewer.login``: set the viewer password.

    A convenience for resetting the account from the terminal (e.g. a forgotten
    password) without the browser. It writes the new hash straight into the
    persisted credential store and clears the forced-change flag.
    """
    # Local import: credentials_store imports from this module at load time, so
    # a top-level import here would be circular.
    from strix.interface.viewer import credentials_store  # noqa: PLC0415

    password = getpass.getpass("New viewer password: ")
    if len(password) < credentials_store.MIN_PASSWORD_CHARS:
        sys.stderr.write(f"Use at least {credentials_store.MIN_PASSWORD_CHARS} characters.\n")
        return 1
    if getpass.getpass("Repeat it: ") != password:
        sys.stderr.write("The passwords do not match.\n")
        return 1
    error = credentials_store.validate_new_password(password)
    if error is not None:
        sys.stderr.write("That password cannot be used; choose another.\n")
        return 1
    account = credentials_store.load_or_seed()
    credentials_store.set_password(account, password)
    sys.stdout.write(
        f"\nPassword updated for user '{account.username}'. "
        "Start the viewer and sign in with it.\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())


__all__ = [
    "DEFAULT_ITERATIONS",
    "DEFAULT_SEED_PASSWORD",
    "DEFAULT_USERNAME",
    "FAILURE_WINDOW_SECONDS",
    "LOCKOUT_SECONDS",
    "MAX_FAILURES",
    "MAX_FIELD_CHARS",
    "MAX_SESSIONS",
    "SESSION_TTL_SECONDS",
    "USE_STORE",
    "LoginConfig",
    "LoginManager",
    "LoginResult",
    "env_login_config",
    "hash_password",
    "parse_hash",
    "verify_password",
]
