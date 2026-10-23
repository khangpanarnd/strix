"""HTTP integration tests for the launch API (Step 6; BR-1..BR-5, BR-32, BR-33).

These run a real ``ThreadingHTTPServer`` (as the other viewer tests do) with a
fake launcher so no real scan process is spawned. The fake launcher records
calls and returns canned results, letting the tests assert the server's gate
order, error mapping, capabilities shape, artifact allow-list and the
no-regression behaviour of the existing endpoints.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from strix.interface.viewer.launch_errors import LaunchError
from strix.interface.viewer.launch_request import LaunchLimits
from strix.interface.viewer.server import serve


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class FakeLauncher:
    """Records launch/stop/status calls; returns canned results or raises LaunchError."""

    def __init__(self) -> None:
        self.limits = LaunchLimits()
        self.launches: list[Any] = []
        self.stops: list[str] = []
        self.next_error: LaunchError | None = None
        self.stop_error: LaunchError | None = None
        self.status_result = {
            "state": "running",
            "exit_code": None,
            "stoppable": True,
            "error_tail": None,
        }

    def launch(self, request: Any) -> str:
        if self.next_error is not None:
            raise self.next_error
        self.launches.append(request)
        return "web-abc123abc123"

    def stop(self, run_name: str) -> dict[str, str]:
        if self.stop_error is not None:
            raise self.stop_error
        self.stops.append(run_name)
        return {"state": "stopping"}

    def status(self, run_name: str) -> dict[str, Any]:  # noqa: ARG002
        return dict(self.status_result)


def _make_run(base: Path, name: str, *, status: str = "completed") -> Path:
    run_dir = base / "strix_runs" / name
    run_dir.mkdir(parents=True)
    record = {"run_name": name, "status": status, "end_time": "2026-01-01T00:00:00Z"}
    (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")
    return run_dir


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
    body: dict[str, object] | None = None,
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


def _serve_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, run_dir: Path | None = None
) -> tuple[Any, str, str, FakeLauncher]:
    _bundle(tmp_path, monkeypatch)
    launcher = FakeLauncher()
    runs_dir = tmp_path / "strix_runs"
    runs_dir.mkdir(exist_ok=True)
    httpd, url, token = serve(
        run_dir,
        host="127.0.0.1",
        open_browser=False,
        runs_dir=runs_dir,
        launcher=launcher,  # type: ignore[arg-type]
    )
    return httpd, url, token, launcher


_VALID_BODY = {"target": "https://example.com", "authorized": True}


# --- authorization (BR-1) ---------------------------------------------------


def test_every_new_endpoint_requires_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpd, url, _token, _launcher = _serve_launch(tmp_path, monkeypatch)
    try:
        # No cookie -> 403 for all five new endpoints.
        checks: list[tuple[str, str, dict[str, object] | None]] = [
            ("POST", "/api/scans", _VALID_BODY),
            ("POST", "/api/scans/stop", {"run": "web-x"}),
            ("GET", "/api/scans/status?run=web-x", None),
            ("GET", "/api/run/artifacts?run=web-x", None),
            ("GET", "/api/run/artifact?run=web-x&name=report.html", None),
        ]
        for method, path, body in checks:
            status, _, _ = _request(url, path, method=method, body=body)
            assert status == 403, f"{method} {path} should be 403 without the cookie"
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- gate order (section 2) -------------------------------------------------


def test_foreign_origin_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, _ = _serve_launch(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        status, _, body = _request(
            url,
            "/api/scans",
            method="POST",
            body=_VALID_BODY,
            cookie=cookie,
            headers={"Origin": "http://evil.example.com"},
        )
        assert status == 403
        assert json.loads(body)["error"] == "bad_origin"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_own_origin_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, _ = _serve_launch(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        status, _, body = _request(
            url,
            "/api/scans",
            method="POST",
            body=_VALID_BODY,
            cookie=cookie,
            headers={"Origin": url},
        )
        assert status == 202
        assert json.loads(body)["run_name"] == "web-abc123abc123"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_wrong_content_type_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, _ = _serve_launch(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        data = json.dumps(_VALID_BODY).encode()
        req = urllib.request.Request(  # noqa: S310
            url + "/api/scans",
            data=data,
            headers={"Content-Type": "text/plain", "Cookie": cookie},
            method="POST",
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
    httpd, url, token, _ = _serve_launch(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        # A chunked body carries no Content-Length -> 411.
        parsed = urlsplit(url)
        conn = http.client.HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port or 80)
        conn.putrequest("POST", "/api/scans")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Cookie", cookie)
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        conn.send(b"0\r\n\r\n")
        resp = conn.getresponse()
        status = resp.status
        payload = resp.read()
        conn.close()
        assert status == 411
        assert json.loads(payload)["error"] == "length_required"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_oversized_body_is_413(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, _ = _serve_launch(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        big = {"target": "https://example.com", "authorized": True, "instruction": "x" * 20_000}
        status, _, body = _request(url, "/api/scans", method="POST", body=big, cookie=cookie)
        assert status == 413
        assert json.loads(body)["error"] == "body_too_large"
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- success and error mapping ---------------------------------------------


def test_success_returns_202_with_run_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, launcher = _serve_launch(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        status, _, body = _request(
            url, "/api/scans", method="POST", body=_VALID_BODY, cookie=cookie
        )
        assert status == 202
        payload = json.loads(body)
        assert payload == {"run_name": "web-abc123abc123", "state": "starting"}
        assert len(launcher.launches) == 1
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_unknown_field_is_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, _ = _serve_launch(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        body = {"target": "https://example.com", "authorized": True, "bogus": 1}
        status, _, payload = _request(url, "/api/scans", method="POST", body=body, cookie=cookie)
        assert status == 400
        assert json.loads(payload)["error"] == "unknown_field"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_scan_active_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, launcher = _serve_launch(tmp_path, monkeypatch)
    launcher.next_error = LaunchError("scan_active", "busy", status=409, extra={"run": "web-live"})
    try:
        cookie = _session_cookie(url, token)
        status, _, body = _request(
            url, "/api/scans", method="POST", body=_VALID_BODY, cookie=cookie
        )
        assert status == 409
        payload = json.loads(body)
        assert payload["error"] == "scan_active"
        assert payload["run"] == "web-live"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_rate_limited_429_has_retry_after(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, launcher = _serve_launch(tmp_path, monkeypatch)
    launcher.next_error = LaunchError(
        "rate_limited", "slow down", status=429, extra={"retry_after": 60}
    )
    try:
        cookie = _session_cookie(url, token)
        status, headers, body = _request(
            url, "/api/scans", method="POST", body=_VALID_BODY, cookie=cookie
        )
        assert status == 429
        assert json.loads(body)["error"] == "rate_limited"
        assert headers.get("Retry-After") == "60"
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- stop -------------------------------------------------------------------


def test_stop_404_for_cli_run_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, launcher = _serve_launch(tmp_path, monkeypatch)
    launcher.stop_error = LaunchError("not_found", "no such live scan", status=404)
    try:
        cookie = _session_cookie(url, token)
        status, _, body = _request(
            url, "/api/scans/stop", method="POST", body={"run": "cli-run"}, cookie=cookie
        )
        assert status == 404
        assert json.loads(body)["error"] == "not_found"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_stop_success_202(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, launcher = _serve_launch(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        status, _, body = _request(
            url, "/api/scans/stop", method="POST", body={"run": "web-x"}, cookie=cookie
        )
        assert status == 202
        assert json.loads(body)["state"] == "stopping"
        assert launcher.stops == ["web-x"]
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- status -----------------------------------------------------------------


def test_status_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, token, _ = _serve_launch(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        status, _, body = _request(url, "/api/scans/status?run=web-x", cookie=cookie)
        assert status == 200
        payload = json.loads(body)
        assert set(payload) == {"state", "exit_code", "stoppable", "error_tail"}
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- artifacts (BR-32) ------------------------------------------------------


def test_artifacts_allow_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _make_run(tmp_path, "web-done")
    (run_dir / "report.html").write_text("<html></html>", encoding="utf-8")
    (run_dir / "vulnerabilities.json").write_text("[]", encoding="utf-8")
    httpd, url, token, _ = _serve_launch(tmp_path, monkeypatch, run_dir=run_dir)
    try:
        cookie = _session_cookie(url, token)
        status, _, body = _request(url, "/api/run/artifacts?run=web-done", cookie=cookie)
        assert status == 200
        available = json.loads(body)["available"]
        assert set(available) == {"report.html", "vulnerabilities.json"}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_artifact_download_streams_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _make_run(tmp_path, "web-done")
    (run_dir / "report.html").write_text("<html>hi</html>", encoding="utf-8")
    httpd, url, token, _ = _serve_launch(tmp_path, monkeypatch, run_dir=run_dir)
    try:
        cookie = _session_cookie(url, token)
        status, headers, body = _request(
            url, "/api/run/artifact?run=web-done&name=report.html", cookie=cookie
        )
        assert status == 200
        assert "attachment" in headers.get("Content-Disposition", "")
        assert body == b"<html>hi</html>"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_artifact_unknown_key_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _make_run(tmp_path, "web-done")
    httpd, url, token, _ = _serve_launch(tmp_path, monkeypatch, run_dir=run_dir)
    try:
        cookie = _session_cookie(url, token)
        status, _, body = _request(
            url, "/api/run/artifact?run=web-done&name=secret.key", cookie=cookie
        )
        assert status == 404
        assert json.loads(body)["error"] == "not_found"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_artifact_traversal_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _make_run(tmp_path, "web-done")
    httpd, url, token, _ = _serve_launch(tmp_path, monkeypatch, run_dir=run_dir)
    try:
        cookie = _session_cookie(url, token)
        # A traversal run name, an absolute path, and a traversal artifact name
        # are all refused; client text never joins a path.
        for query in (
            "run=../secret&name=report.html",
            "run=web-done&name=../../etc/passwd",
            "run=web-done&name=report.html/../../x",
        ):
            status, _, _ = _request(url, f"/api/run/artifact?{query}", cookie=cookie)
            assert status == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- capabilities -----------------------------------------------------------


def test_capabilities_in_launch_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _token, _ = _serve_launch(tmp_path, monkeypatch)
    try:
        # Capabilities are unauthenticated as today.
        status, _, body = _request(url, "/api/capabilities")
        assert status == 200
        payload = json.loads(body)
        assert payload["scan_launch_available"] is True
        assert payload["initial_run"] is False  # empty workspace
        limits = payload["scan_limits"]
        assert limits["max_budget_usd"] == 50
        assert limits["default_budget_usd"] == 5
        assert limits["modes"] == ["quick", "standard", "deep"]
        assert limits["instruction_max"] == 4000
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_capabilities_not_in_launch_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = _make_run(tmp_path, "fixed")
    _bundle(tmp_path, monkeypatch)
    # No launcher -> launch mode off.
    httpd, url, _token = serve(run_dir, host="127.0.0.1", open_browser=False)
    try:
        status, _, body = _request(url, "/api/capabilities")
        assert status == 200
        payload = json.loads(body)
        assert payload["scan_launch_available"] is False
        assert payload["initial_run"] is True  # the fixed run exists
        assert payload["scan_limits"] is None
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_launch_endpoints_403_when_not_launch_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _make_run(tmp_path, "fixed")
    _bundle(tmp_path, monkeypatch)
    httpd, url, token = serve(run_dir, host="127.0.0.1", open_browser=False)
    try:
        cookie = _session_cookie(url, token)
        # With the cookie present, the gate reaches the launch-mode check and
        # returns launch_unavailable (BR-5).
        status, _, body = _request(
            url, "/api/scans", method="POST", body=_VALID_BODY, cookie=cookie
        )
        assert status == 403
        assert json.loads(body)["error"] == "launch_unavailable"

        status, _, body = _request(url, "/api/scans/status?run=web-x", cookie=cookie)
        assert status == 403
        assert json.loads(body)["error"] == "launch_unavailable"
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- no-run behaviour and regression ---------------------------------------


def test_run_data_404_no_run_in_empty_launch_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpd, url, token, _ = _serve_launch(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(url, token)
        status, _, body = _request(url, "/api/run", cookie=cookie)
        assert status == 404
        assert json.loads(body)["error"] == "no_run"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_existing_endpoints_still_work_with_fixed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _make_run(tmp_path, "fixed")
    _bundle(tmp_path, monkeypatch)
    httpd, url, token = serve(run_dir, host="127.0.0.1", open_browser=False)
    try:
        cookie = _session_cookie(url, token)
        status, _, body = _request(url, "/api/run", cookie=cookie)
        assert status == 200
        assert json.loads(body)["run_name"] == "fixed"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_loopback_origin_alias_not_accepted_when_bound_to_all_interfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A viewer exposed on 0.0.0.0 must not treat localhost/127.0.0.1 origins as its own.
    _bundle(tmp_path, monkeypatch)
    launcher = FakeLauncher()
    runs_dir = tmp_path / "strix_runs"
    runs_dir.mkdir(exist_ok=True)
    httpd, _url, token = serve(
        None,
        host="0.0.0.0",  # nosec B104 - the point of the test
        open_browser=False,
        runs_dir=runs_dir,
        launcher=launcher,  # type: ignore[arg-type]
    )
    try:
        port = httpd.server_address[1]
        loopback = f"http://127.0.0.1:{port}"
        cookie = _session_cookie(loopback, token)
        status, _, body = _request(
            loopback,
            "/api/scans",
            method="POST",
            body=_VALID_BODY,
            cookie=cookie,
            headers={"Origin": f"http://localhost:{port}"},
        )
        assert status == 403
        assert json.loads(body)["error"] == "bad_origin"
    finally:
        httpd.shutdown()
        httpd.server_close()
