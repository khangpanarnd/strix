"""Tests for how the ``strix view`` command wires up sign-in."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from strix.interface.viewer import cli
from strix.interface.viewer.login import (
    DEFAULT_PASSWORD_HASH,
    DEFAULT_USERNAME,
    LoginManager,
    hash_password,
)


PASSWORD = "an-example-password-for-tests"  # noqa: S105 - test-only value, not a credential


def _console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, width=200, force_terminal=False, color_system=None), buffer


def test_sign_in_is_on_by_default_with_the_built_in_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("STRIX_VIEWER_LOGIN", "STRIX_VIEWER_USERNAME", "STRIX_VIEWER_PASSWORD_HASH"):
        monkeypatch.delenv(name, raising=False)
    console, _ = _console()

    login = cli._build_login(console)

    assert isinstance(login, LoginManager)
    assert login.username == DEFAULT_USERNAME


def test_sign_in_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRIX_VIEWER_LOGIN", "off")
    console, _ = _console()
    assert cli._build_login(console) is None


def test_env_account_replaces_the_built_in_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRIX_VIEWER_LOGIN", raising=False)
    monkeypatch.setenv("STRIX_VIEWER_USERNAME", "bob")
    monkeypatch.setenv("STRIX_VIEWER_PASSWORD_HASH", hash_password(PASSWORD, iterations=1000))
    console, _ = _console()

    login = cli._build_login(console)

    assert login is not None
    assert login.username == "bob"
    assert login.attempt("bob", PASSWORD).ok is True
    assert login.attempt("prdcv", PASSWORD).ok is False  # the built-in account is gone


@pytest.mark.parametrize(
    "env",
    [
        {"STRIX_VIEWER_PASSWORD_HASH": "not-a-hash-at-all"},
        {"STRIX_VIEWER_USERNAME": "bob"},  # a name with no hash
    ],
)
def test_a_bad_configuration_stops_the_viewer_with_exit_code_1(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    monkeypatch.delenv("STRIX_VIEWER_LOGIN", raising=False)
    monkeypatch.delenv("STRIX_VIEWER_USERNAME", raising=False)
    monkeypatch.delenv("STRIX_VIEWER_PASSWORD_HASH", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    console, buffer = _console()

    with pytest.raises(SystemExit) as exc:
        cli._build_login(console)

    assert exc.value.code == 1
    assert "Cannot start the viewer" in buffer.getvalue()


def test_the_error_does_not_echo_the_bad_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRIX_VIEWER_LOGIN", raising=False)
    monkeypatch.setenv("STRIX_VIEWER_PASSWORD_HASH", "oops-this-might-be-a-real-password")
    console, buffer = _console()
    with pytest.raises(SystemExit):
        cli._build_login(console)
    assert "oops-this-might-be-a-real-password" not in buffer.getvalue()


def test_the_console_note_names_the_user_but_never_a_secret() -> None:
    console, buffer = _console()
    login = LoginManager(
        cli.LoginConfig(username="carol", password_hash=hash_password(PASSWORD, iterations=1000))
    )

    cli._print_login_note(console, login)

    text = buffer.getvalue()
    assert "carol" in text
    assert "Sign-in is on" in text
    assert PASSWORD not in text
    assert "pbkdf2" not in text
    assert DEFAULT_PASSWORD_HASH not in text


def test_the_console_warns_when_sign_in_is_off() -> None:
    console, buffer = _console()
    cli._print_login_note(console, None)
    assert "Sign-in is off" in buffer.getvalue()


def test_markup_in_a_username_cannot_inject_console_formatting() -> None:
    console, buffer = _console()
    login = LoginManager(
        cli.LoginConfig(
            username="[bold red]x[/]", password_hash=hash_password(PASSWORD, iterations=1000)
        )
    )
    cli._print_login_note(console, login)
    assert "[bold red]x[/]" in buffer.getvalue()  # printed literally, not interpreted
