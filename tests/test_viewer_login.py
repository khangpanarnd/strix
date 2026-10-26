"""Tests for viewer sign-in: hashing, configuration, rate limiting and sessions.

Tests use throwaway passwords and a low iteration count so they stay fast. The
shipped default account's real password is deliberately NOT in this file or
anywhere else in the repo; only its hash is, and it is checked for shape.
"""

from __future__ import annotations

import base64
import logging
import threading
import time

import pytest

from strix.interface.viewer import login
from strix.interface.viewer.login import (
    DEFAULT_PASSWORD_HASH,
    DEFAULT_USERNAME,
    FAILURE_WINDOW_SECONDS,
    LOCKOUT_SECONDS,
    MAX_FAILURES,
    MAX_FIELD_CHARS,
    MAX_SESSIONS,
    LoginConfig,
    LoginManager,
    hash_password,
    parse_hash,
    verify_password,
)


FAST = 1_000  # iterations; the minimum allowed, for speed
PASSWORD = "correct-horse-battery-staple"  # noqa: S105 - test-only value, not a real credential


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _manager(clock: Clock | None = None, **kw: float) -> tuple[LoginManager, Clock]:
    clock = clock or Clock()
    config = LoginConfig(username="alice", password_hash=hash_password(PASSWORD, iterations=FAST))
    return LoginManager(config, clock=clock, **kw), clock


# --- hashing -------------------------------------------------------------------


def test_hash_has_the_documented_shape() -> None:
    encoded = hash_password(PASSWORD, iterations=FAST)
    scheme, iterations, salt, digest = encoded.split("$")
    assert scheme == "pbkdf2_sha256"
    assert iterations == str(FAST)
    assert len(base64.b64decode(salt)) == 16
    assert len(base64.b64decode(digest)) == 32
    assert PASSWORD not in encoded


def test_hashing_is_salted() -> None:
    assert hash_password(PASSWORD, iterations=FAST) != hash_password(PASSWORD, iterations=FAST)


def test_correct_password_verifies_and_wrong_ones_do_not() -> None:
    encoded = hash_password(PASSWORD, iterations=FAST)
    assert verify_password(PASSWORD, encoded) is True
    for wrong in ("", "x", PASSWORD + " ", PASSWORD.upper(), PASSWORD[:-1], " " + PASSWORD):
        assert verify_password(wrong, encoded) is False, wrong


def test_unicode_passwords_round_trip() -> None:
    encoded = hash_password("pässwörd-密码-🔑", iterations=FAST)
    assert verify_password("pässwörd-密码-🔑", encoded) is True
    assert verify_password("passwörd-密码-🔑", encoded) is False


def test_a_fixed_salt_makes_the_hash_reproducible() -> None:
    assert hash_password("a", iterations=FAST, salt=b"s" * 16) == hash_password(
        "a", iterations=FAST, salt=b"s" * 16
    )


@pytest.mark.parametrize("iterations", [0, 999, 10_000_001, -1])
def test_hash_password_rejects_unsafe_work_factors(iterations: int) -> None:
    with pytest.raises(ValueError, match="iterations"):
        hash_password("x", iterations=iterations)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "plain-text-password",
        "pbkdf2_sha256$600000$only-three",
        "md5$600000$c2FsdA==$aGFzaA==",
        "pbkdf2_sha256$abc$c2FsdA==$aGFzaA==",
        "pbkdf2_sha256$999$c2FsdA==$aGFzaA==",
        "pbkdf2_sha256$600000$!!!notbase64!!!$aGFzaA==",
        "pbkdf2_sha256$600000$$aGFzaA==",
        "pbkdf2_sha256$600000$c2FsdA==$",
        "pbkdf2_sha256$600000$c2FsdA==$aGFzaA==$extra",
    ],
)
def test_malformed_hashes_never_verify_and_never_raise(bad: str) -> None:
    assert parse_hash(bad) is None
    assert verify_password("anything", bad) is False


def test_parse_hash_rejects_non_strings() -> None:
    assert parse_hash(None) is None  # type: ignore[arg-type]
    assert parse_hash(123) is None  # type: ignore[arg-type]


# --- configuration ----------------------------------------------------------------


def test_the_shipped_default_account_is_well_formed_and_stores_no_plaintext() -> None:
    assert DEFAULT_USERNAME == "prdcv"
    parsed = parse_hash(DEFAULT_PASSWORD_HASH)
    assert parsed is not None
    iterations, salt, digest = parsed
    assert iterations >= 600_000
    assert len(salt) >= 16
    assert len(digest) == 32
    assert DEFAULT_PASSWORD_HASH.startswith("pbkdf2_sha256$")


def test_default_config_uses_the_built_in_account() -> None:
    config = LoginConfig.from_env({})
    assert config is not None
    assert config.username == DEFAULT_USERNAME
    assert config.password_hash == DEFAULT_PASSWORD_HASH


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "No", " off "])
def test_login_can_be_switched_off(value: str) -> None:
    assert LoginConfig.from_env({"STRIX_VIEWER_LOGIN": value}) is None


@pytest.mark.parametrize("value", ["on", "1", "", "yes", "maybe"])
def test_other_values_leave_login_on(value: str) -> None:
    assert LoginConfig.from_env({"STRIX_VIEWER_LOGIN": value}) is not None


def test_env_can_override_the_account() -> None:
    encoded = hash_password(PASSWORD, iterations=FAST)
    config = LoginConfig.from_env(
        {"STRIX_VIEWER_USERNAME": "bob", "STRIX_VIEWER_PASSWORD_HASH": encoded}
    )
    assert config is not None
    assert (config.username, config.password_hash) == ("bob", encoded)


def test_env_hash_alone_keeps_the_default_username() -> None:
    encoded = hash_password(PASSWORD, iterations=FAST)
    config = LoginConfig.from_env({"STRIX_VIEWER_PASSWORD_HASH": encoded})
    assert config is not None
    assert config.username == DEFAULT_USERNAME
    assert config.password_hash == encoded


def test_a_bad_env_hash_is_an_error_not_a_silent_fallback() -> None:
    with pytest.raises(ValueError, match="not a valid hash"):
        LoginConfig.from_env({"STRIX_VIEWER_PASSWORD_HASH": "hunter2"})


def test_a_username_override_without_a_hash_is_an_error() -> None:
    # Otherwise the default password would silently protect the operator's chosen name.
    with pytest.raises(ValueError, match="without STRIX_VIEWER_PASSWORD_HASH"):
        LoginConfig.from_env({"STRIX_VIEWER_USERNAME": "bob"})


def test_config_repr_hides_the_hash() -> None:
    config = LoginConfig(username="alice", password_hash=hash_password(PASSWORD, iterations=FAST))
    assert config.password_hash not in repr(config)
    assert "alice" in repr(config)


def test_manager_rejects_a_malformed_configured_hash() -> None:
    with pytest.raises(ValueError, match="malformed"):
        LoginManager(LoginConfig(username="alice", password_hash="nope"))  # noqa: S106


# --- sign in ----------------------------------------------------------------------------


def test_correct_credentials_open_a_session() -> None:
    manager, _ = _manager()
    result = manager.attempt("alice", PASSWORD)
    assert result.ok is True
    assert result.token and len(result.token) >= 32
    assert result.retry_after is None
    assert manager.is_valid(result.token) is True


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("alice", "wrong"),
        ("mallory", PASSWORD),
        ("Alice", PASSWORD),  # usernames are case-sensitive
        ("alice ", PASSWORD),
        ("", PASSWORD),
        ("alice", ""),
        ("", ""),
        (None, PASSWORD),
        ("alice", None),
        (1, 2),
        (["alice"], [PASSWORD]),
        ({"u": "alice"}, PASSWORD),
        ("alice", b"bytes"),
    ],
)
def test_wrong_or_malformed_credentials_are_refused(username: object, password: object) -> None:
    manager, _ = _manager()
    result = manager.attempt(username, password)
    assert result.ok is False
    assert result.token is None


def test_oversized_fields_are_refused_without_hashing_them() -> None:
    manager, _ = _manager()
    assert manager.attempt("alice", "p" * (MAX_FIELD_CHARS + 1)).ok is False
    assert manager.attempt("a" * (MAX_FIELD_CHARS + 1), PASSWORD).ok is False
    # ...and the limit is inclusive: a password exactly at the cap is still checked normally.
    config = LoginConfig(
        username="alice", password_hash=hash_password("p" * MAX_FIELD_CHARS, iterations=FAST)
    )
    assert LoginManager(config).attempt("alice", "p" * MAX_FIELD_CHARS).ok is True


def test_each_session_token_is_unique_and_independent() -> None:
    manager, _ = _manager()
    first = manager.attempt("alice", PASSWORD).token
    second = manager.attempt("alice", PASSWORD).token
    assert first != second
    manager.logout(first)
    assert manager.is_valid(first) is False
    assert manager.is_valid(second) is True


def test_unknown_and_empty_tokens_are_invalid() -> None:
    manager, _ = _manager()
    assert manager.is_valid(None) is False
    assert manager.is_valid("") is False
    assert manager.is_valid("not-a-real-token") is False


def test_logout_is_idempotent_and_ignores_garbage() -> None:
    manager, _ = _manager()
    token = manager.attempt("alice", PASSWORD).token
    manager.logout(token)
    manager.logout(token)
    manager.logout(None)
    manager.logout("never-existed")
    assert manager.is_valid(token) is False


def test_sessions_expire() -> None:
    manager, clock = _manager(session_ttl=100.0)
    token = manager.attempt("alice", PASSWORD).token
    clock.advance(99)
    assert manager.is_valid(token) is True
    clock.advance(2)
    assert manager.is_valid(token) is False
    assert manager.is_valid(token) is False  # and it stays gone


def test_expired_sessions_are_purged_when_new_ones_open() -> None:
    manager, clock = _manager(session_ttl=10.0)
    for _ in range(5):
        manager.attempt("alice", PASSWORD)
    clock.advance(11)
    manager.attempt("alice", PASSWORD)
    assert len(manager._sessions) == 1


def test_the_number_of_live_sessions_is_capped_and_oldest_go_first() -> None:
    manager, clock = _manager()
    tokens = []
    for _ in range(MAX_SESSIONS + 3):
        clock.advance(1)
        tokens.append(manager.attempt("alice", PASSWORD).token)
    assert len(manager._sessions) == MAX_SESSIONS
    assert not any(manager.is_valid(t) for t in tokens[:3])
    assert all(manager.is_valid(t) for t in tokens[3:])


# --- rate limiting ----------------------------------------------------------------------------


def test_repeated_failures_lock_out_even_the_right_password() -> None:
    manager, _ = _manager()
    for i in range(MAX_FAILURES):
        result = manager.attempt("alice", f"wrong-{i}")
        assert result.ok is False
    locked = manager.attempt("alice", PASSWORD)
    assert locked.ok is False
    assert locked.retry_after is not None
    assert 1 <= locked.retry_after <= LOCKOUT_SECONDS
    assert locked.token is None


def test_the_attempt_that_trips_the_limit_reports_retry_after() -> None:
    manager, _ = _manager()
    results = [manager.attempt("alice", f"wrong-{i}") for i in range(MAX_FAILURES)]
    assert [r.retry_after for r in results[:-1]] == [None] * (MAX_FAILURES - 1)
    assert results[-1].retry_after == int(LOCKOUT_SECONDS)


def test_lockout_ends_and_the_right_password_then_works() -> None:
    manager, clock = _manager()
    for i in range(MAX_FAILURES):
        manager.attempt("alice", f"wrong-{i}")
    clock.advance(LOCKOUT_SECONDS + 1)
    assert manager.attempt("alice", PASSWORD).ok is True


def test_retry_after_counts_down() -> None:
    manager, clock = _manager()
    for i in range(MAX_FAILURES):
        manager.attempt("alice", f"wrong-{i}")
    clock.advance(40)
    result = manager.attempt("alice", PASSWORD)
    assert result.retry_after is not None
    assert result.retry_after <= LOCKOUT_SECONDS - 40 + 1


def test_a_locked_out_attempt_does_not_extend_the_lockout() -> None:
    manager, clock = _manager()
    for i in range(MAX_FAILURES):
        manager.attempt("alice", f"wrong-{i}")
    clock.advance(30)
    manager.attempt("alice", "still-wrong")  # refused while locked
    clock.advance(LOCKOUT_SECONDS - 30 + 1)
    assert manager.attempt("alice", PASSWORD).ok is True


def test_failures_age_out_of_the_window() -> None:
    manager, clock = _manager()
    for i in range(MAX_FAILURES - 1):
        manager.attempt("alice", f"wrong-{i}")
    clock.advance(FAILURE_WINDOW_SECONDS + 1)
    # Those are forgotten, so another few misses do not trip the limit.
    for i in range(MAX_FAILURES - 1):
        assert manager.attempt("alice", f"again-{i}").retry_after is None
    assert manager.attempt("alice", PASSWORD).ok is True


def test_a_success_clears_the_failure_count() -> None:
    manager, _ = _manager()
    for i in range(MAX_FAILURES - 1):
        manager.attempt("alice", f"wrong-{i}")
    assert manager.attempt("alice", PASSWORD).ok is True
    for i in range(MAX_FAILURES - 1):
        assert manager.attempt("alice", f"wrong-{i}").retry_after is None


def test_malformed_input_counts_as_a_failure() -> None:
    manager, _ = _manager()
    for _ in range(MAX_FAILURES):
        manager.attempt(None, None)
    assert manager.attempt("alice", PASSWORD).retry_after is not None


# --- timing and logging ---


def test_unknown_user_costs_about_as_much_as_a_known_one() -> None:
    # A faster "no such user" would reveal which usernames exist. Use a work factor
    # large enough that scheduling noise cannot hide a 100x difference.
    config = LoginConfig(
        username="alice", password_hash=hash_password(PASSWORD, iterations=100_000)
    )
    manager = LoginManager(config)

    def timed(user: str) -> float:
        best = float("inf")
        for _ in range(3):
            manager._failures.clear()
            start = time.perf_counter()
            manager.attempt(user, "definitely-wrong")
            best = min(best, time.perf_counter() - start)
        return best

    known, unknown = timed("alice"), timed("mallory")
    assert unknown > known * 0.4, (
        f"unknown user answered suspiciously fast ({unknown:.4f}s vs {known:.4f}s)"
    )
    assert unknown < known * 2.5


def test_the_dummy_hash_matches_the_real_work_factor() -> None:
    config = LoginConfig(username="alice", password_hash=hash_password(PASSWORD, iterations=5_000))
    manager = LoginManager(config)
    dummy = parse_hash(manager._dummy_hash)
    assert dummy is not None
    assert dummy[0] == 5_000


def test_failures_are_logged_without_credentials(caplog: pytest.LogCaptureFixture) -> None:
    manager, _ = _manager()
    with caplog.at_level(logging.DEBUG, logger=login.__name__):
        manager.attempt("alice", "hunter2-secret-guess")
        manager.attempt("mallory", PASSWORD)
        manager.attempt("alice", PASSWORD)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "login failed" in text
    for secret in ("hunter2-secret-guess", PASSWORD, "alice", "mallory", "pbkdf2"):
        assert secret not in text


def test_concurrent_attempts_cannot_beat_the_lockout() -> None:
    # A burst of parallel guesses must not all slip past the check before the first failure counts.
    manager, _ = _manager()
    results: list[login.LoginResult] = []
    guard = threading.Lock()

    def guess(i: int) -> None:
        outcome = manager.attempt("alice", f"wrong-{i}")
        with guard:
            results.append(outcome)

    threads = [threading.Thread(target=guess, args=(i,)) for i in range(MAX_FAILURES * 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    judged_wrong = [r for r in results if r.retry_after is None]
    assert len(judged_wrong) == MAX_FAILURES - 1, (
        "only the first few guesses may actually be checked"
    )
    assert manager.attempt("alice", PASSWORD).retry_after is not None
