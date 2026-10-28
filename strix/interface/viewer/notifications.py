"""Scan notifications: render, send, and watch scans for lifecycle changes.

Three parts:

* **Rendering** turns a :class:`ScanNotice` into the payload each destination
  expects (Slack ``text``, a Teams Adaptive Card, a Telegram plain-text message).
* :class:`NotificationSender` posts one payload. It is deliberately strict: Slack
  and Teams URLs must be public ``https`` (re-checked against DNS on every send),
  redirects are never followed, requests time out, and no error message ever
  contains a webhook URL, bot token, or chat id.
* :class:`NotificationService` watches the :class:`ScanLauncher`'s children from a
  monitor thread and hands notices to a single delivery thread, so a slow or
  broken webhook can never stall a scan, the launcher, or an HTTP request.

What is sent: target, mode, run name, elapsed time, finding counts by severity
and (when non-zero) cost. Never the instruction text, an error tail, API keys, or
the viewer link (which carries the session token).
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote, urlsplit

from strix.core.paths import run_record_path
from strix.interface.viewer.launch_errors import LaunchError
from strix.interface.viewer.notification_store import (
    HostBlockedError,
    check_public_host,
)


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from strix.interface.viewer.notification_store import (
        ChannelConfig,
        NotificationConfig,
        NotificationStore,
        Resolver,
    )


logger = logging.getLogger(__name__)

_SEND_TIMEOUT_SECONDS = 10.0
_POLL_SECONDS = 2.0
_QUEUE_MAX = 200
_FLUSH_TIMEOUT_SECONDS = 10.0
_TEST_LIMIT = 6
_TEST_WINDOW_SECONDS = 60.0

_TELEGRAM_HOST = "api.telegram.org"
_TELEGRAM_MAX_CHARS = 4000  # the API limit is 4096; keep headroom
_FIELD_MAX_CHARS = 300
_ERROR_BODY_MAX_BYTES = 512

_TERMINAL_EVENT = {"finished": "finished", "stopped": "stopped", "failed": "failed"}

_TITLES = {
    "started": "Scan started",
    "progress": "Scan in progress",
    "finished": "Scan finished",
    "stopped": "Scan stopped",
    "failed": "Scan failed",
    "test": "Test notification",
}
_ICONS = {
    "started": "▶️",
    "progress": "⏳",
    "finished": "✅",
    "stopped": "⏹️",
    "failed": "❌",
    "test": "🔔",
}
_CHANNEL_LABELS = {"slack": "Slack", "teams": "Microsoft Teams", "telegram": "Telegram"}

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_SEVERITIES = ("critical", "high", "medium", "low")


# --- the notice and its renderings ------------------------------------------


@dataclass(frozen=True)
class ScanNotice:
    """Everything a message may say about a scan. All fields are display-safe values."""

    event: str
    run_name: str = ""
    target: str = ""
    mode: str = ""
    elapsed_seconds: float | None = None
    findings: dict[str, int] | None = None
    cost_usd: float | None = None
    detail: str = ""


def _clean(value: object, limit: int = _FIELD_MAX_CHARS) -> str:
    """Single-line, control-free, length-capped text."""
    text = _CONTROL_RE.sub(" ", str(value)).strip()
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_elapsed(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return ""
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_findings(findings: dict[str, int] | None) -> str:
    if findings is None:
        return ""
    total = sum(findings.get(s, 0) for s in _SEVERITIES)
    if total == 0:
        return "none"
    parts = [f"{findings[s]} {s}" for s in _SEVERITIES if findings.get(s, 0)]
    return f"{total} ({', '.join(parts)})"


def notice_facts(notice: ScanNotice) -> list[tuple[str, str]]:
    """The labelled facts shown under the title, in display order, empty ones omitted."""
    facts = [
        ("Target", _clean(notice.target)),
        ("Mode", _clean(notice.mode)),
        ("Run", _clean(notice.run_name)),
        ("Elapsed", format_elapsed(notice.elapsed_seconds)),
        ("Findings", format_findings(notice.findings)),
        ("Cost", f"${notice.cost_usd:.2f}" if notice.cost_usd else ""),
        ("Detail", _clean(notice.detail)),
    ]
    return [(k, v) for k, v in facts if v]


def _title(notice: ScanNotice) -> str:
    return f"{_ICONS.get(notice.event, '')} {_TITLES.get(notice.event, 'Scan update')}".strip()


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_slack(notice: ScanNotice) -> dict[str, Any]:
    lines = [f"*{_title(notice)}*"]
    lines += [f"*{label}:* {_slack_escape(value)}" for label, value in notice_facts(notice)]
    return {"text": "\n".join(lines)}


def render_teams(notice: ScanNotice) -> dict[str, Any]:
    """An Adaptive Card message (accepted by Teams Workflows and legacy webhooks)."""
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": _title(notice),
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
            },
            {
                "type": "FactSet",
                "facts": [{"title": f"{k}:", "value": v} for k, v in notice_facts(notice)],
            },
        ],
    }
    return {
        "type": "message",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
        ],
    }


def render_telegram(notice: ScanNotice, chat_id: str) -> dict[str, Any]:
    lines = [_title(notice)] + [f"{label}: {value}" for label, value in notice_facts(notice)]
    text = "\n".join(lines)
    if len(text) > _TELEGRAM_MAX_CHARS:
        text = text[: _TELEGRAM_MAX_CHARS - 1] + "…"
    return {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}


# --- the sender --------------------------------------------------------------


@dataclass(frozen=True)
class SendResult:
    """Outcome of one send. ``message`` is user-safe: no URL, token, or chat id."""

    ok: bool
    message: str
    status: int | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a webhook that bounces elsewhere is not followed (SSRF)."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        return None


class NotificationSender:
    """Posts one rendered notice to one destination."""

    def __init__(
        self,
        *,
        opener: Any = None,
        resolver: Resolver | None = None,
        timeout: float = _SEND_TIMEOUT_SECONDS,
    ) -> None:
        self._opener: Any = opener or urllib.request.build_opener(_NoRedirect)
        self._resolver = resolver
        self._timeout = timeout

    def send(self, channel: ChannelConfig, notice: ScanNotice) -> SendResult:
        label = _CHANNEL_LABELS.get(channel.name, channel.name)
        try:
            url, payload = self._build(channel, notice)
        except HostBlockedError as exc:
            reason = (
                "its host could not be resolved"
                if exc.reason == "unresolvable"
                else "it points at a private or blocked address"
            )
            return SendResult(ok=False, message=f"{label} was not contacted: {reason}.")
        request = urllib.request.Request(  # noqa: S310 - https only, validated above
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "strix-viewer"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status = int(getattr(response, "status", 200))
        except urllib.error.HTTPError as exc:
            return self._http_error(channel, label, exc)
        except TimeoutError:
            return SendResult(ok=False, message=f"{label} did not respond in time.")
        except (urllib.error.URLError, OSError):
            return SendResult(ok=False, message=f"Could not reach {label}.")
        if 200 <= status < 300:
            return SendResult(ok=True, message=f"Sent to {label}.", status=status)
        return SendResult(ok=False, message=f"{label} answered HTTP {status}.", status=status)

    def _build(self, channel: ChannelConfig, notice: ScanNotice) -> tuple[str, dict[str, Any]]:
        if channel.name == "telegram":
            url = f"https://{_TELEGRAM_HOST}/bot{quote(channel.bot_token, safe=':')}/sendMessage"
            return url, render_telegram(notice, channel.chat_id)
        host = urlsplit(channel.webhook_url).hostname or ""
        check_public_host(host, self._resolver, require_resolution=True)
        payload = render_slack(notice) if channel.name == "slack" else render_teams(notice)
        return channel.webhook_url, payload

    @staticmethod
    def _http_error(channel: ChannelConfig, label: str, exc: urllib.error.HTTPError) -> SendResult:
        code = int(exc.code)
        hint = ""
        if code in (401, 403, 404):
            hint = (
                " Check the bot token and chat id."
                if channel.name == "telegram"
                else " The webhook may be wrong, revoked, or deleted."
            )
        elif 300 <= code < 400:
            hint = " Redirects are not followed."
        elif code == 429:
            hint = " Rate limited; try again shortly."
        detail = ""
        if channel.name == "telegram":
            # Telegram's error body is a short JSON description. It does not
            # normally contain the token, but the token is scrubbed anyway so a
            # message shown in the browser or written to a log can never carry it.
            try:
                body = json.loads(exc.read(_ERROR_BODY_MAX_BYTES) or b"{}")
                description = body.get("description") if isinstance(body, dict) else None
                if isinstance(description, str) and description:
                    text = _clean(description, 120)
                    if channel.bot_token:
                        text = text.replace(channel.bot_token, "[redacted]")
                    detail = f" ({text})"
            except (OSError, ValueError):
                pass
        return SendResult(
            ok=False,
            message=f"{label} rejected the message (HTTP {code}){detail}.{hint}",
            status=code,
        )


# --- the service -------------------------------------------------------------


class _SnapshotLike(Protocol):
    @property
    def run_name(self) -> str: ...
    @property
    def target(self) -> str: ...
    @property
    def mode(self) -> str: ...
    @property
    def state(self) -> str: ...
    @property
    def exit_code(self) -> int | None: ...


class _LauncherLike(Protocol):
    def snapshot(self) -> list[Any]: ...


@dataclass
class _Tracked:
    first_seen: float
    last_progress: float
    terminal_sent: bool = False


_STOP = object()


@dataclass
class _Counters:
    """Cheap service-level diagnostics, exposed for tests and logs."""

    sent: int = 0
    failed: int = 0
    dropped: int = 0
    tests: deque[float] = field(default_factory=deque)


class NotificationService:
    """Watches launched scans and delivers lifecycle notifications."""

    def __init__(
        self,
        store: NotificationStore,
        *,
        launcher: _LauncherLike | None = None,
        runs_dir: Path | None = None,
        sender: NotificationSender | None = None,
        clock: Callable[[], float] | None = None,
        poll_seconds: float = _POLL_SECONDS,
    ) -> None:
        self.store = store
        self._launcher = launcher
        self._runs_dir = runs_dir
        self._sender = sender or NotificationSender()
        self._clock = clock or time.monotonic
        self._poll_seconds = poll_seconds
        self._tracked: dict[str, _Tracked] = {}
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=_QUEUE_MAX)
        self._stop_event = threading.Event()
        self._monitor: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._test_lock = threading.Lock()
        self.counters = _Counters()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the delivery thread, and the monitor when there is a launcher to watch."""
        if self._worker is None:
            self._worker = threading.Thread(
                target=self._deliver_loop, name="strix-notify-worker", daemon=True
            )
            self._worker.start()
        if self._launcher is not None and self._monitor is None:
            self._monitor = threading.Thread(
                target=self._monitor_loop, name="strix-notify-monitor", daemon=True
            )
            self._monitor.start()

    def stop(self, *, flush_timeout: float = _FLUSH_TIMEOUT_SECONDS) -> None:
        """Stop watching, take one last look (so scans stopped by shutdown are reported), flush."""
        self._stop_event.set()
        if self._monitor is not None:
            self._monitor.join(timeout=self._poll_seconds + 1.0)
        try:
            self.poll_once()
        except Exception:
            logger.exception("final notification poll failed")
        if self._worker is not None:
            self._queue.put(_STOP)
            self._worker.join(timeout=flush_timeout)

    # -- monitor -------------------------------------------------------------

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(self._poll_seconds):
            try:
                self.poll_once()
            except Exception:
                # A bad poll must never kill the monitor; the next one retries.
                logger.exception("notification poll failed")

    def poll_once(self) -> None:
        """Compare the launcher's children with what was already announced."""
        if self._launcher is None:
            return
        snapshots = self._launcher.snapshot()
        pending = [s for s in snapshots if not self._is_done(s.run_name)]
        if not pending:
            return
        config = self.store.load()
        now = self._clock()
        for snap in pending:
            tracked = self._tracked.get(snap.run_name)
            if tracked is None:
                tracked = _Tracked(first_seen=now, last_progress=now)
                self._tracked[snap.run_name] = tracked
                self._emit("started", snap, tracked, now, config)
            terminal = _TERMINAL_EVENT.get(snap.state)
            if terminal is not None:
                tracked.terminal_sent = True
                self._emit(terminal, snap, tracked, now, config)
            elif now - tracked.last_progress >= config.progress_interval_minutes * 60:
                tracked.last_progress = now
                self._emit("progress", snap, tracked, now, config)

    def _is_done(self, run_name: str) -> bool:
        tracked = self._tracked.get(run_name)
        return tracked is not None and tracked.terminal_sent

    def _emit(
        self,
        event: str,
        snap: _SnapshotLike,
        tracked: _Tracked,
        now: float,
        config: NotificationConfig,
    ) -> None:
        """Queue a notice if notifications are on, the event is subscribed, a channel is ready."""
        if not config.enabled or event not in config.events or not config.active_channels():
            return
        notice = self._build_notice(event, snap, now - tracked.first_seen)
        try:
            self._queue.put_nowait(notice)
        except queue.Full:
            self.counters.dropped += 1
            logger.warning("notification queue full; dropped a %s notice", event)

    def _build_notice(self, event: str, snap: _SnapshotLike, elapsed: float) -> ScanNotice:
        findings, cost = self._read_run(snap.run_name)
        detail = ""
        if event == "failed" and snap.exit_code is not None:
            detail = f"exit code {snap.exit_code}"
        return ScanNotice(
            event=event,
            run_name=snap.run_name,
            target=snap.target,
            mode=snap.mode,
            elapsed_seconds=elapsed,
            findings=findings,
            cost_usd=cost,
            detail=detail,
        )

    def _read_run(self, run_name: str) -> tuple[dict[str, int] | None, float | None]:
        """Finding counts and cost from the run's files. Missing or bad files give None."""
        if self._runs_dir is None:
            return None, None
        run_dir = self._runs_dir / run_name
        findings: dict[str, int] | None = None
        try:
            data = json.loads((run_dir / "vulnerabilities.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, list):
            findings = dict.fromkeys(_SEVERITIES, 0)
            for item in data:
                raw = item.get("severity") if isinstance(item, dict) else None
                severity = str(raw or "").lower().strip()
                findings[severity if severity in findings else "low"] += 1
        cost: float | None = None
        try:
            record = json.loads(run_record_path(run_dir).read_text(encoding="utf-8"))
            usage = record.get("llm_usage") if isinstance(record, dict) else None
            value = usage.get("cost") if isinstance(usage, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                cost = float(value)
        except (OSError, ValueError):
            pass
        return findings, cost

    # -- delivery ------------------------------------------------------------

    def _deliver_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            try:
                self._deliver(item)
            except Exception:
                logger.exception("notification delivery failed unexpectedly")

    def _deliver(self, notice: ScanNotice) -> None:
        """Send to every ready channel. Re-reads the config so a save applies at once."""
        config = self.store.load()
        if not config.enabled or notice.event not in config.events:
            return
        for channel in config.active_channels():
            result = self._sender.send(channel, notice)
            if result.ok:
                self.counters.sent += 1
            else:
                self.counters.failed += 1
                logger.warning(
                    "notification (%s, %s) failed: %s",
                    channel.name,
                    notice.event,
                    result.message,
                )

    # -- test button ---------------------------------------------------------

    def send_test(self, channel: ChannelConfig) -> SendResult:
        """Send a sample notice synchronously, limited to 6 per minute."""
        now = time.monotonic()
        with self._test_lock:
            window = self.counters.tests
            while window and window[0] <= now - _TEST_WINDOW_SECONDS:
                window.popleft()
            if len(window) >= _TEST_LIMIT:
                raise LaunchError(
                    "rate_limited",
                    "too many test notifications; try again shortly",
                    status=429,
                    extra={"retry_after": int(_TEST_WINDOW_SECONDS)},
                )
            window.append(now)
        notice = ScanNotice(
            event="test",
            run_name="test-run",
            target="https://example.com",
            mode="quick",
            detail="This is a test from the Strix viewer.",
        )
        return self._sender.send(channel, notice)


__all__ = [
    "NotificationSender",
    "NotificationService",
    "ScanNotice",
    "SendResult",
    "format_elapsed",
    "format_findings",
    "notice_facts",
    "render_slack",
    "render_teams",
    "render_telegram",
]
