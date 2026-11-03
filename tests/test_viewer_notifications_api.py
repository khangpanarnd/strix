"""HTTP tests for the ``/api/notifications`` routes.

These run a real ``ThreadingHTTPServer`` (like ``test_llm_settings_api``) with a
real :class:`NotificationStore` on a temp path, a stub resolver (no DNS) and a
recording sender (no network). They check the gate order, the read-only
(non-loopback) behaviour, every error mapping, the happy-path round trip, the
test-send endpoint, and — across every response and the server logs — that a
webhook URL or bot token never leaks.
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
from strix.interface.viewer.login import LoginConfig, LoginManager, hash_password
from strix.interface.viewer.notification_store import NotificationStore
from strix.interface.viewer.notifications import NotificationService, ScanNotice, SendResult
from strix.interface.viewer.server import serve


if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from strix.interface.viewer.notification_store import ChannelConfig


_TEST_USER = "alice"
_TEST_PW = "pw-correct-horse"


# Recognisable fake secrets. Not real credentials.
SLACK_URL = "https://hooks.slack.com/services/T0000/B0000/SECRETSLACKTOKEN123"
BOT_TOKEN = "123456789:AAEhBP0av28SECRETTELEGRAMTOKEN12345678"  # noqa: S105
CHAT_ID = "-1001234567890"
SECRET_FRAGMENTS = ("SECRETSLACKTOKEN123", "SECRETTELEGRAMTOKEN", BOT_TOKEN)


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[ChannelConfig, ScanNotice]] = []
        self.result = SendResult(ok=True, message="Sent.", status=200)

    def send(self, channel: ChannelConfig, notice: ScanNotice) -> SendResult:
        self.sent.append((channel, notice))
        return self.result


def _ok_resolver(_host: str, _timeout: float) -> list[str]:
    return ["93.184.216.34"]


@pytest.fixture(autouse=True)
def _tmp_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        credentials_store, "CREDENTIALS_PATH", tmp_path / ".strix" / "viewer-credentials.json"
    )


def _login_manager() -> LoginManager:
    return LoginManager(LoginConfig(_TEST_USER, hash_password(_TEST_PW, iterations=1000)))


def _bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assets = tmp_path / "bundle"
    assets.mkdir(exist_ok=True)
    (assets / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    monkeypatch.setattr("strix.interface.viewer.server.bundle_dir", lambda: assets)


def _login_cookie(url: str, origin: str | None = None) -> str:
    body = json.dumps({"username": _TEST_USER, "password": _TEST_PW}).encode()
    headers = {"Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    req = urllib.request.Request(  # noqa: S310
        url + "/api/login",
        data=body,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310  # nosec B310
        assert resp.status == 200
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
) -> tuple[int, bytes]:
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
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _serve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    host: str = "127.0.0.1",
    with_notifier: bool = True,
    with_login: bool = True,
) -> tuple[Any, str, NotificationService | None, RecordingSender]:
    _bundle(tmp_path, monkeypatch)
    runs_dir = tmp_path / "strix_runs"
    runs_dir.mkdir(exist_ok=True)
    sender = RecordingSender()
    notifier = (
        NotificationService(
            NotificationStore(path=tmp_path / "notifications.json", resolver=_ok_resolver),
            sender=sender,  # type: ignore[arg-type]
        )
        if with_notifier
        else None
    )
    httpd, url, _empty = serve(
        None,
        host=host,
        open_browser=False,
        runs_dir=runs_dir,
        notifier=notifier,
        login=_login_manager() if with_login else None,
    )
    return httpd, url, notifier, sender


def _close(httpd: Any) -> None:
    httpd.shutdown()
    httpd.server_close()


# --- authorization -----------------------------------------------------------


def test_every_route_requires_the_session_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpd, url, _n, _s = _serve(tmp_path, monkeypatch)
    try:
        for method, path, body in (
            ("GET", "/api/notifications", None),
            ("PUT", "/api/notifications", {"enabled": True}),
            ("POST", "/api/notifications/test", {"channel": "slack"}),
        ):
            status, payload = _request(url, path, method=method, body=body)
            assert status == 403, f"{method} {path}"
            assert json.loads(payload)["error"] == "forbidden"
    finally:
        _close(httpd)


def test_routes_answer_unavailable_when_no_notifier_is_wired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpd, url, _n, _s = _serve(tmp_path, monkeypatch, with_notifier=False)
    try:
        cookie = _login_cookie(url)
        for method, path, body in (
            ("GET", "/api/notifications", None),
            ("PUT", "/api/notifications", {"enabled": True}),
            ("POST", "/api/notifications/test", {"channel": "slack"}),
        ):
            status, payload = _request(url, path, method=method, body=body, cookie=cookie)
            assert status == 403, f"{method} {path}"
            assert json.loads(payload)["error"] == "notifications_unavailable"
        _status, caps = _request(url, "/api/capabilities")
        assert json.loads(caps)["notifications"] == {"available": False, "editable": True}
    finally:
        _close(httpd)


def test_capabilities_advertise_notifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpd, url, _n, _s = _serve(tmp_path, monkeypatch)
    try:
        status, payload = _request(url, "/api/capabilities")
        assert status == 200
        assert json.loads(payload)["notifications"] == {"available": True, "editable": True}
    finally:
        _close(httpd)


def test_non_loopback_bind_with_login_is_editable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exposed on 0.0.0.0 but sign-in protected: an authenticated user may edit.
    httpd, _url, _n, _s = _serve(tmp_path, monkeypatch, host="0.0.0.0")  # nosec B104
    try:
        port = httpd.server_address[1]
        loopback = f"http://127.0.0.1:{port}"
        origin = f"http://0.0.0.0:{port}"
        cookie = _login_cookie(loopback, origin=origin)

        status, payload = _request(loopback, "/api/notifications", cookie=cookie)
        assert status == 200
        assert json.loads(payload)["editable"] is True

        status, _ = _request(
            loopback,
            "/api/notifications",
            method="PUT",
            body={"enabled": True},
            cookie=cookie,
            headers={"Origin": origin},
        )
        assert status == 200
    finally:
        _close(httpd)


def test_non_loopback_bind_without_login_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exposed on 0.0.0.0 with sign-in OFF: configuration stays locked.
    httpd, _url, _n, _s = _serve(tmp_path, monkeypatch, host="0.0.0.0", with_login=False)  # nosec B104
    try:
        port = httpd.server_address[1]
        loopback = f"http://127.0.0.1:{port}"

        status, payload = _request(loopback, "/api/notifications")
        assert status == 200
        assert json.loads(payload)["editable"] is False

        for method, path, body in (
            ("PUT", "/api/notifications", {"enabled": True}),
            ("POST", "/api/notifications/test", {"channel": "slack", "webhook_url": SLACK_URL}),
        ):
            status, payload = _request(loopback, path, method=method, body=body)
            assert status == 403, f"{method} {path}"
            assert json.loads(payload)["error"] == "notifications_read_only"
    finally:
        _close(httpd)


# --- gate order -----------------------------------------------------------------


def test_foreign_origin_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _n, sender = _serve(tmp_path, monkeypatch)
    try:
        cookie = _login_cookie(url)
        for method, path, body in (
            ("PUT", "/api/notifications", {"enabled": True}),
            ("POST", "/api/notifications/test", {"channel": "slack", "webhook_url": SLACK_URL}),
        ):
            status, payload = _request(
                url,
                path,
                method=method,
                body=body,
                cookie=cookie,
                headers={"Origin": "http://evil.example.com"},
            )
            assert status == 403, path
            assert json.loads(payload)["error"] == "bad_origin"
        assert sender.sent == []
    finally:
        _close(httpd)


def test_wrong_content_type_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _n, _s = _serve(tmp_path, monkeypatch)
    try:
        cookie = _login_cookie(url)
        req = urllib.request.Request(  # noqa: S310
            url + "/api/notifications",
            data=b'{"enabled": true}',
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
        _close(httpd)


def test_missing_content_length_is_411(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _n, _s = _serve(tmp_path, monkeypatch)
    try:
        cookie = _login_cookie(url)
        parsed = urlsplit(url)
        conn = http.client.HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port or 80)
        conn.putrequest("PUT", "/api/notifications")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Cookie", cookie)
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        response = conn.getresponse()
        assert response.status == 411
        conn.close()
    finally:
        _close(httpd)


# --- round trip --------------------------------------------------------------------


def test_get_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _n, _s = _serve(tmp_path, monkeypatch)
    try:
        status, payload = _request(url, "/api/notifications", cookie=_login_cookie(url))
        view = json.loads(payload)
        assert status == 200
        assert view["enabled"] is False
        assert view["editable"] is True
        assert view["config_path"] == str(tmp_path / "notifications.json")
        assert set(view["channels"]) == {"slack", "teams", "telegram"}
    finally:
        _close(httpd)


def test_save_then_get_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, notifier, _s = _serve(tmp_path, monkeypatch)
    try:
        cookie = _login_cookie(url)
        status, payload = _request(
            url,
            "/api/notifications",
            method="PUT",
            cookie=cookie,
            body={
                "enabled": True,
                "events": {"progress": True},
                "progress_interval_minutes": 30,
                "channels": {
                    "slack": {"enabled": True, "webhook_url": SLACK_URL},
                    "telegram": {"enabled": True, "bot_token": BOT_TOKEN, "chat_id": CHAT_ID},
                },
            },
        )
        assert status == 200, payload
        saved = json.loads(payload)
        assert saved["editable"] is True

        status, payload = _request(url, "/api/notifications", cookie=cookie)
        view = json.loads(payload)
        assert view["enabled"] is True
        assert view["events"]["progress"] is True
        assert view["progress_interval_minutes"] == 30
        assert view["channels"]["slack"] == {
            "enabled": True,
            "configured": True,
            "host": "hooks.slack.com",
        }
        assert view["channels"]["telegram"]["token_set"] is True
        assert view["channels"]["teams"]["configured"] is False

        # The service sees the new configuration with no restart.
        assert notifier is not None
        assert [c.name for c in notifier.store.load().active_channels()] == ["slack", "telegram"]
    finally:
        _close(httpd)


def test_secrets_never_appear_in_any_response_or_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    httpd, url, _n, _s = _serve(tmp_path, monkeypatch)
    bodies: list[bytes] = []
    try:
        cookie = _login_cookie(url)
        with caplog.at_level(logging.DEBUG):
            for method, path, body in (
                (
                    "PUT",
                    "/api/notifications",
                    {
                        "enabled": True,
                        "channels": {
                            "slack": {"enabled": True, "webhook_url": SLACK_URL},
                            "telegram": {"bot_token": BOT_TOKEN, "chat_id": CHAT_ID},
                        },
                    },
                ),
                ("GET", "/api/notifications", None),
                ("POST", "/api/notifications/test", {"channel": "slack"}),
                ("POST", "/api/notifications/test", {"channel": "telegram"}),
                # error responses must not echo what was typed
                (
                    "PUT",
                    "/api/notifications",
                    {"channels": {"slack": {"webhook_url": "http://" + SLACK_URL[8:]}}},
                ),
                (
                    "PUT",
                    "/api/notifications",
                    {"channels": {"telegram": {"bot_token": BOT_TOKEN + "!"}}},
                ),
                ("PUT", "/api/notifications", {"unknown": SLACK_URL}),
                ("GET", "/api/capabilities", None),
            ):
                _status, payload = _request(url, path, method=method, body=body, cookie=cookie)
                bodies.append(payload)
        everything = b"\n".join(bodies).decode() + "\n".join(r.getMessage() for r in caplog.records)
        for fragment in SECRET_FRAGMENTS:
            assert fragment not in everything, fragment
        # The static handler cannot be coaxed into serving the config file either.
        _status, static = _request(url, "/notifications.json", cookie=cookie)
        assert b"SECRETSLACKTOKEN123" not in static
    finally:
        _close(httpd)


# --- validation errors ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            {"channels": {"slack": {"webhook_url": "http://hooks.slack.com/x"}}},
            "invalid_webhook_url",
        ),
        ({"channels": {"teams": {"webhook_url": "https://localhost/x"}}}, "invalid_webhook_url"),
        ({"channels": {"telegram": {"bot_token": "nope"}}}, "invalid_bot_token"),
        ({"channels": {"telegram": {"chat_id": "nope nope"}}}, "invalid_chat_id"),
        ({"progress_interval_minutes": 1}, "invalid_interval"),
        ({"channels": {"slack": {"enabled": True}}}, "channel_incomplete"),
        ({"events": {"exploded": True}}, "invalid_json"),
        ({"surprise": 1}, "unknown_field"),
    ],
)
def test_validation_errors_map_to_400_with_a_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any], code: str
) -> None:
    httpd, url, _n, _s = _serve(tmp_path, monkeypatch)
    try:
        status, payload = _request(
            url, "/api/notifications", method="PUT", body=body, cookie=_login_cookie(url)
        )
        assert status == 400
        assert json.loads(payload)["error"] == code
        assert not (tmp_path / "notifications.json").exists()
    finally:
        _close(httpd)


def test_non_object_body_is_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _n, _s = _serve(tmp_path, monkeypatch)
    try:
        cookie = _login_cookie(url)
        req = urllib.request.Request(  # noqa: S310
            url + "/api/notifications",
            data=b"[1, 2, 3]",
            headers={"Content-Type": "application/json", "Cookie": cookie},
            method="PUT",
        )
        try:
            urllib.request.urlopen(req)  # noqa: S310  # nosec B310
            status = 200
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, exc.read()
        assert status == 400
        assert json.loads(payload)["error"] == "invalid_json"
    finally:
        _close(httpd)


def test_unreadable_config_file_is_409_and_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpd, url, _n, _s = _serve(tmp_path, monkeypatch)
    try:
        (tmp_path / "notifications.json").write_text("{nope", encoding="utf-8")
        status, payload = _request(
            url,
            "/api/notifications",
            method="PUT",
            body={"enabled": True},
            cookie=_login_cookie(url),
        )
        assert status == 409
        assert json.loads(payload)["error"] == "config_unreadable"
        assert (tmp_path / "notifications.json").read_text(encoding="utf-8") == "{nope"
    finally:
        _close(httpd)


# --- test-send endpoint ---------------------------------------------------------------


def test_test_send_uses_the_saved_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _n, sender = _serve(tmp_path, monkeypatch)
    try:
        cookie = _login_cookie(url)
        _request(
            url,
            "/api/notifications",
            method="PUT",
            cookie=cookie,
            body={"channels": {"slack": {"webhook_url": SLACK_URL}}},
        )

        status, payload = _request(
            url, "/api/notifications/test", method="POST", cookie=cookie, body={"channel": "slack"}
        )

        assert status == 200
        assert json.loads(payload) == {"ok": True, "message": "Sent.", "status": 200}
        ((channel, notice),) = sender.sent
        assert channel.webhook_url == SLACK_URL
        assert notice.event == "test"
    finally:
        _close(httpd)


def test_test_send_can_use_typed_values_without_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpd, url, _n, sender = _serve(tmp_path, monkeypatch)
    try:
        status, _payload = _request(
            url,
            "/api/notifications/test",
            method="POST",
            cookie=_login_cookie(url),
            body={"channel": "telegram", "bot_token": BOT_TOKEN, "chat_id": CHAT_ID},
        )
        assert status == 200
        assert sender.sent[0][0].chat_id == CHAT_ID
        assert not (tmp_path / "notifications.json").exists()
    finally:
        _close(httpd)


def test_a_failed_delivery_is_http_200_with_ok_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpd, url, _n, sender = _serve(tmp_path, monkeypatch)
    try:
        sender.result = SendResult(
            ok=False, message="Slack rejected the message (HTTP 404).", status=404
        )
        status, payload = _request(
            url,
            "/api/notifications/test",
            method="POST",
            cookie=_login_cookie(url),
            body={"channel": "slack", "webhook_url": SLACK_URL},
        )
        assert status == 200
        result = json.loads(payload)
        assert result["ok"] is False
        assert result["status"] == 404
        assert "HTTP 404" in result["message"]
    finally:
        _close(httpd)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({}, "invalid_channel"),
        ({"channel": 5}, "invalid_channel"),
        ({"channel": ["slack"]}, "invalid_channel"),
        ({"channel": "discord"}, "invalid_channel"),
        ({"channel": "slack"}, "channel_incomplete"),
        ({"channel": "telegram", "bot_token": BOT_TOKEN}, "channel_incomplete"),
        ({"channel": "slack", "webhook_url": "http://insecure.example"}, "invalid_webhook_url"),
        ({"channel": "slack", "bot_token": BOT_TOKEN}, "invalid_json"),
    ],
)
def test_test_send_rejects_bad_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any], code: str
) -> None:
    httpd, url, _n, sender = _serve(tmp_path, monkeypatch)
    try:
        status, payload = _request(
            url,
            "/api/notifications/test",
            method="POST",
            cookie=_login_cookie(url),
            body=body,
        )
        assert status == 400
        assert json.loads(payload)["error"] == code
        assert sender.sent == []
    finally:
        _close(httpd)


def test_test_send_is_rate_limited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, url, _n, sender = _serve(tmp_path, monkeypatch)
    try:
        cookie = _login_cookie(url)
        body = {"channel": "slack", "webhook_url": SLACK_URL}
        statuses = [
            _request(url, "/api/notifications/test", method="POST", cookie=cookie, body=body)[0]
            for _ in range(7)
        ]
        assert statuses == [200] * 6 + [429]
        assert len(sender.sent) == 6
    finally:
        _close(httpd)


def test_changes_share_the_settings_change_rate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpd, url, _n, _s = _serve(tmp_path, monkeypatch)
    try:
        cookie = _login_cookie(url)
        # Requests rejected as invalid still count against the window (it is
        # charged before the body is read), and they never touch the disk.
        statuses = [
            _request(url, "/api/notifications", method="PUT", cookie=cookie, body={"surprise": i})[
                0
            ]
            for i in range(32)
        ]
        assert statuses[:30] == [400] * 30
        assert statuses[30:] == [429, 429]
        assert not (tmp_path / "notifications.json").exists()
    finally:
        _close(httpd)
