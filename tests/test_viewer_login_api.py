"""HTTP tests for viewer sign-in (second factor) and byte-range static serving.

A real ``ThreadingHTTPServer`` with a real :class:`LoginManager` (low iteration
count for speed). The properties that matter:

* the password endpoint cannot be reached without the secret-link session cookie;
* a login cookie alone grants nothing, and neither does the session cookie alone;
* every data route is gated once login is on, and nothing changes when it is off;
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

from strix.interface.viewer.login import MAX_FAILURES, LoginConfig, LoginManager, hash_password
from strix.interface.viewer.server import serve


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


USERNAME = "alice"
PASSWORD = "correct-horse-battery-staple"  # noqa: S105 - test-only value, not a real credential
VIDEO = bytes(range(256)) * 20  # 5120 deterministic bytes; any byte offset is checkable


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _manager(clock: Clock | None = None, ttl: float = 3600.0) -> LoginManager:
    config = LoginConfig(USERNAME, hash_password(PASSWORD, iterations=1000))
    return LoginManager(config, clock=clock, session_ttl=ttl)


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
    def __init__(self, url: str, token: str, port: int) -> None:
        self.url, self.token, self.port = url, token, port
        self.session_cookie = ""

    def session(self) -> str:
        """Open the secret link, as the browser does, and return the session cookie."""
        with urllib.request.urlopen(f"{self.url}/?token={self.token}") as resp:  # noqa: S310  # nosec B310
            raw = str(resp.headers.get("Set-Cookie", ""))
        return raw.split(";", 1)[0]


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Served]:
    monkeypatch.setattr("strix.interface.viewer.server.bundle_dir", lambda: _make_assets(tmp_path))
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    _make_run(runs)
    login = _manager()
    httpd, url, token = serve(None, open_browser=False, runs_dir=runs, login=login)
    s = Served(url, token, httpd.server_address[1])
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


def _sign_in(s: Served, session: str) -> str:
    status, headers, _ = _req(
        s,
        "/api/login",
        method="POST",
        body={"username": USERNAME, "password": PASSWORD},
        cookies=[session],
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
def test_status_for_a_browser_without_the_secret_link(served: Served) -> None:
    status, _, body = _req(served, "/api/login/status")
    assert status == 200
    assert _json(body) == {
        "login_required": True,
        "authenticated": False,
        "authorized_browser": False,
        "username": None,
    }


def test_status_with_only_the_session_cookie(served: Served) -> None:
    session = served.session()
    info = _json(_req(served, "/api/login/status", cookies=[session])[2])
    assert info["login_required"] is True
    assert info["authorized_browser"] is True
    assert info["authenticated"] is False
    assert info["username"] is None  # a name is never shown to someone who has not signed in


def test_status_after_signing_in_names_the_user(served: Served) -> None:
    session = served.session()
    login = _sign_in(served, session)
    info = _json(_req(served, "/api/login/status", cookies=[session, login])[2])
    assert info == {
        "login_required": True,
        "authenticated": True,
        "authorized_browser": True,
        "username": USERNAME,
    }


def test_status_is_never_cached(served: Served) -> None:
    assert _req(served, "/api/login/status")[1]["Cache-Control"] == "no-store"


# --- the first factor guards the password endpoint ---
def test_the_password_endpoint_is_unreachable_without_the_secret_link(served: Served) -> None:
    status, headers, body = _req(
        served, "/api/login", method="POST", body={"username": USERNAME, "password": PASSWORD}
    )
    assert status == 403
    assert _json(body) == {"error": "forbidden"}
    assert "Set-Cookie" not in headers


def test_guessing_without_the_secret_link_never_counts_toward_a_lockout(served: Served) -> None:
    # Strangers who cannot reach the form must not be able to lock the real user out.
    for i in range(MAX_FAILURES * 3):
        status, _, _ = _req(
            served,
            "/api/login",
            method="POST",
            body={"username": USERNAME, "password": f"guess-{i}"},
        )
        assert status == 403
    session = served.session()
    assert _sign_in(served, session)  # still works at once


def test_a_wrong_session_cookie_is_not_enough(served: Served) -> None:
    status, _, _ = _req(
        served,
        "/api/login",
        method="POST",
        body={"username": USERNAME, "password": PASSWORD},
        cookies=[f"strix_viewer_session_{served.port}=not-the-token"],
    )
    assert status == 403


# --- signing in ---
def test_correct_credentials_sign_in_and_set_a_hardened_cookie(served: Served) -> None:
    session = served.session()
    status, headers, body = _req(
        served,
        "/api/login",
        method="POST",
        body={"username": USERNAME, "password": PASSWORD},
        cookies=[session],
    )
    assert status == 200
    assert _json(body) == {"ok": True, "username": USERNAME}
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
    session = served.session()
    assert _sign_in(served, session) != _sign_in(served, session)


def test_wrong_password_and_unknown_user_look_identical(served: Served) -> None:
    session = served.session()
    wrong_password = _req(
        served,
        "/api/login",
        method="POST",
        body={"username": USERNAME, "password": "nope"},
        cookies=[session],
    )
    unknown_user = _req(
        served,
        "/api/login",
        method="POST",
        body={"username": "mallory", "password": PASSWORD},
        cookies=[session],
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
    status, headers, payload = _req(
        served, "/api/login", method="POST", body=body, cookies=[served.session()]
    )
    assert status == 401
    assert _json(payload)["error"] == "invalid_credentials"
    assert "Set-Cookie" not in headers


def test_extra_fields_are_ignored(served: Served) -> None:
    session = served.session()
    status, _, _ = _req(
        served,
        "/api/login",
        method="POST",
        body={"username": USERNAME, "password": PASSWORD, "admin": True},
        cookies=[session],
    )
    assert status == 200


@pytest.mark.parametrize("raw", [b"{nope", b"\xff\xfe\xfd", b"[1,2]", b'"str"', b"123", b"null"])
def test_malformed_bodies_are_400(served: Served, raw: bytes) -> None:
    status, _, payload = _req(
        served,
        "/api/login",
        method="POST",
        raw_body=raw,
        cookies=[served.session()],
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
        cookies=[served.session()],
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
        cookies=[served.session()],
    )
    assert status == 415
    assert _json(payload)["error"] == "invalid_content_type"


def test_a_foreign_origin_cannot_sign_in(served: Served) -> None:
    status, headers, payload = _req(
        served,
        "/api/login",
        method="POST",
        body={"username": USERNAME, "password": PASSWORD},
        cookies=[served.session()],
        headers={"Origin": "http://evil.example.com"},
    )
    assert status == 403
    assert _json(payload)["error"] == "bad_origin"
    assert "Set-Cookie" not in headers


def test_a_missing_content_length_is_411(served: Served) -> None:
    parsed = urlsplit(served.url)
    conn = http.client.HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port or 80)
    conn.putrequest("POST", "/api/login")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Cookie", served.session())
    conn.putheader("Transfer-Encoding", "chunked")
    conn.endheaders()
    assert conn.getresponse().status == 411
    conn.close()


# --- lock-out ---


def test_repeated_failures_lock_the_form_even_for_the_right_password(served: Served) -> None:
    session = served.session()
    for i in range(MAX_FAILURES):
        _req(
            served,
            "/api/login",
            method="POST",
            body={"username": USERNAME, "password": f"wrong-{i}"},
            cookies=[session],
        )

    status, headers, payload = _req(
        served,
        "/api/login",
        method="POST",
        body={"username": USERNAME, "password": PASSWORD},
        cookies=[session],
    )

    assert status == 429
    info = _json(payload)
    assert info["error"] == "rate_limited"
    assert 1 <= info["retry_after"] <= 60
    assert headers["Retry-After"] == str(info["retry_after"])
    assert "Set-Cookie" not in headers


# --- the second factor gates every data route ---
@pytest.mark.parametrize("path", DATA_ROUTES)
def test_data_routes_need_both_factors(served: Served, path: str) -> None:
    session = served.session()
    login = _sign_in(served, session)

    nothing = _req(served, path)[0]
    session_only = _req(served, path, cookies=[session])[0]
    login_only = _req(served, path, cookies=[login])[0]
    both = _req(served, path, cookies=[session, login])[0]

    assert nothing == 403
    assert session_only == 403, "the secret link alone must no longer open the data"
    assert login_only == 403, "a login cookie without the secret link grants nothing"
    assert both == 200


def test_a_forged_login_cookie_grants_nothing(served: Served) -> None:
    session = served.session()
    forged = f"strix_viewer_login_{served.port}=" + "A" * 43
    assert _req(served, "/api/run", cookies=[session, forged])[0] == 403


def test_the_login_cookie_from_another_port_is_not_accepted(served: Served) -> None:
    session = served.session()
    login = _sign_in(served, session)
    token = login.split("=", 1)[1]
    wrong_name = f"strix_viewer_login_{served.port + 1}={token}"
    assert _req(served, "/api/run", cookies=[session, wrong_name])[0] == 403


def test_run_history_stays_locked_until_signed_in(served: Served) -> None:
    session = served.session()
    login = _sign_in(served, session)
    before = _json(_req(served, "/api/runs", cookies=[session])[2])
    after = _json(_req(served, "/api/runs", cookies=[session, login])[2])
    assert before["locked"] is True
    assert before["runs"] == []
    assert after["locked"] is False
    assert [r["name"] for r in after["runs"]] == ["web-1"]


def test_settings_and_launch_posts_are_gated_too(served: Served) -> None:
    session = served.session()
    for method, path in (("GET", "/api/settings"), ("GET", "/api/notifications")):
        assert _req(served, path, method=method, cookies=[session])[0] == 403
    status, _, _ = _req(
        served, "/api/scans", method="POST", body={"target": "x"}, cookies=[session]
    )
    assert status == 403


def test_sessions_expire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("strix.interface.viewer.server.bundle_dir", lambda: _make_assets(tmp_path))
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    _make_run(runs)
    clock = Clock()
    httpd, url, token = serve(
        None, open_browser=False, runs_dir=runs, login=_manager(clock, ttl=100.0)
    )
    s = Served(url, token, httpd.server_address[1])
    try:
        session = s.session()
        login = _sign_in(s, session)
        assert _req(s, "/api/run", cookies=[session, login])[0] == 200
        clock.now += 101
        assert _req(s, "/api/run", cookies=[session, login])[0] == 403
        info = _json(_req(s, "/api/login/status", cookies=[session, login])[2])
        assert info["authenticated"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- sign out ---
def test_logout_ends_the_session_and_clears_the_cookie(served: Served) -> None:
    session = served.session()
    login = _sign_in(served, session)
    assert _req(served, "/api/run", cookies=[session, login])[0] == 200

    status, headers, payload = _req(
        served, "/api/logout", method="POST", body={}, cookies=[session, login]
    )

    assert status == 200
    assert _json(payload) == {"ok": True}
    assert "Max-Age=0" in headers["Set-Cookie"]
    # The server forgot the session, not just the browser: replaying the old cookie fails.
    assert _req(served, "/api/run", cookies=[session, login])[0] == 403


def test_logout_only_ends_the_callers_own_session(served: Served) -> None:
    session = served.session()
    mine, other = _sign_in(served, session), _sign_in(served, session)
    _req(served, "/api/logout", method="POST", body={}, cookies=[session, mine])
    assert _req(served, "/api/run", cookies=[session, other])[0] == 200


def test_logout_is_idempotent(served: Served) -> None:
    session = served.session()
    for _ in range(3):
        assert _req(served, "/api/logout", method="POST", body={}, cookies=[session])[0] == 200


def test_logout_needs_the_secret_link_and_a_same_origin_request(served: Served) -> None:
    session = served.session()
    login = _sign_in(served, session)
    assert _req(served, "/api/logout", method="POST", body={})[0] == 403
    status, _, payload = _req(
        served,
        "/api/logout",
        method="POST",
        body={},
        cookies=[session, login],
        headers={"Origin": "http://evil.example.com"},
    )
    assert status == 403
    assert _json(payload)["error"] == "bad_origin"
    # A cross-site page could not sign the user out.
    assert _req(served, "/api/run", cookies=[session, login])[0] == 200


# --- nothing leaks ---
def test_credentials_never_appear_in_any_response_or_log(
    served: Served, caplog: pytest.LogCaptureFixture
) -> None:
    session = served.session()
    seen: list[bytes] = []
    with caplog.at_level(logging.DEBUG):
        for body in (
            {"username": USERNAME, "password": "my-secret-guess-123"},
            {"username": "mallory", "password": PASSWORD},
            {"username": USERNAME, "password": PASSWORD},
            {"username": 5, "password": PASSWORD},
        ):
            _status, headers, payload = _req(
                served, "/api/login", method="POST", body=body, cookies=[session]
            )
            seen += [payload, json.dumps(headers).encode()]
        seen.append(_req(served, "/api/login/status", cookies=[session])[2])
        seen.append(_req(served, "/api/capabilities")[2])
    everything = b"\n".join(seen).decode() + "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("my-secret-guess-123", PASSWORD, "pbkdf2_sha256", "mallory"):
        assert secret not in everything, secret


def test_the_hash_is_not_served_as_a_static_file(served: Served) -> None:
    status, _, payload = _req(served, "/strix/interface/viewer/login.py")
    assert b"pbkdf2_sha256$" not in payload
    assert status in (200, 404)  # falls back to the SPA shell, never the source


# --- login off: nothing changes ---
def test_with_login_off_the_session_cookie_alone_works_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("strix.interface.viewer.server.bundle_dir", lambda: _make_assets(tmp_path))
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    _make_run(runs)
    httpd, url, token = serve(None, open_browser=False, runs_dir=runs)
    s = Served(url, token, httpd.server_address[1])
    try:
        session = s.session()
        assert _req(s, "/api/run", cookies=[session])[0] == 200
        assert _req(s, "/api/run")[0] == 403
        info = _json(_req(s, "/api/login/status", cookies=[session])[2])
        assert info["login_required"] is False
        assert info["authenticated"] is True
        status, _, payload = _req(
            s,
            "/api/login",
            method="POST",
            body={"username": "x", "password": "y"},
            cookies=[session],
        )
        assert status == 404
        assert _json(payload)["error"] == "login_disabled"
        assert _req(s, "/api/logout", method="POST", body={}, cookies=[session])[0] == 200
        assert _req(s, "/api/run", cookies=[session])[0] == 200  # logout is a no-op here
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- public pages and the seekable video ---
def test_the_landing_assets_are_public(served: Served) -> None:
    for path in ("/", "/logo.png", "/intro.mp4"):
        assert _req(served, path)[0] == 200, path


def test_the_page_alone_does_not_mint_the_session(served: Served) -> None:
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
