"""Tests for how the ``strix view`` command wires up sign-in.

The default account now comes from the persisted credential store, so every
test points ``credentials_store.CREDENTIALS_PATH`` at a throwaway file under
``tmp_path``. Nothing here ever reads or writes the real
``~/.strix/viewer-credentials.json``.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest
from rich.console import Console

from strix.interface.viewer import cli, credentials_store
from strix.interface.viewer.login import (
    DEFAULT_SEED_PASSWORD,
    DEFAULT_USERNAME,
    LoginManager,
    hash_password,
)


if TYPE_CHECKING:
    from pathlib import Path


PASSWORD = "an-example-password-for-tests"  # noqa: S105 - test-only value, not a credential


@pytest.fixture(autouse=True)
def _tmp_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the store at a tmp file. ``cli`` reads it via the module, so
    patching the one attribute covers every reference."""
    path = tmp_path / ".strix" / "viewer-credentials.json"
    monkeypatch.setattr(credentials_store, "CREDENTIALS_PATH", path)
    return path


def _console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, width=200, force_terminal=False, color_system=None), buffer


def test_sign_in_is_on_by_default_with_the_stored_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("STRIX_VIEWER_LOGIN", "STRIX_VIEWER_USERNAME", "STRIX_VIEWER_PASSWORD_HASH"):
        monkeypatch.delenv(name, raising=False)
    console, _ = _console()

    login = cli._build_login(console)

    assert isinstance(login, LoginManager)
    assert login.username == DEFAULT_USERNAME


def test_the_first_run_default_account_must_change_its_password(
    monkeypatch: pytest.MonkeyPatch, _tmp_credentials: Path
) -> None:
    for name in ("STRIX_VIEWER_LOGIN", "STRIX_VIEWER_USERNAME", "STRIX_VIEWER_PASSWORD_HASH"):
        monkeypatch.delenv(name, raising=False)
    assert not _tmp_credentials.exists()
    console, buffer = _console()

    login = cli._build_login(console)

    assert isinstance(login, LoginManager)
    # Seeded with the well-known default, which can sign in but must be changed.
    assert login.must_change_password is True
    assert login.attempt(DEFAULT_USERNAME, DEFAULT_SEED_PASSWORD).ok is True

    cli._print_login_note(console, login)
    text = buffer.getvalue()
    assert DEFAULT_USERNAME in text
    assert "First sign-in" in text  # the note warns about the forced change
    assert "set a new one" in text


def test_sign_in_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRIX_VIEWER_LOGIN", "off")
    console, _ = _console()
    assert cli._build_login(console) is None


def test_credential_env_vars_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    # The old env-account path is gone: even with both vars set, sign-in uses
    # the stored default and those values have no effect.
    monkeypatch.delenv("STRIX_VIEWER_LOGIN", raising=False)
    monkeypatch.setenv("STRIX_VIEWER_USERNAME", "bob")
    monkeypatch.setenv("STRIX_VIEWER_PASSWORD_HASH", hash_password(PASSWORD, iterations=1000))
    console, _ = _console()

    login = cli._build_login(console)

    assert login is not None
    assert login.username == DEFAULT_USERNAME
    assert login.attempt("bob", PASSWORD).ok is False
    assert login.attempt(DEFAULT_USERNAME, DEFAULT_SEED_PASSWORD).ok is True


def test_a_bad_env_hash_no_longer_stops_the_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    # A junk STRIX_VIEWER_PASSWORD_HASH used to abort startup; now it is simply
    # ignored and the stored account is used.
    monkeypatch.delenv("STRIX_VIEWER_LOGIN", raising=False)
    monkeypatch.setenv("STRIX_VIEWER_PASSWORD_HASH", "not-a-hash-at-all")
    console, _ = _console()

    login = cli._build_login(console)

    assert login is not None
    assert login.username == DEFAULT_USERNAME
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
