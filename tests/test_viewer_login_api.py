"""HTTP tests for viewer sign-in (username/password) and byte-range static serving.

A real ``ThreadingHTTPServer`` with a real :class:`LoginManager` (low iteration
count for speed). Username/password is the ONLY factor now: there is no URL
token and no bootstrap session cookie. The properties that matter:

* a valid login cookie, and nothing else, opens the data routes — once the
  account no longer owes a forced password change;
* a wrong password and an unknown user look identical, and failures lock out;
* the forced-change flow keeps the data locked until the password is replaced;
* credentials, hashes and tokens never appear in a response body or a log line;
* the landing page and its video stay public and the video can be seeked.
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import pytest

from strix.interface.viewer import credentials_store
from strix.interface.viewer.login import (
    DEFAULT_SEED_PASSWORD,
    MAX_FAILURES,
    LoginConfig,
    LoginManager,
    hash_password,
)
from strix.interface.viewer.server import serve


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


USERNAME = "alice"
PASSWORD = "correct-horse-battery-staple"  # noqa: S105 - test-only value, not a real credential
NEW_PASSWORD = "a-brand-new-password-9"  # noqa: S105 - test-only value, not a real credential
VIDEO = bytes(range(256)) * 20  # 5120 deterministic bytes; any byte offset is checkable


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _tmp_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real ``~/.strix`` untouched even though these tests pass an
    explicit login manager — a defence in depth against any stray store read."""
    monkeypatch.setattr(
        credentials_store, "CREDENTIALS_PATH", tmp_path / ".strix" / "viewer-credentials.json"
    )


def _manager(clock: Clock | None = None, ttl: float = 3600.0) -> LoginManager:
    config = LoginConfig(USERNAME, hash_password(PASSWORD, iterations=1000))
    return LoginManager(config, clock=clock, session_ttl=ttl)


def _must_change_manager(
    clock: Clock | None = None, ttl: float = 3600.0
) -> tuple[LoginManager, list[str]]:
    """A manager whose account still owes the forced change, plus a capture list."""
    captured: list[str] = []
    config = LoginConfig(USERNAME, hash_password(PASSWORD, iterations=1000), must_change=True)
    manager = LoginManager(
        config, clock=clock, session_ttl=ttl, on_password_changed=captured.append
    )
    return manager, captured


def _make_assets(tmp_path: Path) -> Path:
    assets = tmp_path / "bundle"
    (assets / "assets").mkdir(parents=True)
    (assets / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    (assets / "intro.mp4").write_bytes(VIDEO)
    (assets / "logo.png").write_bytes(b"\x89PNG-not-really")
    return assets


def _make_run(runs: Path) -> None:
    run = runs / "web-1"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run_name": "web-1", "status": "running"}), "utf-8")
    (run / "strix.log").write_text(
        "2026-10-26 13:20:13.123 INFO    s - strix.core.runner: hello\n", encoding="utf-8"
    )


class Served:
    def __init__(self, url: str, port: int) -> None:
        self.url, self.port = url, port


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Served]:
    monkeypatch.setattr("strix.interface.viewer.server.bundle_dir", lambda: _make_assets(tmp_path))
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    _make_run(runs)
    login = _manager()
    httpd, url, _empty = serve(None, open_browser=False, runs_dir=runs, login=login)
    s = Served(url, httpd.server_address[1])
    s.login = login  # type: ignore[attr-defined]
    try:
        yield s
    finally:
        httpd.shutdown()
        httpd.server_close()


def _req(
    s: Served,
    path: str,
    *,
    method: str = "GET",
    body: Any = None,
    cookies: list[str] | None = None,
    headers: dict[str, str] | None = None,
    raw_body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    hdrs = dict(headers or {})
    data = raw_body
    if body is not None:
        data = json.dumps(body).encode()
    if data is not None:
        hdrs.setdefault("Content-Type", "application/json")
    if cookies:
        hdrs["Cookie"] = "; ".join(cookies)
    req = urllib.request.Request(s.url + path, data=data, headers=hdrs, method=method)  # noqa: S310
    # When the server rejects a request before reading its body (415, 403 bad
    # origin, 411), Windows can reset the connection while the client is still
    # sending. That is a client-side race, not a server answer, so retry it. A
    # genuine HTTP response (including every error status) is never retried.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req) as resp:  # noqa: S310  # nosec B310
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()
        except (urllib.error.URLError, ConnectionError):
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def _json(raw: bytes) -> Any:
    return json.loads(raw) if raw else None


def _sign_in(
    s: Served, username: str = USERNAME, password: str = PASSWORD
) -> str:
    """Post straight to /api/login (no prerequisite cookie) and return the login cookie."""
    status, headers, _ = _req(
        s, "/api/login", method="POST", body={"username": username, "password": password}
    )
    assert status == 200
    return headers["Set-Cookie"].split(";", 1)[0]


DATA_ROUTES = [
    "/api/run",
    "/api/vulnerabilities",
    "/api/transcript",
    "/api/log",
    "/api/report",
]


# --- status ---
def test_status_for_a_signed_out_browser(served: Served) -> None:
    status, _, body = _req(served, "/api/login/status")
    assert status == 200
    assert _json(body) == {
        "login_required": True,
        "authenticated": False,
        "must_change_password": False,
        "username": None,
    }


def test_status_after_signing_in_names_the_user(served: Served) -> None:
    login = _sign_in(served)
    info = _json(_req(served, "/api/login/status", cookies=[login])[2])
    assert info == {
        "login_required": True,
        "authenticated": True,
        "must_change_password": False,
        "username": USERNAME,
    }


def test_status_is_never_cached(served: Served) -> None:
    assert _req(served, "/api/login/status")[1]["Cache-Control"] == "no-store"


# --- signing in ---
def test_correct_credentials_sign_in_and_set_a_hardened_cookie(served: Served) -> None:
    status, headers, body = _req(
        served,
        "/api/login",
        method="POST",
        body={"username": USERNAME, "password": PASSWORD},
    )
    assert status == 200
    assert _json(body) == {"ok": True, "username": USERNAME, "must_change_password": False}
    cookie = headers["Set-Cookie"]
    assert cookie.startswith(f"strix_viewer_login_{served.port}=")
    for attribute in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=3600"):
        assert attribute in cookie, attribute
    assert headers["Cache-Control"] == "no-store"
    token = cookie.split("=", 1)[1].split(";", 1)[0]
    assert len(token) >= 32
    assert PASSWORD not in cookie
    assert PASSWORD not in body.decode()


def test_each_sign_in_gets_its_own_token(served: Served) -> None:
    assert _sign_in(served) != _sign_in(served)


def test_wrong_password_and_unknown_user_look_identical(served: Served) -> None:
    wrong_password = _req(
        served,
        "/api/login",
        method="POST",
        body={"username": USERNAME, "password": "nope"},
    )
    unknown_user = _req(
        served,
        "/api/login",
        method="POST",
        body={"username": "mallory", "password": PASSWORD},
    )
    assert wrong_password[0] == unknown_user[0] == 401
    assert wrong_password[2] == unknown_user[2]  # same body: no username enumeration
    assert _json(wrong_password[2])["error"] == "invalid_credentials"
    assert "Set-Cookie" not in wrong_password[1]
    assert "Set-Cookie" not in unknown_user[1]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"username": USERNAME},
        {"password": PASSWORD},
        {"username": None, "password": None},
        {"username": 123, "password": 456},
        {"username": [USERNAME], "password": [PASSWORD]},
        {"username": USERNAME, "password": ""},
        {"username": "", "password": PASSWORD},
        {"username": "a" * 5000, "password": PASSWORD},
        {"username": USERNAME, "password": "p" * 5000},
    ],
)
def test_incomplete_or_wrongly_typed_credentials_are_401(
    served: Served, body: dict[str, Any]
) -> None:
    status, headers, payload = _req(served, "/api/login", method="POST", body=body)
    assert status == 401
    assert _json(payload)["error"] == "invalid_credentials"
    assert "Set-Cookie" not in headers


def test_extra_fields_are_ignored(served: Served) -> None:
    status, _, _ = _req(
        served,
        "/api/login",
        method="POST",
        body={"username": USERNAME, "password": PASSWORD, "admin": True},
    )
    assert status == 200


@pytest.mark.parametrize("raw", [b"{nope", b"\xff\xfe\xfd", b"[1,2]", b'"str"', b"123", b"null"])
def test_malformed_bodies_are_400(served: Served, raw: bytes) -> None:
    status, _, payload = _req(
        served,
        "/api/login",
        method="POST",
        raw_body=raw,
        headers={"Content-Type": "application/json"},
    )
    assert status == 400
    assert _json(payload)["error"] == "invalid_json"


def test_an_empty_body_means_no_credentials(served: Served) -> None:
    status, headers, payload = _req(
        served,
        "/api/login",
        method="POST",
        raw_body=b"",
        headers={"Content-Type": "application/json"},
    )
    assert status == 401
    assert _json(payload)["error"] == "invalid_credentials"
    assert "Set-Cookie" not in headers


def test_wrong_content_type_is_415(served: Served) -> None:
    status, _, payload = _req(
        served,
        "/api/login",
        method="POST",
        raw_body=json.dumps({"username": USERNAME, "password": PASSWORD}).encode(),
        headers={"Content-Type": "text/plain"},
    )
    assert status == 415
    assert _json(payload)["error"] == "invalid_content_type"


def test_a_foreign_origin_cannot_sign_in(served: Served) -> None:
    status, headers, payload = _req(
        served,
        "/api/login",
        method="POST",
        body={"username": USERNAME, "password": PASSWORD},
        headers={"Origin": "http://evil.example.com"},
    )
    assert status == 403
    assert _json(payload)["error"] == "bad_origin"
    assert "Set-Cookie" not in headers


def test_a_wildcard_bind_accepts_a_matching_lan_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A viewer on 0.0.0.0 reached by its LAN IP must accept the sign-in: the
    # browser sends Origin == Host == that IP, which is same-origin even though
    # it is not the literal bound host. (Regression: this used to 403.)
    monkeypatch.setattr("strix.interface.viewer.server.bundle_dir", lambda: _make_assets(tmp_path))
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    _make_run(runs)
    httpd, _url, _empty = serve(
        None, host="0.0.0.0", open_browser=False, runs_dir=runs, login=_manager()
    )
    port = httpd.server_address[1]
    lan = f"http://127.0.0.1:{port}"  # stand-in for the LAN IP; the check is Origin==Host
    try:
        # Reach it on 127.0.0.1 and claim that same host as Origin: accepted.
        status, headers, _ = _req(
            Served(lan, port),
            "/api/login",
            method="POST",
            body={"username": USERNAME, "password": PASSWORD},
            headers={"Origin": lan, "Host": f"127.0.0.1:{port}"},
        )
        assert status == 200
        assert "Set-Cookie" in headers

        # But a different host in the Origin than the Host is still rejected.
        status, _, payload = _req(
            Served(lan, port),
            "/api/login",
            method="POST",
            body={"username": USERNAME, "password": PASSWORD},
            headers={"Origin": f"http://10.0.0.9:{port}", "Host": f"127.0.0.1:{port}"},
        )
        assert status == 403
        assert _json(payload)["error"] == "bad_origin"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_missing_content_length_is_411(served: Served) -> None:
    parsed = urlsplit(served.url)
    conn = http.client.HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port or 80)
    conn.putrequest("POST", "/api/login")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Transfer-Encoding", "chunked")
    conn.endheaders()
    assert conn.getresponse().status == 411
    conn.close()


# --- lock-out ---


def test_repeated_failures_lock_the_form_even_for_the_right_password(served: Served) -> None:
    for i in range(MAX_FAILURES):
        _req(
            served,
            "/api/login",
            method="POST",
            body={"username": USERNAME, "password": f"wrong-{i}"},
        )

    status, headers, payload = _req(
        served,
        "/api/login",
        method="POST",
        body={"username": USERNAME, "password": PASSWORD},
    )

    assert status == 429
    info = _json(payload)
    assert info["error"] == "rate_limited"
    assert 1 <= info["retry_after"] <= 60
    assert headers["Retry-After"] == str(info["retry_after"])
    assert "Set-Cookie" not in headers


# --- the login cookie gates every data route ---
@pytest.mark.parametrize("path", DATA_ROUTES)
def test_data_routes_need_a_login_cookie(served: Served, path: str) -> None:
    login = _sign_in(served)

    nothing = _req(served, path)[0]
    with_login = _req(served, path, cookies=[login])[0]

    assert nothing == 403, "nothing but a login cookie opens the data"
    assert with_login == 200


def test_a_forged_login_cookie_grants_nothing(served: Served) -> None:
    forged = f"strix_viewer_login_{served.port}=" + "A" * 43
    assert _req(served, "/api/run", cookies=[forged])[0] == 403


def test_the_login_cookie_from_another_port_is_not_accepted(served: Served) -> None:
    login = _sign_in(served)
    token = login.split("=", 1)[1]
    wrong_name = f"strix_viewer_login_{served.port + 1}={token}"
    assert _req(served, "/api/run", cookies=[wrong_name])[0] == 403


def test_run_history_stays_locked_until_signed_in(served: Served) -> None:
    login = _sign_in(served)
    before = _json(_req(served, "/api/runs")[2])
    after = _json(_req(served, "/api/runs", cookies=[login])[2])
    assert before["locked"] is True
    assert before["runs"] == []
    assert after["locked"] is False
    assert [r["name"] for r in after["runs"]] == ["web-1"]


def test_settings_and_launch_posts_are_gated_too(served: Served) -> None:
    for method, path in (("GET", "/api/settings"), ("GET", "/api/notifications")):
        assert _req(served, path, method=method)[0] == 403
    status, _, _ = _req(served, "/api/scans", method="POST", body={"target": "x"})
    assert status == 403


def test_sessions_expire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("strix.interface.viewer.server.bundle_dir", lambda: _make_assets(tmp_path))
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    _make_run(runs)
    clock = Clock()
    httpd, url, _empty = serve(
        None, open_browser=False, runs_dir=runs, login=_manager(clock, ttl=100.0)
    )
    s = Served(url, httpd.server_address[1])
    try:
        login = _sign_in(s)
        assert _req(s, "/api/run", cookies=[login])[0] == 200
        clock.now += 101
        assert _req(s, "/api/run", cookies=[login])[0] == 403
        info = _json(_req(s, "/api/login/status", cookies=[login])[2])
        assert info["authenticated"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- forced password change ---


@pytest.fixture
def served_must_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Served]:
    """A viewer whose account still owes the forced first-login change."""
    monkeypatch.setattr("strix.interface.viewer.server.bundle_dir", lambda: _make_assets(tmp_path))
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    _make_run(runs)
    login, captured = _must_change_manager()
    httpd, url, _empty = serve(None, open_browser=False, runs_dir=runs, login=login)
    s = Served(url, httpd.server_address[1])
    s.login = login  # type: ignore[attr-defined]
    s.captured = captured  # type: ignore[attr-defined]
    try:
        yield s
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_must_change_account_signs_in_but_data_stays_locked(served_must_change: Served) -> None:
    status, _, body = _req(
        served_must_change,
        "/api/login",
        method="POST",
        body={"username": USERNAME, "password": PASSWORD},
    )
    assert status == 200
    assert _json(body)["must_change_password"] is True
    login = _sign_in(served_must_change)

    # The status shows the half-signed-in state: a name, but not authenticated.
    info = _json(_req(served_must_change, "/api/login/status", cookies=[login])[2])
    assert info == {
        "login_required": True,
        "authenticated": False,
        "must_change_password": True,
        "username": USERNAME,
    }
    # Data stays locked until the password is changed.
    for path in DATA_ROUTES:
        assert _req(served_must_change, path, cookies=[login])[0] == 403, path


def test_changing_the_password_unlocks_the_data(served_must_change: Served) -> None:
    login = _sign_in(served_must_change)
    status, _, body = _req(
        served_must_change,
        "/api/password",
        method="POST",
        body={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        cookies=[login],
    )
    assert status == 200
    assert _json(body) == {"ok": True, "username": USERNAME}
    # The persistence callback saw the new plaintext.
    assert served_must_change.captured == [NEW_PASSWORD]  # type: ignore[attr-defined]

    # The same session is now fully authenticated and data is open.
    info = _json(_req(served_must_change, "/api/login/status", cookies=[login])[2])
    assert info["authenticated"] is True
    assert info["must_change_password"] is False
    assert _req(served_must_change, "/api/run", cookies=[login])[0] == 200


def test_the_password_change_rejects_a_wrong_current(served_must_change: Served) -> None:
    login = _sign_in(served_must_change)
    status, _, payload = _req(
        served_must_change,
        "/api/password",
        method="POST",
        body={"current_password": "not-the-password", "new_password": NEW_PASSWORD},
        cookies=[login],
    )
    assert status == 401
    assert _json(payload)["error"] == "invalid_current_password"
    # Still locked.
    assert _req(served_must_change, "/api/run", cookies=[login])[0] == 403


def test_the_password_change_rejects_a_too_short_new_password(
    served_must_change: Served,
) -> None:
    login = _sign_in(served_must_change)
    status, _, payload = _req(
        served_must_change,
        "/api/password",
        method="POST",
        body={"current_password": PASSWORD, "new_password": "short"},
        cookies=[login],
    )
    assert status == 400
    assert _json(payload)["error"] == "password_too_short"


def test_the_password_change_rejects_reusing_the_shipped_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A must-change account whose current password IS the shipped default. The
    # operator cannot simply re-submit the default: it fails the policy (it is
    # shorter than the minimum, so the length check refuses it) through the HTTP
    # layer, so the forced change cannot be a no-op.
    monkeypatch.setattr("strix.interface.viewer.server.bundle_dir", lambda: _make_assets(tmp_path))
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    _make_run(runs)
    config = LoginConfig(
        USERNAME, hash_password(DEFAULT_SEED_PASSWORD, iterations=1000), must_change=True
    )
    login_mgr = LoginManager(config)
    httpd, url, _empty = serve(None, open_browser=False, runs_dir=runs, login=login_mgr)
    s = Served(url, httpd.server_address[1])
    try:
        login = _sign_in(s, password=DEFAULT_SEED_PASSWORD)
        status, _, payload = _req(
            s,
            "/api/password",
            method="POST",
            body={"current_password": DEFAULT_SEED_PASSWORD, "new_password": DEFAULT_SEED_PASSWORD},
            cookies=[login],
        )
        assert status == 400
        assert _json(payload)["error"] == "password_too_short"
        # Still must-change: the no-op was refused.
        assert _req(s, "/api/run", cookies=[login])[0] == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_password_change_needs_a_login_cookie(served_must_change: Served) -> None:
    status, _, payload = _req(
        served_must_change,
        "/api/password",
        method="POST",
        body={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert status == 403
    assert _json(payload)["error"] == "forbidden"


def test_the_password_change_rejects_a_foreign_origin(served_must_change: Served) -> None:
    login = _sign_in(served_must_change)
    status, _, payload = _req(
        served_must_change,
        "/api/password",
        method="POST",
        body={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        cookies=[login],
        headers={"Origin": "http://evil.example.com"},
    )
    assert status == 403
    assert _json(payload)["error"] == "bad_origin"
    # And nothing changed: still locked.
    assert _req(served_must_change, "/api/run", cookies=[login])[0] == 403


# --- sign out ---
def test_logout_ends_the_session_and_clears_the_cookie(served: Served) -> None:
    login = _sign_in(served)
    assert _req(served, "/api/run", cookies=[login])[0] == 200

    status, headers, payload = _req(
        served, "/api/logout", method="POST", body={}, cookies=[login]
    )

    assert status == 200
    assert _json(payload) == {"ok": True}
    assert "Max-Age=0" in headers["Set-Cookie"]
    # The server forgot the session, not just the browser: replaying the old cookie fails.
    assert _req(served, "/api/run", cookies=[login])[0] == 403


def test_logout_only_ends_the_callers_own_session(served: Served) -> None:
    mine, other = _sign_in(served), _sign_in(served)
    _req(served, "/api/logout", method="POST", body={}, cookies=[mine])
    assert _req(served, "/api/run", cookies=[other])[0] == 200


def test_logout_without_a_session_is_forbidden(served: Served) -> None:
    # Logout needs a live login session now; a cookie-less caller cannot drive it.
    assert _req(served, "/api/logout", method="POST", body={})[0] == 403


def test_logout_needs_a_same_origin_request(served: Served) -> None:
    login = _sign_in(served)
    status, _, payload = _req(
        served,
        "/api/logout",
        method="POST",
        body={},
        cookies=[login],
        headers={"Origin": "http://evil.example.com"},
    )
    assert status == 403
    assert _json(payload)["error"] == "bad_origin"
    # A cross-site page could not sign the user out.
    assert _req(served, "/api/run", cookies=[login])[0] == 200


# --- nothing leaks ---
def test_credentials_never_appear_in_any_response_or_log(
    served: Served, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[bytes] = []
    with caplog.at_level(logging.DEBUG):
        for body in (
            {"username": USERNAME, "password": "my-secret-guess-123"},
            {"username": "mallory", "password": PASSWORD},
            {"username": USERNAME, "password": PASSWORD},
            {"username": 5, "password": PASSWORD},
        ):
            _status, headers, payload = _req(served, "/api/login", method="POST", body=body)
            seen += [payload, json.dumps(headers).encode()]
        seen.append(_req(served, "/api/login/status")[2])
        seen.append(_req(served, "/api/capabilities")[2])
    everything = b"\n".join(seen).decode() + "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("my-secret-guess-123", PASSWORD, "pbkdf2_sha256", "mallory"):
        assert secret not in everything, secret


def test_the_hash_is_not_served_as_a_static_file(served: Served) -> None:
    status, _, payload = _req(served, "/strix/interface/viewer/login.py")
    assert b"pbkdf2_sha256$" not in payload
    assert status in (200, 404)  # falls back to the SPA shell, never the source


# --- login off: data is open, there is no sign-in ---
def test_with_login_off_the_data_is_open_without_any_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("strix.interface.viewer.server.bundle_dir", lambda: _make_assets(tmp_path))
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    _make_run(runs)
    httpd, url, _empty = serve(None, open_browser=False, runs_dir=runs)
    s = Served(url, httpd.server_address[1])
    try:
        assert _req(s, "/api/run")[0] == 200  # no cookie needed
        info = _json(_req(s, "/api/login/status")[2])
        assert info["login_required"] is False
        assert info["authenticated"] is True
        status, _, payload = _req(
            s, "/api/login", method="POST", body={"username": "x", "password": "y"}
        )
        assert status == 404
        assert _json(payload)["error"] == "login_disabled"
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- public pages and the seekable video ---
def test_the_landing_assets_are_public(served: Served) -> None:
    for path in ("/", "/logo.png", "/intro.mp4"):
        assert _req(served, path)[0] == 200, path


def test_the_page_alone_sets_no_cookie(served: Served) -> None:
    _, headers, _ = _req(served, "/")
    assert "Set-Cookie" not in headers


def test_a_video_request_without_a_range_is_whole_and_advertises_ranges(served: Served) -> None:
    status, headers, body = _req(served, "/intro.mp4")
    assert status == 200
    assert body == VIDEO
    assert headers["Accept-Ranges"] == "bytes"
    assert headers["Content-Type"] == "video/mp4"
    assert headers["Content-Length"] == str(len(VIDEO))
    assert "Content-Range" not in headers


@pytest.mark.parametrize(
    ("header", "first", "last"),
    [
        ("bytes=0-99", 0, 99),
        ("bytes=1000-1999", 1000, 1999),
        ("bytes=5000-", 5000, len(VIDEO) - 1),
        ("bytes=-200", len(VIDEO) - 200, len(VIDEO) - 1),
        ("bytes=0-0", 0, 0),
        ("bytes=5000-999999", 5000, len(VIDEO) - 1),
    ],
)
def test_range_requests_return_exactly_those_bytes(
    served: Served, header: str, first: int, last: int
) -> None:
    status, headers, body = _req(served, "/intro.mp4", headers={"Range": header})
    assert status == 206
    assert body == VIDEO[first : last + 1]
    assert headers["Content-Range"] == f"bytes {first}-{last}/{len(VIDEO)}"
    assert headers["Content-Length"] == str(last - first + 1)
    assert headers["Accept-Ranges"] == "bytes"
    assert headers["Content-Type"] == "video/mp4"


def test_a_range_past_the_end_is_416_with_the_size(served: Served) -> None:
    status, headers, body = _req(served, "/intro.mp4", headers={"Range": "bytes=999999-"})
    assert status == 416
    assert headers["Content-Range"] == f"bytes */{len(VIDEO)}"
    assert body == b""


@pytest.mark.parametrize("header", ["bytes=0-10,20-30", "items=0-5", "bytes=oops", "bytes=9-3"])
def test_unusable_ranges_just_send_the_whole_file(served: Served, header: str) -> None:
    status, _, body = _req(served, "/intro.mp4", headers={"Range": header})
    assert status == 200
    assert body == VIDEO


def test_the_spa_shell_ignores_ranges(served: Served) -> None:
    status, headers, body = _req(served, "/", headers={"Range": "bytes=0-5"})
    assert status == 200
    assert body == b"<!doctype html><div id=root></div>"
    assert "Accept-Ranges" not in headers


def test_the_whole_video_can_be_reassembled_from_ranges(served: Served) -> None:
    # What a browser does when seeking around: arbitrary slices must tile exactly.
    pieces = []
    for start in range(0, len(VIDEO), 777):
        end = min(start + 776, len(VIDEO) - 1)
        pieces.append(_req(served, "/intro.mp4", headers={"Range": f"bytes={start}-{end}"})[2])
    assert b"".join(pieces) == VIDEO


@pytest.mark.parametrize(
    "path", ["/../../etc/passwd", "/..%2f..%2fetc%2fpasswd", "/%2e%2e/%2e%2e/secret"]
)
def test_ranges_cannot_be_used_to_escape_the_bundle(served: Served, path: str) -> None:
    status, _, body = _req(served, path, headers={"Range": "bytes=0-50"})
    assert b"root:" not in body
    assert status in (200, 404)
