"""Tests for scan notifications: store, rendering, sender and the watching service.

Everything is injected: a fake opener (no network), a stub resolver (no DNS), a
fake launcher and a manual clock. The service is driven with ``poll_once`` and a
manual drain of its queue so no test depends on thread timing, except the one
that checks the real shutdown flush.
"""

from __future__ import annotations

import io
import json
import logging
import queue
import sys
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import pytest

from strix.interface.viewer.launch_errors import LaunchError
from strix.interface.viewer.launch_request import LaunchLimits, LaunchRequest
from strix.interface.viewer.notification_store import (
    INTERVAL_MAX,
    INTERVAL_MIN,
    ChannelConfig,
    HostBlockedError,
    NotificationStore,
    check_public_host,
)
from strix.interface.viewer.notifications import (
    NotificationSender,
    NotificationService,
    ScanNotice,
    SendResult,
    _NoRedirect,
    format_elapsed,
    format_findings,
    render_slack,
    render_teams,
    render_telegram,
)
from strix.interface.viewer.scan_launcher import ScanLauncher


# Recognisable fake secrets: they must never appear in a view, a result message,
# or a log line. Not real credentials.
SLACK_URL = "https://hooks.slack.com/services/T0000/B0000/SECRETSLACKTOKEN123"
TEAMS_URL = "https://example.webhook.office.com/webhookb2/SECRETTEAMSTOKEN456"
BOT_TOKEN = "123456789:AAEhBP0av28SECRETTELEGRAMTOKEN12345678"  # noqa: S105
CHAT_ID = "-1001234567890"
SECRETS = (SLACK_URL, TEAMS_URL, BOT_TOKEN, "SECRETSLACKTOKEN123", "SECRETTEAMSTOKEN456")


def _public_resolver(_host: str, _timeout: float) -> list[str]:
    return ["93.184.216.34"]


def _store(tmp_path: Path, resolver: Any = _public_resolver) -> NotificationStore:
    return NotificationStore(path=tmp_path / "notifications.json", resolver=resolver)


# =============================================================================
# store
# =============================================================================


def test_defaults_are_disabled_and_empty(tmp_path: Path) -> None:
    view = _store(tmp_path).view(editable=True)

    assert view["enabled"] is False
    assert view["file_status"] == "missing"
    assert view["events"] == {
        "started": True,
        "progress": False,
        "finished": True,
        "stopped": True,
        "failed": True,
    }
    assert view["progress_interval_minutes"] == 15
    for name in ("slack", "teams", "telegram"):
        assert view["channels"][name]["enabled"] is False
        assert view["channels"][name]["configured"] is False


def test_apply_saves_and_view_never_returns_secrets(tmp_path: Path) -> None:
    store = _store(tmp_path)
    view = store.apply(
        {
            "enabled": True,
            "channels": {
                "slack": {"enabled": True, "webhook_url": SLACK_URL},
                "teams": {"enabled": True, "webhook_url": TEAMS_URL},
                "telegram": {"enabled": True, "bot_token": BOT_TOKEN, "chat_id": CHAT_ID},
            },
        }
    )

    assert view["enabled"] is True
    assert view["channels"]["slack"] == {
        "enabled": True,
        "configured": True,
        "host": "hooks.slack.com",
    }
    assert view["channels"]["telegram"]["token_set"] is True
    assert view["channels"]["telegram"]["chat_id"] == CHAT_ID
    dumped = json.dumps(store.view(editable=True))
    assert not any(secret in dumped for secret in SECRETS)


def test_secrets_are_persisted_and_loadable_for_the_sender(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply({"channels": {"slack": {"enabled": True, "webhook_url": SLACK_URL}}})

    config = _store(tmp_path).load()  # a fresh store: proves it round-trips via the file

    (channel,) = config.active_channels()
    assert channel.name == "slack"
    assert channel.webhook_url == SLACK_URL


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_config_file_is_owner_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply({"channels": {"slack": {"webhook_url": SLACK_URL}}})
    assert (store.path.stat().st_mode & 0o777) == 0o600


def test_partial_update_keeps_everything_not_mentioned(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply(
        {
            "enabled": True,
            "progress_interval_minutes": 30,
            "channels": {"slack": {"enabled": True, "webhook_url": SLACK_URL}},
        }
    )

    view = store.apply({"events": {"progress": True}})

    assert view["enabled"] is True
    assert view["progress_interval_minutes"] == 30
    assert view["events"]["progress"] is True
    assert view["events"]["finished"] is True
    assert view["channels"]["slack"]["configured"] is True
    assert store.load().active_channels()[0].webhook_url == SLACK_URL


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.slack.com/services/x",  # not https
        "https://localhost/hook",
        "https://127.0.0.1/hook",
        "https://169.254.169.254/latest",  # cloud metadata
        "https://user:pw@hooks.slack.com/x",  # embedded credentials
        "ftp://hooks.slack.com/x",
        "not a url",
        "",
        "https://hooks.slack.com/" + "a" * 2100,
    ],
)
def test_webhook_url_must_be_public_https(tmp_path: Path, url: str) -> None:
    with pytest.raises(LaunchError) as exc:
        _store(tmp_path).apply({"channels": {"slack": {"webhook_url": url}}})
    assert exc.value.code == "invalid_webhook_url"
    assert exc.value.status == 400
    assert url not in exc.value.message or url == ""


def test_webhook_resolving_to_a_private_address_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path, resolver=lambda _h, _t: ["10.0.0.5"])
    with pytest.raises(LaunchError) as exc:
        store.apply({"channels": {"teams": {"webhook_url": TEAMS_URL}}})
    assert exc.value.code == "invalid_webhook_url"
    assert "SECRETTEAMSTOKEN456" not in exc.value.message


def test_save_succeeds_when_dns_is_down(tmp_path: Path) -> None:
    def broken(_host: str, _timeout: float) -> list[str]:
        raise OSError("no network")

    view = _store(tmp_path, resolver=broken).apply(
        {"channels": {"slack": {"webhook_url": SLACK_URL}}}
    )
    assert view["channels"]["slack"]["configured"] is True


@pytest.mark.parametrize("token", ["", "abc", "123:short", "12345678:" + "!" * 35, 12345, None])
def test_bot_token_validation(tmp_path: Path, token: object) -> None:
    with pytest.raises(LaunchError) as exc:
        _store(tmp_path).apply({"channels": {"telegram": {"bot_token": token}}})
    assert exc.value.code == "invalid_bot_token"


@pytest.mark.parametrize("chat", ["", "abc", "12 34", "@ab", "-", "1" * 30, 5, None])
def test_chat_id_validation(tmp_path: Path, chat: object) -> None:
    with pytest.raises(LaunchError) as exc:
        _store(tmp_path).apply({"channels": {"telegram": {"chat_id": chat}}})
    assert exc.value.code == "invalid_chat_id"


@pytest.mark.parametrize("chat", ["123456", "-1001234567890", "@my_channel"])
def test_chat_id_accepts_numeric_and_channel_names(tmp_path: Path, chat: str) -> None:
    view = _store(tmp_path).apply({"channels": {"telegram": {"chat_id": chat}}})
    assert view["channels"]["telegram"]["chat_id"] == chat


@pytest.mark.parametrize(
    "value", [INTERVAL_MIN - 1, INTERVAL_MAX + 1, 0, -5, True, "15", 15.5, None]
)
def test_interval_bounds(tmp_path: Path, value: object) -> None:
    with pytest.raises(LaunchError) as exc:
        _store(tmp_path).apply({"progress_interval_minutes": value})
    assert exc.value.code == "invalid_interval"


@pytest.mark.parametrize("value", [INTERVAL_MIN, 15, INTERVAL_MAX])
def test_interval_accepts_bounds(tmp_path: Path, value: int) -> None:
    view = _store(tmp_path).apply({"progress_interval_minutes": value})
    assert view["progress_interval_minutes"] == value


def test_enabling_a_channel_without_credentials_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for body in (
        {"channels": {"slack": {"enabled": True}}},
        {"channels": {"teams": {"enabled": True}}},
        {"channels": {"telegram": {"enabled": True, "bot_token": BOT_TOKEN}}},  # no chat id
    ):
        with pytest.raises(LaunchError) as exc:
            store.apply(body)
        assert exc.value.code == "channel_incomplete"
    assert not store.path.exists()  # nothing was written


def test_credentials_and_enable_in_one_request_is_fine(tmp_path: Path) -> None:
    view = _store(tmp_path).apply(
        {"channels": {"telegram": {"enabled": True, "bot_token": BOT_TOKEN, "chat_id": CHAT_ID}}}
    )
    assert view["channels"]["telegram"]["enabled"] is True


def test_clear_removes_a_secret(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply({"channels": {"slack": {"webhook_url": SLACK_URL}}})

    view = store.apply({"clear": ["slack.webhook_url"]})

    assert view["channels"]["slack"]["configured"] is False
    assert view["channels"]["slack"]["host"] is None
    assert SLACK_URL not in store.path.read_text(encoding="utf-8")


def test_clearing_the_secret_of_an_enabled_channel_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply({"channels": {"slack": {"enabled": True, "webhook_url": SLACK_URL}}})
    with pytest.raises(LaunchError) as exc:
        store.apply({"clear": ["slack.webhook_url"]})
    assert exc.value.code == "channel_incomplete"
    assert store.load().active_channels()[0].webhook_url == SLACK_URL  # unchanged


def test_disable_and_clear_together_works(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply({"channels": {"slack": {"enabled": True, "webhook_url": SLACK_URL}}})
    view = store.apply({"channels": {"slack": {"enabled": False}}, "clear": ["slack.webhook_url"]})
    assert view["channels"]["slack"] == {"enabled": False, "configured": False, "host": None}


@pytest.mark.parametrize(
    "body",
    [
        {"bogus": 1},
        {"events": {"exploded": True}},
        {"events": "all"},
        {"events": {"started": "yes"}},
        {"enabled": "true"},
        {"channels": {"discord": {}}},
        {"channels": {"slack": {"webhook_url": SLACK_URL, "extra": 1}}},
        {"channels": {"slack": "x"}},
        {"clear": "slack.webhook_url"},
        {"clear": ["slack.enabled"]},
        {"clear": [1]},
    ],
)
def test_malformed_bodies_are_rejected(tmp_path: Path, body: dict[str, Any]) -> None:
    store = _store(tmp_path)
    with pytest.raises(LaunchError) as exc:
        store.apply(body)
    assert exc.value.status == 400
    assert not store.path.exists()


def test_set_and_clear_of_the_same_field_conflicts(tmp_path: Path) -> None:
    with pytest.raises(LaunchError) as exc:
        _store(tmp_path).apply(
            {"channels": {"slack": {"webhook_url": SLACK_URL}}, "clear": ["slack.webhook_url"]}
        )
    assert exc.value.code == "invalid_json"


def test_non_object_body_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(LaunchError) as exc:
        _store(tmp_path).apply([1, 2])  # type: ignore[arg-type]
    assert exc.value.code == "invalid_json"


def test_unreadable_file_is_never_overwritten(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(LaunchError) as exc:
        store.apply({"enabled": True})

    assert exc.value.code == "config_unreadable"
    assert exc.value.status == 409
    assert store.path.read_text(encoding="utf-8") == "{ this is not json"
    # Reads degrade to "off" rather than crashing the viewer or the service.
    assert store.view(editable=True)["file_status"] == "unreadable"
    assert store.load().enabled is False


def test_wrong_typed_values_in_the_file_fall_back_to_defaults(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.write_text(
        json.dumps(
            {
                "enabled": "yes",
                "events": {"started": "no", "finished": False},
                "progress_interval_minutes": 9999,
                "channels": {"slack": {"enabled": True, "webhook_url": 5}, "teams": "x"},
            }
        ),
        encoding="utf-8",
    )
    config = store.load()
    assert config.enabled is False
    assert "started" in config.events  # bad value ignored -> default True
    assert "finished" not in config.events  # good value respected
    assert config.progress_interval_minutes == 15
    assert config.active_channels() == []


def test_channel_config_repr_does_not_leak_secrets() -> None:
    channel = ChannelConfig(name="telegram", enabled=True, bot_token=BOT_TOKEN, chat_id=CHAT_ID)
    text = repr(channel) + str(channel)
    assert BOT_TOKEN not in text
    assert CHAT_ID not in text


def test_save_audit_line_has_no_secrets(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="strix.interface.viewer.notification_store"):
        _store(tmp_path).apply(
            {
                "enabled": True,
                "channels": {
                    "slack": {"enabled": True, "webhook_url": SLACK_URL},
                    "telegram": {"bot_token": BOT_TOKEN, "chat_id": CHAT_ID},
                },
            }
        )
    audit = "\n".join(r.getMessage() for r in caplog.records)
    assert "viewer notifications saved" in audit
    assert "slack" in audit
    assert not any(secret in audit for secret in SECRETS)
    assert CHAT_ID not in audit


# --- test-send channel resolution --------------------------------------------


def test_resolve_test_channel_overlays_typed_values_on_saved(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply({"channels": {"telegram": {"bot_token": BOT_TOKEN, "chat_id": CHAT_ID}}})

    resolved = store.resolve_test_channel(
        "telegram", {"channel": "telegram", "chat_id": "@other_chan"}
    )

    assert resolved.bot_token == BOT_TOKEN  # saved
    assert resolved.chat_id == "@other_chan"  # typed
    assert store.load().channels[2].chat_id == CHAT_ID  # nothing was saved


def test_resolve_test_channel_works_without_saving_anything(tmp_path: Path) -> None:
    store = _store(tmp_path)
    resolved = store.resolve_test_channel("slack", {"channel": "slack", "webhook_url": SLACK_URL})
    assert resolved.webhook_url == SLACK_URL
    assert not store.path.exists()


def test_resolve_test_channel_requires_a_complete_channel(tmp_path: Path) -> None:
    with pytest.raises(LaunchError) as exc:
        _store(tmp_path).resolve_test_channel("slack", {"channel": "slack"})
    assert exc.value.code == "channel_incomplete"


def test_resolve_test_channel_rejects_unknown_channel_and_bad_values(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(LaunchError) as exc:
        store.resolve_test_channel("discord", {"channel": "discord"})
    assert exc.value.code == "invalid_channel"
    with pytest.raises(LaunchError) as exc2:
        store.resolve_test_channel("slack", {"channel": "slack", "webhook_url": "http://x.example"})
    assert exc2.value.code == "invalid_webhook_url"


# --- host safety ---------------------------------------------------------------


def test_check_public_host_blocks_private_metadata_and_local_names() -> None:
    for host in ("localhost", "127.0.0.1", "10.1.2.3", "192.168.0.9", "169.254.169.254", "::1", ""):
        with pytest.raises(HostBlockedError):
            check_public_host(host, _public_resolver, require_resolution=True)


def test_check_public_host_resolution_modes() -> None:
    def broken(_h: str, _t: float) -> list[str]:
        raise OSError("down")

    check_public_host("hooks.slack.com", broken, require_resolution=False)  # tolerated on save
    with pytest.raises(HostBlockedError) as exc:
        check_public_host("hooks.slack.com", broken, require_resolution=True)  # reported on send
    assert exc.value.reason == "unresolvable"
    with pytest.raises(HostBlockedError):
        check_public_host(
            "evil.example", lambda _h, _t: ["93.184.216.34", "10.0.0.1"], require_resolution=True
        )


# =============================================================================
# rendering
# =============================================================================


def _notice(**kw: Any) -> ScanNotice:
    base: dict[str, Any] = {
        "event": "finished",
        "run_name": "web-abc123",
        "target": "https://staging.example.com",
        "mode": "standard",
        "elapsed_seconds": 3725,
        "findings": {"critical": 1, "high": 2, "medium": 0, "low": 3},
        "cost_usd": 1.5,
    }
    base.update(kw)
    return ScanNotice(**base)


def test_format_helpers() -> None:
    assert format_elapsed(None) == ""
    assert format_elapsed(-1) == ""
    assert format_elapsed(7) == "7s"
    assert format_elapsed(125) == "2m 5s"
    assert format_elapsed(3725) == "1h 2m"
    assert format_findings(None) == ""
    assert format_findings({"critical": 0, "high": 0, "medium": 0, "low": 0}) == "none"
    assert format_findings({"critical": 1, "high": 2, "medium": 0, "low": 3}) == (
        "6 (1 critical, 2 high, 3 low)"
    )


def test_slack_payload() -> None:
    payload = render_slack(_notice())
    assert set(payload) == {"text"}
    text = payload["text"]
    assert text.startswith("*✅ Scan finished*")
    assert "*Target:* https://staging.example.com" in text
    assert "*Findings:* 6 (1 critical, 2 high, 3 low)" in text
    assert "*Cost:* $1.50" in text
    assert "*Elapsed:* 1h 2m" in text


def test_slack_escapes_markup_in_values() -> None:
    text = render_slack(_notice(target="<!channel> & <script>"))["text"]
    assert "<!channel>" not in text
    assert "&lt;!channel&gt; &amp; &lt;script&gt;" in text


def test_teams_payload_is_an_adaptive_card() -> None:
    payload = render_teams(_notice(event="failed", detail="exit code 3"))
    assert payload["type"] == "message"
    (attachment,) = payload["attachments"]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard"
    title, factset = card["body"]
    assert title["text"] == "❌ Scan failed"
    facts = {f["title"]: f["value"] for f in factset["facts"]}
    assert facts["Target:"] == "https://staging.example.com"
    assert facts["Detail:"] == "exit code 3"
    json.dumps(payload)  # serialisable


def test_telegram_payload_is_plain_text_for_the_chat() -> None:
    payload = render_telegram(_notice(event="stopped"), CHAT_ID)
    assert payload["chat_id"] == CHAT_ID
    assert payload["disable_web_page_preview"] is True
    assert "parse_mode" not in payload  # plain text: nothing to escape
    assert payload["text"].startswith("⏹️ Scan stopped")
    assert "Target: https://staging.example.com" in payload["text"]


def test_long_and_hostile_values_are_cleaned_and_capped() -> None:
    notice = _notice(target="a\x00b\r\nc\x1b[31m" + "x" * 5000, mode="m" * 5000)
    text = render_telegram(notice, CHAT_ID)["text"]
    assert "\x00" not in text
    assert "\x1b" not in text
    assert "\r" not in text
    assert len(text) <= 4000
    for line in text.splitlines():
        assert len(line) <= 320


def test_optional_facts_are_omitted() -> None:
    text = render_slack(ScanNotice(event="started", run_name="r1", target="t", mode="quick"))[
        "text"
    ]
    assert "Findings" not in text
    assert "Cost" not in text
    assert "Elapsed" not in text
    assert "Detail" not in text


def test_zero_cost_is_not_shown() -> None:
    assert "Cost" not in render_slack(_notice(cost_usd=0.0))["text"]
    assert "Cost" not in render_slack(_notice(cost_usd=None))["text"]


# =============================================================================
# sender
# =============================================================================


class FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class FakeOpener:
    def __init__(self, *, status: int = 200, error: BaseException | None = None) -> None:
        self.status = status
        self.error = error
        self.calls: list[tuple[Any, float]] = []

    def open(self, request: Any, timeout: float) -> FakeResponse:
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.status)


def _slack() -> ChannelConfig:
    return ChannelConfig(name="slack", enabled=True, webhook_url=SLACK_URL)


def _teams() -> ChannelConfig:
    return ChannelConfig(name="teams", enabled=True, webhook_url=TEAMS_URL)


def _telegram() -> ChannelConfig:
    return ChannelConfig(name="telegram", enabled=True, bot_token=BOT_TOKEN, chat_id=CHAT_ID)


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x.example", code, "msg", {}, io.BytesIO(body))  # type: ignore[arg-type]


def _sender(opener: FakeOpener, resolver: Any = _public_resolver) -> NotificationSender:
    return NotificationSender(opener=opener, resolver=resolver, timeout=7.0)


def test_slack_post_goes_to_the_webhook_with_json() -> None:
    opener = FakeOpener()
    result = _sender(opener).send(_slack(), _notice())

    assert result.ok is True
    assert result.status == 200
    ((request, timeout),) = opener.calls
    assert request.full_url == SLACK_URL
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data)["text"].startswith("*✅ Scan finished*")
    assert timeout == 7.0


def test_teams_accepts_202() -> None:
    opener = FakeOpener(status=202)
    result = _sender(opener).send(_teams(), _notice())
    assert result.ok is True
    assert json.loads(opener.calls[0][0].data)["type"] == "message"


def test_telegram_posts_to_the_bot_api_with_the_chat() -> None:
    opener = FakeOpener()
    result = _sender(opener).send(_telegram(), _notice())

    assert result.ok is True
    request = opener.calls[0][0]
    assert request.full_url == f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    body = json.loads(request.data)
    assert body["chat_id"] == CHAT_ID
    assert "Scan finished" in body["text"]


@pytest.mark.parametrize("code", [400, 401, 403, 404, 429, 500, 302])
def test_http_errors_become_safe_messages(code: int) -> None:
    for channel in (_slack(), _teams(), _telegram()):
        result = _sender(FakeOpener(error=_http_error(code))).send(channel, _notice())
        assert result.ok is False
        assert result.status == code
        assert f"HTTP {code}" in result.message
        assert not any(secret in result.message for secret in SECRETS)
        assert CHAT_ID not in result.message


def test_telegram_error_description_is_shown_but_the_token_is_scrubbed() -> None:
    body = json.dumps({"ok": False, "description": f"Bad Request: chat not found {BOT_TOKEN}"})
    result = _sender(FakeOpener(error=_http_error(400, body.encode()))).send(_telegram(), _notice())
    assert "chat not found" in result.message
    assert "[redacted]" in result.message
    assert BOT_TOKEN not in result.message
    assert "SECRETTELEGRAMTOKEN" not in result.message


def test_unparseable_telegram_error_body_is_tolerated() -> None:
    result = _sender(FakeOpener(error=_http_error(502, b"<html>bad gateway</html>"))).send(
        _telegram(), _notice()
    )
    assert result.ok is False
    assert "HTTP 502" in result.message


def test_timeouts_and_network_errors_are_reported_without_details() -> None:
    timed_out = _sender(FakeOpener(error=TimeoutError("took too long"))).send(_slack(), _notice())
    assert timed_out.ok is False
    assert "did not respond" in timed_out.message

    unreachable = _sender(
        FakeOpener(error=urllib.error.URLError(f"failed to reach {SLACK_URL}"))
    ).send(_slack(), _notice())
    assert unreachable.ok is False
    assert "Could not reach" in unreachable.message
    assert "SECRETSLACKTOKEN123" not in unreachable.message

    refused = _sender(FakeOpener(error=ConnectionRefusedError("refused"))).send(_slack(), _notice())
    assert refused.ok is False


def test_a_non_2xx_status_without_an_exception_is_a_failure() -> None:
    result = _sender(FakeOpener(status=504)).send(_slack(), _notice())
    assert result.ok is False
    assert result.status == 504


def test_private_resolution_means_the_webhook_is_not_contacted() -> None:
    opener = FakeOpener()
    result = _sender(opener, resolver=lambda _h, _t: ["192.168.1.10"]).send(_slack(), _notice())

    assert result.ok is False
    assert "private or blocked" in result.message
    assert opener.calls == []
    assert "SECRETSLACKTOKEN123" not in result.message


def test_unresolvable_webhook_is_reported_and_not_contacted() -> None:
    def broken(_h: str, _t: float) -> list[str]:
        raise OSError("nxdomain")

    opener = FakeOpener()
    result = _sender(opener, resolver=broken).send(_slack(), _notice())

    assert result.ok is False
    assert "could not be resolved" in result.message
    assert opener.calls == []


def test_dns_rebinding_is_caught_at_send_time() -> None:
    # The webhook passed validation when saved (public), then its name started
    # resolving to the metadata service. The send-time check must refuse it.
    opener = FakeOpener()
    result = _sender(opener, resolver=lambda _h, _t: ["169.254.169.254"]).send(_teams(), _notice())
    assert result.ok is False
    assert opener.calls == []


def test_redirects_are_never_followed() -> None:
    handler = _NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/") is None  # type: ignore[arg-type]


# =============================================================================
# service
# =============================================================================


@dataclass
class Snap:
    run_name: str
    target: str
    mode: str
    state: str
    exit_code: int | None = None


class FakeLauncher:
    def __init__(self) -> None:
        self.snaps: list[Snap] = []

    def snapshot(self) -> list[Snap]:
        return list(self.snaps)


class RecordingSender:
    """Records (channel name, event); ``fail`` names channels that return a failure."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.sent: list[tuple[str, ScanNotice]] = []
        self.fail = fail or set()

    def send(self, channel: ChannelConfig, notice: ScanNotice) -> SendResult:
        self.sent.append((channel.name, notice))
        if channel.name in self.fail:
            return SendResult(ok=False, message="boom")
        return SendResult(ok=True, message="ok", status=200)

    @property
    def events(self) -> list[tuple[str, str]]:
        return [(name, n.event) for name, n in self.sent]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _drain(service: NotificationService) -> None:
    while True:
        try:
            item = service._queue.get_nowait()
        except queue.Empty:
            return
        service._deliver(item)


def _setup(
    tmp_path: Path,
    *,
    config: dict[str, Any] | None = None,
    sender: RecordingSender | None = None,
) -> tuple[NotificationService, FakeLauncher, RecordingSender, Clock, NotificationStore]:
    store = _store(tmp_path)
    store.apply(
        config
        if config is not None
        else {
            "enabled": True,
            "channels": {"slack": {"enabled": True, "webhook_url": SLACK_URL}},
        }
    )
    launcher = FakeLauncher()
    recorder = sender or RecordingSender()
    clock = Clock()
    runs = tmp_path / "strix_runs"
    runs.mkdir(exist_ok=True)
    service = NotificationService(
        store,
        launcher=launcher,
        runs_dir=runs,
        sender=recorder,  # type: ignore[arg-type]
        clock=clock,
        poll_seconds=60.0,
    )
    return service, launcher, recorder, clock, store


def test_start_then_finish_sends_two_messages_once_each(tmp_path: Path) -> None:
    service, launcher, sender, _clock, _cfg = _setup(tmp_path)
    launcher.snaps = [Snap("web-1", "https://t.example", "quick", "starting")]

    service.poll_once()
    service.poll_once()  # nothing new
    _drain(service)
    assert sender.events == [("slack", "started")]

    launcher.snaps[0].state = "running"
    service.poll_once()  # still running: no duplicate "started"
    launcher.snaps[0].state = "finished"
    launcher.snaps[0].exit_code = 0
    service.poll_once()
    service.poll_once()  # terminal already announced
    _drain(service)

    assert sender.events == [("slack", "started"), ("slack", "finished")]


@pytest.mark.parametrize(
    ("state", "exit_code", "event", "detail"),
    [
        ("finished", 2, "finished", ""),
        ("stopped", -2, "stopped", ""),
        ("failed", 3, "failed", "exit code 3"),
    ],
)
def test_each_terminal_state_maps_to_its_event(
    tmp_path: Path, state: str, exit_code: int, event: str, detail: str
) -> None:
    service, launcher, sender, _c, _s = _setup(tmp_path)
    launcher.snaps = [Snap("web-1", "https://t.example", "deep", state, exit_code)]

    service.poll_once()
    _drain(service)

    assert [n.event for _name, n in sender.sent] == ["started", event]
    assert sender.sent[-1][1].detail == detail


def test_notice_carries_run_facts_from_disk(tmp_path: Path) -> None:
    service, launcher, sender, clock, _s = _setup(tmp_path)
    run_dir = tmp_path / "strix_runs" / "web-1"
    run_dir.mkdir(parents=True)
    (run_dir / "vulnerabilities.json").write_text(
        json.dumps(
            [
                {"severity": "High"},
                {"severity": "critical"},
                {"severity": "info"},  # unknown buckets fold into low, like the UI
                {"severity": None},
                "garbage",
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "run.json").write_text(json.dumps({"llm_usage": {"cost": 2.5}}), encoding="utf-8")
    launcher.snaps = [Snap("web-1", "https://t.example", "quick", "running")]
    service.poll_once()
    clock.now += 125
    launcher.snaps[0].state = "finished"
    launcher.snaps[0].exit_code = 0
    service.poll_once()
    _drain(service)

    finished = sender.sent[-1][1]
    assert finished.event == "finished"
    assert finished.findings == {"critical": 1, "high": 1, "medium": 0, "low": 3}
    assert finished.cost_usd == 2.5
    assert finished.elapsed_seconds == 125
    assert (finished.target, finished.mode, finished.run_name) == (
        "https://t.example",
        "quick",
        "web-1",
    )


def test_missing_or_corrupt_run_files_do_not_break_a_notice(tmp_path: Path) -> None:
    service, launcher, sender, _c, _s = _setup(tmp_path)
    run_dir = tmp_path / "strix_runs" / "web-1"
    run_dir.mkdir(parents=True)
    (run_dir / "vulnerabilities.json").write_text("{not json", encoding="utf-8")
    (run_dir / "run.json").write_text("[1, 2]", encoding="utf-8")
    launcher.snaps = [Snap("web-1", "t", "quick", "finished", 0)]

    service.poll_once()
    _drain(service)

    assert [n.event for _n, n in sender.sent] == ["started", "finished"]
    assert sender.sent[-1][1].findings is None
    assert sender.sent[-1][1].cost_usd is None


def test_progress_is_off_by_default(tmp_path: Path) -> None:
    service, launcher, sender, clock, _s = _setup(tmp_path)
    launcher.snaps = [Snap("web-1", "t", "quick", "running")]
    service.poll_once()
    clock.now += 10 * 3600
    service.poll_once()
    _drain(service)
    assert sender.events == [("slack", "started")]


def test_progress_fires_each_interval_while_running(tmp_path: Path) -> None:
    service, launcher, sender, clock, _s = _setup(
        tmp_path,
        config={
            "enabled": True,
            "events": {"progress": True},
            "progress_interval_minutes": 10,
            "channels": {"slack": {"enabled": True, "webhook_url": SLACK_URL}},
        },
    )
    launcher.snaps = [Snap("web-1", "t", "quick", "running")]
    service.poll_once()  # started
    clock.now += 9 * 60
    service.poll_once()  # too early
    clock.now += 2 * 60
    service.poll_once()  # 11 minutes in: first progress
    clock.now += 5 * 60
    service.poll_once()  # only 5 minutes since the last one
    clock.now += 6 * 60
    service.poll_once()  # 11 minutes since the last one: second progress
    launcher.snaps[0].state = "finished"
    launcher.snaps[0].exit_code = 0
    clock.now += 60
    service.poll_once()
    _drain(service)

    assert [n.event for _n, n in sender.sent] == ["started", "progress", "progress", "finished"]
    assert sender.sent[1][1].elapsed_seconds == 11 * 60


def test_unsubscribed_events_are_skipped(tmp_path: Path) -> None:
    service, launcher, sender, _c, _s = _setup(
        tmp_path,
        config={
            "enabled": True,
            "events": {"started": False, "finished": False, "stopped": True, "failed": False},
            "channels": {"slack": {"enabled": True, "webhook_url": SLACK_URL}},
        },
    )
    launcher.snaps = [Snap("web-1", "t", "quick", "starting")]
    service.poll_once()
    launcher.snaps[0].state = "stopped"
    service.poll_once()
    _drain(service)
    assert [n.event for _n, n in sender.sent] == ["stopped"]


def test_master_switch_off_sends_nothing_and_does_not_replay_later(tmp_path: Path) -> None:
    service, launcher, sender, _c, store = _setup(
        tmp_path,
        config={
            "enabled": False,
            "channels": {"slack": {"enabled": True, "webhook_url": SLACK_URL}},
        },
    )
    launcher.snaps = [Snap("web-1", "t", "quick", "running")]
    service.poll_once()
    launcher.snaps[0].state = "finished"
    launcher.snaps[0].exit_code = 0
    service.poll_once()
    store.apply({"enabled": True})  # turned on afterwards
    service.poll_once()
    _drain(service)
    assert sender.sent == []  # past events are not replayed


def test_no_ready_channel_sends_nothing(tmp_path: Path) -> None:
    service, launcher, sender, _c, _s = _setup(
        tmp_path, config={"enabled": True, "channels": {"slack": {"webhook_url": SLACK_URL}}}
    )  # configured but not switched on
    launcher.snaps = [Snap("web-1", "t", "quick", "finished", 0)]
    service.poll_once()
    _drain(service)
    assert sender.sent == []


def test_every_ready_channel_gets_the_message_and_one_failure_does_not_block_others(
    tmp_path: Path,
) -> None:
    service, launcher, sender, _c, _s = _setup(
        tmp_path,
        sender=RecordingSender(fail={"slack"}),
        config={
            "enabled": True,
            "channels": {
                "slack": {"enabled": True, "webhook_url": SLACK_URL},
                "teams": {"enabled": True, "webhook_url": TEAMS_URL},
                "telegram": {"enabled": True, "bot_token": BOT_TOKEN, "chat_id": CHAT_ID},
            },
        },
    )
    launcher.snaps = [Snap("web-1", "t", "quick", "starting")]
    service.poll_once()
    _drain(service)

    assert [name for name, _n in sender.sent] == ["slack", "teams", "telegram"]
    assert service.counters.sent == 2
    assert service.counters.failed == 1


def test_a_config_change_between_queueing_and_sending_is_honoured(tmp_path: Path) -> None:
    service, launcher, sender, _c, store = _setup(tmp_path)
    launcher.snaps = [Snap("web-1", "t", "quick", "starting")]
    service.poll_once()  # queued while enabled
    store.apply({"enabled": False})  # user switches it off before delivery
    _drain(service)
    assert sender.sent == []


def test_delivery_failures_log_a_safe_message(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service, launcher, _sender, _c, _s = _setup(tmp_path, sender=RecordingSender(fail={"slack"}))
    launcher.snaps = [Snap("web-1", "t", "quick", "starting")]
    with caplog.at_level(logging.WARNING, logger="strix.interface.viewer.notifications"):
        service.poll_once()
        _drain(service)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "notification (slack, started) failed: boom" in text
    assert not any(secret in text for secret in SECRETS)


def test_a_full_queue_drops_instead_of_blocking(tmp_path: Path) -> None:
    service, launcher, sender, _c, _s = _setup(tmp_path)
    service._queue = queue.Queue(maxsize=1)
    launcher.snaps = [Snap("web-1", "t", "quick", "finished", 0)]  # started + finished = 2
    service.poll_once()
    _drain(service)
    assert len(sender.sent) == 1
    assert service.counters.dropped == 1


def test_two_scans_are_tracked_independently(tmp_path: Path) -> None:
    service, launcher, sender, _c, _s = _setup(tmp_path)
    launcher.snaps = [
        Snap("web-1", "a", "quick", "finished", 0),
        Snap("web-2", "b", "deep", "running"),
    ]
    service.poll_once()
    _drain(service)
    assert [(n.run_name, n.event) for _x, n in sender.sent] == [
        ("web-1", "started"),
        ("web-1", "finished"),
        ("web-2", "started"),
    ]


def test_service_without_a_launcher_is_inert(tmp_path: Path) -> None:
    service = NotificationService(_store(tmp_path))
    service.poll_once()  # nothing to watch, nothing to do
    service.start()
    service.stop(flush_timeout=1.0)


def test_poll_survives_a_launcher_that_raises_in_the_monitor_loop(tmp_path: Path) -> None:
    class Exploding:
        def snapshot(self) -> list[Any]:
            raise RuntimeError("boom")

    service = NotificationService(_store(tmp_path), launcher=Exploding(), poll_seconds=0.01)
    service.start()
    service._stop_event.wait(0.1)
    assert service._monitor is not None
    assert service._monitor.is_alive()
    # stop() does a final poll that also raises; it must be swallowed.
    service.stop(flush_timeout=1.0)


def test_stop_flushes_a_scan_that_ended_during_shutdown(tmp_path: Path) -> None:
    # Real threads: the viewer is shut down (Ctrl-C) while a scan is live. The
    # launcher stops it, and the service's final look must still announce it.
    service, launcher, sender, _c, _s = _setup(tmp_path)
    launcher.snaps = [Snap("web-1", "t", "quick", "running")]
    service.start()
    service.poll_once()  # the monitor has seen it running ("started" is queued)
    launcher.snaps[0].state = "stopped"  # shutdown stopped it
    launcher.snaps[0].exit_code = -2

    service.stop(flush_timeout=5.0)

    assert [n.event for _n, n in sender.sent] == ["started", "stopped"]


# --- test button ----------------------------------------------------------------


def test_send_test_uses_the_channel_and_marks_the_message_as_a_test(tmp_path: Path) -> None:
    service, _l, sender, _c, _s = _setup(tmp_path)

    result = service.send_test(_slack())

    assert result.ok is True
    ((name, notice),) = sender.sent
    assert name == "slack"
    assert notice.event == "test"
    assert "Test notification" in render_slack(notice)["text"]


def test_send_test_works_even_when_notifications_are_switched_off(tmp_path: Path) -> None:
    service, _l, sender, _c, _s = _setup(tmp_path, config={"enabled": False})
    assert service.send_test(_telegram()).ok is True
    assert len(sender.sent) == 1


def test_send_test_is_limited_to_six_per_minute(tmp_path: Path) -> None:
    service, _l, sender, _c, _s = _setup(tmp_path)
    for _ in range(6):
        service.send_test(_slack())
    with pytest.raises(LaunchError) as exc:
        service.send_test(_slack())
    assert exc.value.code == "rate_limited"
    assert exc.value.status == 429
    assert exc.value.extra["retry_after"] == 60
    assert len(sender.sent) == 6


def test_environment_is_not_required_for_default_paths() -> None:
    # The default store path lives under the user's home, not the cwd.
    store = NotificationStore()
    assert store.path.name == "notifications.json"
    assert store.path.parent.name == ".strix"
    assert Path.home() in store.path.parents


# --- integration with the real launcher ------------------------------------------


class _FakeProcess:
    """The slice of ``subprocess.Popen`` the launcher touches."""

    pid = 4242

    def __init__(self) -> None:
        self.stdout = io.StringIO("")
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return self.code if self.code is not None else -9

    def send_signal(self, sig: int) -> None:  # noqa: ARG002
        return None

    def kill(self) -> None:
        self.code = -9


def test_service_reads_the_real_launchers_snapshots(tmp_path: Path) -> None:
    # Guards against the service and ScanLauncher.snapshot() drifting apart, which
    # the FakeLauncher-based tests above cannot catch.
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    process = _FakeProcess()
    launcher = ScanLauncher(
        runs,
        LaunchLimits(),
        popen=lambda *_a, **_k: process,  # type: ignore[arg-type,return-value]
        resolver=lambda _h, _t: ["93.184.216.34"],
        classifier=lambda _t: "web_application",
        environ={"PATH": "/usr/bin"},
    )
    sender = RecordingSender()
    store = _store(tmp_path)
    store.apply(
        {"enabled": True, "channels": {"slack": {"enabled": True, "webhook_url": SLACK_URL}}}
    )
    service = NotificationService(
        store,
        launcher=launcher,
        runs_dir=runs,
        sender=sender,
        poll_seconds=60.0,  # type: ignore[arg-type]
    )

    run_name = launcher.launch(
        LaunchRequest(
            target="example.com",
            scan_mode="deep",
            max_budget_usd=None,
            instruction="SECRET-INSTRUCTION-TEXT",
            authorized=True,
            allow_private=False,
        )
    )
    service.poll_once()
    process.code = 2  # exit code 2 = "vulnerabilities found", still a finished scan
    service.poll_once()
    _drain(service)

    assert [n.event for _name, n in sender.sent] == ["started", "finished"]
    started = sender.sent[0][1]
    assert (started.run_name, started.mode) == (run_name, "deep")
    assert started.target == "https://example.com"
    rendered = repr(sender.sent) + json.dumps(render_slack(sender.sent[0][1]))
    assert "SECRET-INSTRUCTION-TEXT" not in rendered
