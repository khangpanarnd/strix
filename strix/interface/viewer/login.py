"""Username/password sign-in for the viewer, enforced on the server.

The viewer already has one factor: a per-process token in the URL it prints and
opens, exchanged for a session cookie. This adds a second. When login is on, a
request is authorised only if it carries BOTH the session cookie and a valid
login cookie, and a login cookie is only issued by ``POST /api/login`` to a
caller that already holds the session cookie. So a password cannot be guessed
from the network: an attacker would first need the secret URL.

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

* ``STRIX_VIEWER_LOGIN=off``           turn login off entirely.
* ``STRIX_VIEWER_USERNAME``            the username.
* ``STRIX_VIEWER_PASSWORD_HASH``       the hash, from ``python -m strix.interface.viewer.login``.

Without overrides the viewer uses the default account below. Anyone who can read
this source can run offline guesses against its hash, so for anything beyond a
single-user machine, set your own hash.
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
from dataclasses import dataclass
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

#: The built-in account. Overridable through the environment. Only the HASH is here.
DEFAULT_USERNAME = "prdcv"
DEFAULT_PASSWORD_HASH = (
    # A salted PBKDF2 hash, not a password.
    "pbkdf2_sha256$600000$vmbzJA+JkCnYpGcmlNN4Ow==$QLgHIhwuNIYogpF/o4WnjOG522vkez2dtOj/NXOM6GE="  # noqa: S105
)

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

    def __repr__(self) -> str:  # keep the hash out of any stray log of the object
        return f"LoginConfig(username={self.username!r}, password_hash=<hidden>)"

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> LoginConfig | None:
        """The configured account, or None when login is switched off.

        An invalid ``STRIX_VIEWER_PASSWORD_HASH`` raises ``ValueError`` rather than
        silently falling back to the default account: a typo must not quietly
        leave the viewer protected by a different password than the operator set.
        """
        if environ.get("STRIX_VIEWER_LOGIN", "").strip().lower() in {"off", "0", "false", "no"}:
            return None
        username = environ.get("STRIX_VIEWER_USERNAME", "").strip() or DEFAULT_USERNAME
        password_hash = environ.get("STRIX_VIEWER_PASSWORD_HASH", "").strip()
        if password_hash and parse_hash(password_hash) is None:
            raise ValueError(
                "STRIX_VIEWER_PASSWORD_HASH is not a valid hash; generate one with "
                "'python -m strix.interface.viewer.login'"
            )
        if not password_hash:
            if environ.get("STRIX_VIEWER_USERNAME", "").strip():
                raise ValueError(
                    "STRIX_VIEWER_USERNAME is set without STRIX_VIEWER_PASSWORD_HASH; "
                    "set both, or neither to use the built-in account"
                )
            password_hash = DEFAULT_PASSWORD_HASH
        return cls(username=username, password_hash=password_hash)


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
    ) -> None:
        self._config = config
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


def _main() -> int:  # pragma: no cover - interactive helper
    """``python -m strix.interface.viewer.login``: print a hash for STRIX_VIEWER_PASSWORD_HASH."""
    password = getpass.getpass("New viewer password: ")
    if len(password) < 8:
        sys.stderr.write("Use at least 8 characters.\n")
        return 1
    if getpass.getpass("Repeat it: ") != password:
        sys.stderr.write("The passwords do not match.\n")
        return 1
    sys.stdout.write(
        "\nSet these before starting the viewer:\n\n"
        "  STRIX_VIEWER_USERNAME=<your username>\n"
        f"  STRIX_VIEWER_PASSWORD_HASH={hash_password(password)}\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())


__all__ = [
    "DEFAULT_ITERATIONS",
    "DEFAULT_PASSWORD_HASH",
    "DEFAULT_USERNAME",
    "FAILURE_WINDOW_SECONDS",
    "LOCKOUT_SECONDS",
    "MAX_FAILURES",
    "MAX_FIELD_CHARS",
    "MAX_SESSIONS",
    "SESSION_TTL_SECONDS",
    "LoginConfig",
    "LoginManager",
    "LoginResult",
    "hash_password",
    "parse_hash",
    "verify_password",
]
