"""Real-server tests for the LLM *profiles* API (``/api/settings/profiles*``).

Mirrors ``test_llm_settings_api``: one real :class:`ThreadingHTTPServer` with a
real :class:`SettingsStore` + :class:`ProfileStore` on a temp path, a fake
resolver (no DNS) and a fake tester (no child). Asserts the auth cookie, the
read-only (exposed without sign-in) behaviour, the header gates, the happy paths, the new
error codes, the shared 30/min change limit, and that a recognisable key never
leaks across any response.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

import pytest

from strix.interface.viewer import credentials_store
from strix.interface.viewer.llm_profiles import ProfileStore
from strix.interface.viewer.llm_settings import SettingsStore
from strix.interface.viewer.login import LoginConfig, LoginManager, hash_password
from strix.interface.viewer.server import serve


if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from strix.interface.viewer.launch_errors import LaunchError


SECRET_KEY = "sk-PROFILEKEY1234567890abcdefONE"  # noqa: S105 - test-only fake secret


class FakeTester:
    def __init__(self) -> None:
        self.calls: list[dict[str, str | None]] = []
        self.result: dict[str, Any] = {"ok": True, "message": "ok"}
        self.error: LaunchError | None = None

    def run(self, values: Any) -> dict[str, Any]:
        self.calls.append(dict(values))
        if self.error is not None:
            raise self.error
        return dict(self.result)


def _ok_resolver(_host: str, _timeout: float) -> list[str]:
    return ["93.184.216.34"]


def _bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assets = tmp_path / "bundle"
    assets.mkdir(exist_ok=True)
    (assets / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    monkeypatch.setattr("strix.interface.viewer.server.bundle_dir", lambda: assets)


_TEST_USER = "alice"
_TEST_PW = "pw-correct-horse"  # test-only fake password


@pytest.fixture(autouse=True)
def _tmp_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        credentials_store, "CREDENTIALS_PATH", tmp_path / ".strix" / "viewer-users.db"
    )


def _login_manager() -> LoginManager:
    return LoginManager(LoginConfig(_TEST_USER, hash_password(_TEST_PW, iterations=1000)))


def _login_cookie(url: str, *, origin: str | None = None) -> str:
    body = json.dumps({"username": _TEST_USER, "password": _TEST_PW}).encode()
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(  # noqa: S310
        url + "/api/login", data=body, headers=headers, method="POST"
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310  # nosec B310
        assert resp.status == 200
        return str(resp.headers.get("Set-Cookie", "")).split(";", 1)[0]


def _request(
    url: str,
    path: str,
    *,
    method: str = "GET",
    body: Mapping[str, object] | None = None,
    cookie: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    hdrs: dict[str, str] = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    if cookie:
        hdrs["Cookie"] = cookie
    req = urllib.request.Request(url + path, data=data, headers=hdrs, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310  # nosec B310
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _serve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    host: str = "127.0.0.1",
    with_store: bool = True,
    with_login: bool = True,
) -> tuple[Any, str, SettingsStore | None, FakeTester]:
    _bundle(tmp_path, monkeypatch)
    runs_dir = tmp_path / "strix_runs"
    runs_dir.mkdir(exist_ok=True)
    config = tmp_path / "cli-config.json"
    store = SettingsStore(path=config, environ={}, resolver=_ok_resolver) if with_store else None
    profile_store = ProfileStore(store) if store is not None else None
    tester = FakeTester()
    httpd, url, _empty = serve(
        None,
        host=host,
        open_browser=False,
        runs_dir=runs_dir,
        launcher=None,
        settings_store=store,
        connection_tester=tester,  # type: ignore[arg-type]
        profile_store=profile_store,
        login=_login_manager() if with_login else None,
    )
    return httpd, url, store, tester


# --- authorization ----------------------------------------------------------


def test_routes_require_cookie(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        checks: list[tuple[str, str, dict[str, object] | None]] = [
            ("GET", "/api/settings/profiles", None),
            ("POST", "/api/settings/profiles/save", {"name": "P", "model": "openai/gpt-5"}),
            ("POST", "/api/settings/profiles/activate", {"id": "p-00000000"}),
            ("POST", "/api/settings/profiles/delete", {"id": "p-00000000"}),
        ]
        for method, path, body in checks:
            status, _, payload = _request(url, path, method=method, body=body)
            assert status == 403
            assert json.loads(payload)["error"] == "forbidden"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_unavailable_when_no_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _store, _tester = _serve(tmp_path, monkeypatch, with_store=False)
    try:
        cookie = _login_cookie(url)
        status, _, payload = _request(url, "/api/settings/profiles", cookie=cookie)
        assert status == 403
        assert json.loads(payload)["error"] == "settings_unavailable"
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- editability on a non-loopback bind ---------------------------------------

_WRITE_ROUTES: tuple[tuple[str, str, dict[str, object]], ...] = (
    ("POST", "/api/settings/profiles/save", {"name": "P", "model": "openai/gpt-5"}),
    ("POST", "/api/settings/profiles/activate", {"id": "p-0"}),
    ("POST", "/api/settings/profiles/delete", {"id": "p-0"}),
)


def test_non_loopback_with_login_is_editable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exposed on 0.0.0.0 WITH sign-in: only a signed-in caller reaches a write,
    # so remote editing is allowed.
    httpd, _url, _store, _tester = _serve(tmp_path, monkeypatch, host="0.0.0.0")  # nosec B104
    try:
        port = httpd.server_address[1]
        loopback = f"http://127.0.0.1:{port}"
        origin = f"http://0.0.0.0:{port}"
        cookie = _login_cookie(loopback, origin=origin)

        status, _, payload = _request(loopback, "/api/settings/profiles", cookie=cookie)
        assert status == 200
        assert json.loads(payload)["editable"] is True

        status, _, payload = _request(
            loopback,
            "/api/settings/profiles/save",
            method="POST",
            body={"name": "Remote", "model": "openai/gpt-5"},
            cookie=cookie,
            headers={"Origin": origin},
        )
        assert status == 200, payload
        assert json.loads(payload)["profiles"][0]["name"] == "Remote"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_non_loopback_without_login_is_read_only_but_get_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exposed on 0.0.0.0 with sign-in OFF: data is open, config stays read-only.
    httpd, _url, _store, _tester = _serve(
        tmp_path,
        monkeypatch,
        host="0.0.0.0",  # nosec B104
        with_login=False,
    )
    try:
        port = httpd.server_address[1]
        loopback = f"http://127.0.0.1:{port}"

        status, _, payload = _request(loopback, "/api/settings/profiles")
        assert status == 200
        assert json.loads(payload)["editable"] is False

        for method, path, body in _WRITE_ROUTES:
            status, _, payload = _request(loopback, path, method=method, body=body)
            assert status == 403, f"{method} {path}"
            assert json.loads(payload)["error"] == "settings_read_only"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_remote_config_can_be_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # STRIX_VIEWER_REMOTE_CONFIG=off restores loopback-only editing even with
    # sign-in on.
    monkeypatch.setenv("STRIX_VIEWER_REMOTE_CONFIG", "off")
    httpd, _url, _store, _tester = _serve(tmp_path, monkeypatch, host="0.0.0.0")  # nosec B104
    try:
        port = httpd.server_address[1]
        loopback = f"http://127.0.0.1:{port}"
        origin = f"http://0.0.0.0:{port}"
        cookie = _login_cookie(loopback, origin=origin)

        status, _, payload = _request(loopback, "/api/settings/profiles", cookie=cookie)
        assert status == 200
        assert json.loads(payload)["editable"] is False

        for method, path, body in _WRITE_ROUTES:
            status, _, payload = _request(
                loopback,
                path,
                method=method,
                body=body,
                cookie=cookie,
                headers={"Origin": origin},
            )
            assert status == 403, f"{method} {path}"
            assert json.loads(payload)["error"] == "settings_read_only"
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- header gates -----------------------------------------------------------


def test_save_requires_json_content_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _login_cookie(url)
        status, _, payload = _request(
            url,
            "/api/settings/profiles/save",
            method="POST",
            body={"name": "P", "model": "openai/gpt-5"},
            cookie=cookie,
            headers={"Content-Type": "text/plain"},
        )
        assert status == 415
        assert json.loads(payload)["error"] == "invalid_content_type"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_save_requires_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _login_cookie(url)
        status, _, payload = _request(
            url,
            "/api/settings/profiles/save",
            method="POST",
            body={"name": "P", "model": "openai/gpt-5"},
            cookie=cookie,
            headers={"Origin": "http://evil.example"},
        )
        assert status == 403
        assert json.loads(payload)["error"] == "bad_origin"
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- happy path -------------------------------------------------------------


def test_create_activate_delete_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _login_cookie(url)
        status, _, payload = _request(
            url,
            "/api/settings/profiles/save",
            method="POST",
            body={"name": "Prod", "preset": "openai", "model": "openai/gpt-5"},
            cookie=cookie,
        )
        assert status == 200
        view = json.loads(payload)
        pid = view["profiles"][0]["id"]
        assert view["active_id"] is None  # save never activates

        status, _, payload = _request(
            url, "/api/settings/profiles/activate", method="POST", body={"id": pid}, cookie=cookie
        )
        assert status == 200
        assert json.loads(payload)["active_id"] == pid

        status, _, payload = _request(
            url, "/api/settings/profiles/delete", method="POST", body={"id": pid}, cookie=cookie
        )
        # Active cannot be deleted.
        assert status == 409
        assert json.loads(payload)["error"] == "cannot_delete_active_profile"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_get_returns_presets_and_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _login_cookie(url)
        status, _, payload = _request(url, "/api/settings/profiles", cookie=cookie)
        assert status == 200
        view = json.loads(payload)
        assert view["max_profiles"] == 20
        assert any(p["id"] == "openai" for p in view["presets"])
        assert view["profiles"] == []
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- error codes ------------------------------------------------------------


def test_duplicate_name_is_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _login_cookie(url)
        body = {"name": "Prod", "model": "openai/gpt-5"}
        _request(url, "/api/settings/profiles/save", method="POST", body=body, cookie=cookie)
        status, _, payload = _request(
            url, "/api/settings/profiles/save", method="POST", body=body, cookie=cookie
        )
        assert status == 400
        assert json.loads(payload)["error"] == "profile_name_taken"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_activate_unknown_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _login_cookie(url)
        status, _, payload = _request(
            url,
            "/api/settings/profiles/activate",
            method="POST",
            body={"id": "p-deadbeef"},
            cookie=cookie,
        )
        assert status == 404
        assert json.loads(payload)["error"] == "profile_not_found"
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- test endpoint with profile_id ------------------------------------------


def test_test_endpoint_accepts_profile_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _store, tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _login_cookie(url)
        _, _, payload = _request(
            url,
            "/api/settings/profiles/save",
            method="POST",
            body={"name": "Prod", "model": "openai/gpt-5", "api_key": SECRET_KEY},
            cookie=cookie,
        )
        pid = json.loads(payload)["profiles"][0]["id"]
        status, _, payload = _request(
            url,
            "/api/settings/test",
            method="POST",
            body={"profile_id": pid},
            cookie=cookie,
        )
        assert status == 200
        # The tester saw that profile's saved values.
        assert tester.calls[-1]["model"] == "openai/gpt-5"
        assert tester.calls[-1]["api_key"] == SECRET_KEY
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- shared rate limit ------------------------------------------------------


def test_change_limit_shared_with_other_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpd, url, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _login_cookie(url)
        # 29 flat resets + one profile save exhaust the 30/min window; the
        # 31st change (a profile save) is rate-limited.
        for _ in range(29):
            status, _, _ = _request(
                url, "/api/settings/reset", method="POST", body={"confirm": True}, cookie=cookie
            )
            assert status == 200
        status, _, _ = _request(
            url,
            "/api/settings/profiles/save",
            method="POST",
            body={"name": "P", "model": "openai/gpt-5"},
            cookie=cookie,
        )
        assert status == 200
        status, headers, payload = _request(
            url,
            "/api/settings/profiles/save",
            method="POST",
            body={"name": "Q", "model": "openai/gpt-5"},
            cookie=cookie,
        )
        assert status == 429
        assert json.loads(payload)["error"] == "rate_limited"
        assert "Retry-After" in headers
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- no key leak ------------------------------------------------------------


def test_key_never_leaks_across_responses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _login_cookie(url)
        responses: list[bytes] = []
        _, _, p = _request(
            url,
            "/api/settings/profiles/save",
            method="POST",
            body={"name": "Prod", "model": "openai/gpt-5", "api_key": SECRET_KEY},
            cookie=cookie,
        )
        responses.append(p)
        pid = json.loads(p)["profiles"][0]["id"]
        _, _, p = _request(
            url, "/api/settings/profiles/activate", method="POST", body={"id": pid}, cookie=cookie
        )
        responses.append(p)
        _, _, p = _request(url, "/api/settings/profiles", cookie=cookie)
        responses.append(p)
        for raw in responses:
            assert SECRET_KEY.encode() not in raw
    finally:
        httpd.shutdown()
        httpd.server_close()
