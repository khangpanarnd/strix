"""HTTP integration tests for the settings API (Step 5.2; BR-1..BR-5, BR-12..BR-16).

These run a real ``ThreadingHTTPServer`` (like ``test_viewer_launch_api``) with a
real :class:`SettingsStore` on a temp config path, a fake resolver (no DNS), a
fake :class:`ConnectionTester` (no child process, no network), and the launch
feature's ``FakeLauncher`` for the ``scan_active`` wiring. They assert the gate
order, the read-only (non-loopback) behaviour, every error code, the happy-path
round trip, the key-required rules, the rate limit, capabilities in both modes,
the connection-test result/error mapping, and — across every endpoint including
error responses, the server logs, and the static handler — that a recognisable
API key never leaks.
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

from strix.interface.viewer.launch_errors import LaunchError
from strix.interface.viewer.launch_request import LaunchLimits
from strix.interface.viewer.llm_settings import SettingsStore
from strix.interface.viewer.server import serve


if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


# A recognisable secret used throughout: it must never appear in any response,
# log line, or on-disk-served asset (BR-12, NFR-1). Not a real credential.
SECRET_KEY = "sk-SECRETKEY1234567890abcdefONE"  # noqa: S105 - test-only fake secret
SECRET_KEY_TWO = "sk-SECRETKEY1234567890abcdefTWO"  # noqa: S105 - test-only fake secret


class FakeLauncher:
    """Minimal launcher: only ``active_run`` and ``limits`` are read here."""

    def __init__(self, *, active: str | None = None) -> None:
        self.limits = LaunchLimits()
        self._active = active

    def active_run(self) -> str | None:
        return self._active


class FakeTester:
    """Records the resolved values and returns a canned result or raises."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str | None]] = []
        self.result: dict[str, Any] = {
            "ok": True,
            "latency_ms": 12,
            "category": None,
            "status": None,
            "message": "The connection succeeded.",
        }
        self.error: LaunchError | None = None

    def run(self, values: Any) -> dict[str, Any]:
        self.calls.append(dict(values))
        if self.error is not None:
            raise self.error
        return dict(self.result)


def _ok_resolver(_host: str, _timeout: float) -> list[str]:
    """A public, non-metadata address so https bases always validate."""
    return ["93.184.216.34"]


def _bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assets = tmp_path / "bundle"
    assets.mkdir(exist_ok=True)
    (assets / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    monkeypatch.setattr("strix.interface.viewer.server.bundle_dir", lambda: assets)


def _session_cookie(url: str, token: str) -> str:
    bootstrap = f"{url}/?token={token}"
    with urllib.request.urlopen(bootstrap) as resp:  # noqa: S310  # nosec B310
        raw = str(resp.headers.get("Set-Cookie", ""))
    return raw.split(";", 1)[0]


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
    with_tester: bool = True,
    launcher: FakeLauncher | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[Any, str, str, SettingsStore | None, FakeTester | None]:
    """Start a real server with a temp-path store and a fake tester."""
    _bundle(tmp_path, monkeypatch)
    runs_dir = tmp_path / "strix_runs"
    runs_dir.mkdir(exist_ok=True)
    config = tmp_path / "cli-config.json"
    store = (
        SettingsStore(path=config, environ=environ or {}, resolver=_ok_resolver)
        if with_store
        else None
    )
    tester = FakeTester() if with_tester else None
    httpd, url, token = serve(
        None,
        host=host,
        open_browser=False,
        runs_dir=runs_dir,
        launcher=launcher,  # type: ignore[arg-type]
        settings_store=store,
        connection_tester=tester,  # type: ignore[arg-type]
    )
    return httpd, url, token, store, tester


# --- authorization (BR-1) ---------------------------------------------------


def test_all_four_routes_require_cookie(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _token, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        checks: list[tuple[str, str, dict[str, object] | None]] = [
            ("GET", "/api/settings", None),
            ("PUT", "/api/settings", {"model": "openai/gpt-5"}),
            ("POST", "/api/settings/reset", {"confirm": True}),
            ("POST", "/api/settings/test", {"model": "openai/gpt-5"}),
        ]
        for method, path, body in checks:
            status, _, payload = _request(url, path, method=method, body=body)
            assert status == 403, f"{method} {path} should be 403 without cookie"
            assert json.loads(payload)["error"] == "forbidden"
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- read-only (non-loopback) bind (BR-2) -----------------------------------


def test_non_loopback_bind_is_read_only_but_get_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpd, _url, token, _store, _tester = _serve(tmp_path, monkeypatch, host="0.0.0.0")  # nosec B104
    try:
        port = httpd.server_address[1]
        loopback = f"http://127.0.0.1:{port}"
        cookie = _session_cookie(loopback, token)

        # GET works and reports editable=false.
        status, _, payload = _request(loopback, "/api/settings", cookie=cookie)
        assert status == 200
        assert json.loads(payload)["editable"] is False

        # PUT, reset and test are all 403 settings_read_only.
        for method, path, body in (
            ("PUT", "/api/settings", {"model": "openai/gpt-5"}),
            ("POST", "/api/settings/reset", {"confirm": True}),
            ("POST", "/api/settings/test", {"model": "openai/gpt-5"}),
        ):
            status, _, payload = _request(loopback, path, method=method, body=body, cookie=cookie)
            assert status == 403, f"{method} {path}"
            assert json.loads(payload)["error"] == "settings_read_only"
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- gate order: origin / content-type / length -----------------------------


def test_foreign_origin_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        status, _, payload = _request(
            url,
            "/api/settings",
            method="PUT",
            body={"model": "openai/gpt-5"},
            cookie=cookie,
            headers={"Origin": "http://evil.example.com"},
        )
        assert status == 403
        assert json.loads(payload)["error"] == "bad_origin"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_wrong_content_type_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        data = json.dumps({"model": "openai/gpt-5"}).encode()
        req = urllib.request.Request(  # noqa: S310
            url + "/api/settings",
            data=data,
            headers={"Content-Type": "text/plain", "Cookie": cookie},
            method="PUT",
        )
        try:
            with urllib.request.urlopen(req) as resp:  # noqa: S310  # nosec B310
                status, payload = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, exc.read()
        assert status == 415
        assert json.loads(payload)["error"] == "invalid_content_type"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_missing_content_length_is_411(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        parsed = urlsplit(url)
        conn = http.client.HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port or 80)
        conn.putrequest("PUT", "/api/settings")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Cookie", cookie)
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        conn.send(b"0\r\n\r\n")
        resp = conn.getresponse()
        status, payload = resp.status, resp.read()
        conn.close()
        assert status == 411
        assert json.loads(payload)["error"] == "length_required"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_oversized_body_is_413(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        big = {"model": "openai/gpt-5", "api_key": "x" * 20_000}
        status, _, payload = _request(url, "/api/settings", method="PUT", body=big, cookie=cookie)
        assert status == 413
        assert json.loads(payload)["error"] == "body_too_large"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_unknown_field_is_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        status, _, payload = _request(
            url, "/api/settings", method="PUT", body={"bogus": 1}, cookie=cookie
        )
        assert status == 400
        assert json.loads(payload)["error"] == "unknown_field"
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- happy path: save then GET reflect --------------------------------------


def test_save_then_get_reflects_ui_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, store, _tester = _serve(tmp_path, monkeypatch)
    assert store is not None
    try:
        cookie = _session_cookie(url, token)
        status, _, payload = _request(
            url,
            "/api/settings",
            method="PUT",
            body={
                "model": "anthropic/claude-sonnet-4-5",
                "api_key": SECRET_KEY,
                "reasoning_effort": "high",
            },
            cookie=cookie,
        )
        assert status == 200
        view = json.loads(payload)
        assert view["managed"] is True
        assert view["editable"] is True
        assert view["fields"]["model"]["value"] == "anthropic/claude-sonnet-4-5"
        assert view["fields"]["model"]["source"] == "ui"
        # The key is {set, source} only — never a value.
        assert view["fields"]["api_key"] == {"set": True, "source": "ui"}
        assert "warnings" in view

        status, _, payload = _request(url, "/api/settings", cookie=cookie)
        assert status == 200
        view = json.loads(payload)
        assert view["fields"]["api_key"]["set"] is True
        assert view["fields"]["model"]["value"] == "anthropic/claude-sonnet-4-5"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_scan_active_reflected_from_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = FakeLauncher(active="web-live")
    httpd, url, token, _store, _tester = _serve(tmp_path, monkeypatch, launcher=launcher)
    try:
        cookie = _session_cookie(url, token)
        status, _, payload = _request(url, "/api/settings", cookie=cookie)
        assert status == 200
        assert json.loads(payload)["scan_active"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- key never leaks (BR-12, NFR-1) across every endpoint -------------------


def test_key_never_appears_in_any_response_log_or_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    httpd, url, token, store, tester = _serve(tmp_path, monkeypatch)
    assert store is not None and tester is not None
    tester.error = None
    collected: list[bytes] = []
    try:
        cookie = _session_cookie(url, token)
        with caplog.at_level(logging.DEBUG):
            # Save with the key.
            _, _, body = _request(
                url,
                "/api/settings",
                method="PUT",
                body={"model": "anthropic/claude-sonnet-4-5", "api_key": SECRET_KEY},
                cookie=cookie,
            )
            collected.append(body)
            # GET.
            _, _, body = _request(url, "/api/settings", cookie=cookie)
            collected.append(body)
            # Test (echoes nothing of the key).
            _, _, body = _request(url, "/api/settings/test", method="POST", body={}, cookie=cookie)
            collected.append(body)
            # An error response that echoes user input — but never the key.
            _, _, body = _request(
                url,
                "/api/settings",
                method="PUT",
                body={"model": "has space", "api_key": SECRET_KEY},
                cookie=cookie,
            )
            collected.append(body)
            # Reset.
            _, _, body = _request(
                url, "/api/settings/reset", method="POST", body={"confirm": True}, cookie=cookie
            )
            collected.append(body)

        for body in collected:
            assert SECRET_KEY.encode() not in body

        log_text = "\n".join(record.getMessage() for record in caplog.records)
        assert SECRET_KEY not in log_text

        # The key IS on disk (that is its home) but the viewer never serves the
        # config directory — only the bundle. A traversal path is rejected and
        # the SPA fallback serves index.html (HTTP 200), so the guarantee is
        # that the config bytes never appear in what is served, not the status.
        for probe in (
            "/cli-config.json",
            "/../cli-config.json",
            "/%2e%2e/cli-config.json",
            "/..%2fcli-config.json",
        ):
            _status, _, served = _request(url, probe)
            assert SECRET_KEY.encode() not in served
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- key-required rules (BR-13, BR-14) --------------------------------------


def test_base_change_with_saved_key_requires_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpd, url, token, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        # Save a key first.
        _request(
            url,
            "/api/settings",
            method="PUT",
            body={"model": "anthropic/claude-sonnet-4-5", "api_key": SECRET_KEY},
            cookie=cookie,
        )
        # Changing the base without re-entering the key is refused.
        status, _, payload = _request(
            url,
            "/api/settings",
            method="PUT",
            body={"api_base": "https://proxy.example.com"},
            cookie=cookie,
        )
        assert status == 400
        assert json.loads(payload)["error"] == "key_required_for_base_change"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_first_save_with_shell_only_key_requires_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The key exists only in the viewer's shell environment, never in the file;
    # the first managed save must re-enter it (BR-14).
    httpd, url, token, _store, _tester = _serve(
        tmp_path, monkeypatch, environ={"LLM_API_KEY": SECRET_KEY}
    )
    try:
        cookie = _session_cookie(url, token)
        status, _, payload = _request(
            url,
            "/api/settings",
            method="PUT",
            body={"model": "anthropic/claude-sonnet-4-5"},
            cookie=cookie,
        )
        assert status == 400
        assert json.loads(payload)["error"] == "key_required_first_save"
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- clear and reset --------------------------------------------------------


def test_clear_field_then_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        _request(
            url,
            "/api/settings",
            method="PUT",
            body={"model": "anthropic/claude-sonnet-4-5", "api_key": SECRET_KEY},
            cookie=cookie,
        )
        # Clear the key.
        status, _, payload = _request(
            url, "/api/settings", method="PUT", body={"clear": ["api_key"]}, cookie=cookie
        )
        assert status == 200
        assert json.loads(payload)["fields"]["api_key"]["set"] is False

        # Reset removes the marker entirely.
        status, _, payload = _request(
            url, "/api/settings/reset", method="POST", body={"confirm": True}, cookie=cookie
        )
        assert status == 200
        assert json.loads(payload)["managed"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_reset_without_confirm_is_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        status, _, payload = _request(
            url, "/api/settings/reset", method="POST", body={"confirm": False}, cookie=cookie
        )
        assert status == 400
        assert json.loads(payload)["error"] == "invalid_json"
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- rate limit (BR-4: 30 changes per minute) -----------------------------


def test_change_rate_limit_is_429_with_retry_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpd, url, token, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        # 30 successful changes.
        for i in range(30):
            status, _, _ = _request(
                url,
                "/api/settings",
                method="PUT",
                body={"reasoning_effort": "high" if i % 2 else "low"},
                cookie=cookie,
            )
            assert status == 200, f"change {i} should succeed"
        # The 31st within the window is rate-limited.
        status, headers, payload = _request(
            url, "/api/settings", method="PUT", body={"reasoning_effort": "high"}, cookie=cookie
        )
        assert status == 429
        assert json.loads(payload)["error"] == "rate_limited"
        assert headers.get("Retry-After") == "60"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_test_endpoint_not_counted_against_change_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The connection test has its own 6/min window inside the tester, so it must
    # NOT consume the 10/min change window. Fire 20 tests, then a save still works.
    httpd, url, token, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        for _ in range(20):
            status, _, _ = _request(
                url,
                "/api/settings/test",
                method="POST",
                body={"model": "openai/gpt-5"},
                cookie=cookie,
            )
            assert status == 200
        status, _, _ = _request(
            url, "/api/settings", method="PUT", body={"reasoning_effort": "high"}, cookie=cookie
        )
        assert status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- capabilities in both modes ---------------------------------------------


def test_capabilities_reports_llm_settings_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpd, url, _token, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        status, _, payload = _request(url, "/api/capabilities")
        assert status == 200
        caps = json.loads(payload)["llm_settings"]
        assert caps == {"available": True, "editable": True}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_capabilities_reports_llm_settings_unavailable_without_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpd, url, token, _store, _tester = _serve(
        tmp_path, monkeypatch, with_store=False, with_tester=False
    )
    try:
        status, _, payload = _request(url, "/api/capabilities")
        assert status == 200
        caps = json.loads(payload)["llm_settings"]
        assert caps["available"] is False

        # And the routes answer settings_unavailable with the cookie present.
        cookie = _session_cookie(url, token)
        for method, path, body in (
            ("GET", "/api/settings", None),
            ("PUT", "/api/settings", {"model": "openai/gpt-5"}),
            ("POST", "/api/settings/reset", {"confirm": True}),
            ("POST", "/api/settings/test", {"model": "openai/gpt-5"}),
        ):
            status, _, payload = _request(url, path, method=method, body=body, cookie=cookie)
            assert status == 403, f"{method} {path}"
            assert json.loads(payload)["error"] == "settings_unavailable"
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- connection test mapping -------------------------------------------------


def test_test_maps_tester_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, _store, tester = _serve(tmp_path, monkeypatch)
    assert tester is not None
    tester.result = {
        "ok": False,
        "latency_ms": 830,
        "category": "auth_failed",
        "status": 401,
        "message": "The provider rejected the API key (HTTP 401).",
    }
    try:
        cookie = _session_cookie(url, token)
        # An ok:false result is still HTTP 200.
        status, _, payload = _request(
            url, "/api/settings/test", method="POST", body={"model": "openai/gpt-5"}, cookie=cookie
        )
        assert status == 200
        result = json.loads(payload)
        assert result["ok"] is False
        assert result["category"] == "auth_failed"
        assert result["status"] == 401
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize(("code", "http_status"), [(409, 409), (429, 429)])
def test_test_maps_tester_launch_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, http_status: int
) -> None:
    httpd, url, token, _store, tester = _serve(tmp_path, monkeypatch)
    assert tester is not None
    name = "test_in_progress" if code == 409 else "rate_limited"
    extra = {"retry_after": 60} if code == 429 else None
    tester.error = LaunchError(name, "nope", status=code, extra=extra)
    try:
        cookie = _session_cookie(url, token)
        status, headers, payload = _request(
            url, "/api/settings/test", method="POST", body={"model": "openai/gpt-5"}, cookie=cookie
        )
        assert status == http_status
        assert json.loads(payload)["error"] == name
        if code == 429:
            assert headers.get("Retry-After") == "60"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_test_key_required_for_base_change_is_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A saved key, a typed base that differs, and no typed key -> 400 and no
    # process is started (the fake tester is never called).
    httpd, url, token, _store, tester = _serve(tmp_path, monkeypatch)
    assert tester is not None
    try:
        cookie = _session_cookie(url, token)
        _request(
            url,
            "/api/settings",
            method="PUT",
            body={"model": "anthropic/claude-sonnet-4-5", "api_key": SECRET_KEY},
            cookie=cookie,
        )
        status, _, payload = _request(
            url,
            "/api/settings/test",
            method="POST",
            body={"api_base": "https://proxy.example.com"},
            cookie=cookie,
        )
        assert status == 400
        assert json.loads(payload)["error"] == "key_required_for_base_change"
        assert tester.calls == []
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_static_handler_cannot_read_config_after_second_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Save twice (two different keys); neither key is reachable through the
    # static asset handler by any ``../`` style path.
    httpd, url, token, _store, _tester = _serve(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        _request(
            url,
            "/api/settings",
            method="PUT",
            body={"model": "openai/gpt-5", "api_key": SECRET_KEY},
            cookie=cookie,
        )
        _request(
            url,
            "/api/settings",
            method="PUT",
            body={"api_key": SECRET_KEY_TWO},
            cookie=cookie,
        )
        for probe in ("/../cli-config.json", "/..%2f..%2fcli-config.json"):
            _status, _, served = _request(url, probe)
            assert SECRET_KEY.encode() not in served
            assert SECRET_KEY_TWO.encode() not in served
    finally:
        httpd.shutdown()
        httpd.server_close()
