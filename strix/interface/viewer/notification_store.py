"""Persistent configuration for scan notifications (Slack, Microsoft Teams, Telegram).

``NotificationStore`` owns ``~/.strix/notifications.json``. The file holds
secrets (webhook URLs and bot tokens), so it is written through the shared
``write_secret_text`` helper (atomic replace, owner-only mode) and the secrets
are write-only through the API: :meth:`NotificationStore.view` reports only
whether a secret is set (plus a webhook's host), never its value.

This module imports nothing from the scan engine, so it never pulls LiteLLM into
the viewer process (``memory/project.md`` c3).

Outbound-URL safety: a webhook must be a public ``https`` URL. It is checked
syntactically here (``is_safe_web_url``) and again against DNS at save time and
at every send (:func:`check_public_host`), so a webhook that resolves to a
private or cloud-metadata address is refused.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import sys
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from strix.interface.url_safety import is_safe_web_url
from strix.interface.viewer.launch_errors import LaunchError
from strix.interface.viewer.target_validation import (
    _METADATA_HOSTS,
    _RESOLVE_TIMEOUT,
    _default_resolver,
    _is_cloud_metadata,
    _is_private_class,
    _is_private_host_name,
)
from strix.utils.secret_files import write_secret_text


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    Resolver = Callable[[str, float], list[str]]


logger = logging.getLogger(__name__)

#: Scan lifecycle events a user can subscribe to. ``progress`` is the periodic
#: "still running" update sent while a scan is in flight.
EVENT_KEYS: tuple[str, ...] = ("started", "progress", "finished", "stopped", "failed")

#: Supported destinations.
CHANNEL_KEYS: tuple[str, ...] = ("slack", "teams", "telegram")

#: Bounds for the progress-update interval, in minutes.
INTERVAL_MIN = 5
INTERVAL_MAX = 240
INTERVAL_DEFAULT = 15

#: Fields that can be cleared, as ``<channel>.<field>``.
CLEARABLE: frozenset[str] = frozenset(
    {"slack.webhook_url", "teams.webhook_url", "telegram.bot_token", "telegram.chat_id"}
)

_CHANNEL_FIELDS: dict[str, tuple[str, ...]] = {
    "slack": ("webhook_url",),
    "teams": ("webhook_url",),
    "telegram": ("bot_token", "chat_id"),
}

_CHANNEL_LABELS = {"slack": "Slack", "teams": "Microsoft Teams", "telegram": "Telegram"}

_WEBHOOK_MAX_LEN = 2048

# Telegram bot tokens look like ``123456789:AA...`` (digits, colon, 30+ url-safe chars).
_BOT_TOKEN_RE = re.compile(r"^\d{5,16}:[A-Za-z0-9_-]{30,64}$")
# A chat id is a (possibly negative) integer, or a public ``@channelname``.
_CHAT_ID_RE = re.compile(r"^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")

_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "events": {
        "started": True,
        "progress": False,
        "finished": True,
        "stopped": True,
        "failed": True,
    },
    "progress_interval_minutes": INTERVAL_DEFAULT,
    "channels": {
        "slack": {"enabled": False, "webhook_url": ""},
        "teams": {"enabled": False, "webhook_url": ""},
        "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
    },
}

_TOP_LEVEL_KEYS = frozenset({"enabled", "events", "progress_interval_minutes", "channels", "clear"})


class HostBlockedError(Exception):
    """A destination host is private, cloud-metadata, or (when required) unresolvable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def check_public_host(
    host: str,
    resolver: Resolver | None = None,
    *,
    require_resolution: bool,
) -> None:
    """Raise :class:`HostBlockedError` unless ``host`` resolves only to public addresses.

    A literal IP is judged as-is. A name is resolved through the bounded resolver
    shared with target validation. When ``require_resolution`` is false an
    unresolvable name passes (a save made while offline must not fail); the send
    path passes true so an unresolvable host is reported instead of attempted.
    """
    name = host.lower().rstrip(".")
    if not name or name in _METADATA_HOSTS or _is_private_host_name(name):
        raise HostBlockedError("blocked")
    try:
        addresses = [str(ipaddress.ip_address(name))]
    except ValueError:
        try:
            addresses = list((resolver or _default_resolver)(name, _RESOLVE_TIMEOUT))
        except (OSError, ValueError, FutureTimeoutError) as exc:
            if require_resolution:
                raise HostBlockedError("unresolvable") from exc
            return
    if require_resolution and not addresses:
        raise HostBlockedError("unresolvable")
    if any(_is_cloud_metadata(addr) or _is_private_class(addr) for addr in addresses):
        raise HostBlockedError("blocked")


# --- validators --------------------------------------------------------------


def validate_webhook_url(raw: object, resolver: Resolver | None = None) -> str:
    """Return the trimmed webhook URL or raise ``invalid_webhook_url`` (400).

    The URL is a secret (it embeds the credential), so no message echoes it.
    """
    if not isinstance(raw, str):
        raise LaunchError("invalid_webhook_url", "the webhook URL must be a string", status=400)
    url = raw.strip()
    if not 1 <= len(url) <= _WEBHOOK_MAX_LEN:
        raise LaunchError(
            "invalid_webhook_url",
            f"the webhook URL must be 1 to {_WEBHOOK_MAX_LEN} characters",
            status=400,
        )
    if not is_safe_web_url(url):
        raise LaunchError(
            "invalid_webhook_url",
            "the webhook URL must be a public https:// address",
            status=400,
        )
    try:
        check_public_host(urlsplit(url).hostname or "", resolver, require_resolution=False)
    except HostBlockedError as exc:
        raise LaunchError(
            "invalid_webhook_url",
            "the webhook URL points at a private or blocked address",
            status=400,
        ) from exc
    return url


def validate_bot_token(raw: object) -> str:
    """Return the trimmed Telegram bot token or raise ``invalid_bot_token`` (400)."""
    if not isinstance(raw, str) or not _BOT_TOKEN_RE.match(raw.strip()):
        raise LaunchError(
            "invalid_bot_token",
            "the Telegram bot token must look like 123456789:AA... (from @BotFather)",
            status=400,
        )
    return raw.strip()


def validate_chat_id(raw: object) -> str:
    """Return the trimmed Telegram chat id or raise ``invalid_chat_id`` (400)."""
    if not isinstance(raw, str) or not _CHAT_ID_RE.match(raw.strip()):
        raise LaunchError(
            "invalid_chat_id",
            "the Telegram chat id must be a number (for example -1001234567890) or @channelname",
            status=400,
        )
    return raw.strip()


def validate_interval(raw: object) -> int:
    """Return the progress interval in minutes or raise ``invalid_interval`` (400)."""
    if isinstance(raw, bool) or not isinstance(raw, int) or not INTERVAL_MIN <= raw <= INTERVAL_MAX:
        raise LaunchError(
            "invalid_interval",
            f"the progress interval must be a whole number of minutes "
            f"from {INTERVAL_MIN} to {INTERVAL_MAX}",
            status=400,
        )
    return raw


# --- typed config handed to the sender --------------------------------------


@dataclass(frozen=True)
class ChannelConfig:
    """One destination's effective settings (secrets included; never serialised)."""

    name: str
    enabled: bool
    webhook_url: str = ""
    bot_token: str = ""
    chat_id: str = ""

    @property
    def configured(self) -> bool:
        if self.name == "telegram":
            return bool(self.bot_token and self.chat_id)
        return bool(self.webhook_url)

    def __repr__(self) -> str:  # never leak secrets through a stray log of the object
        return (
            f"ChannelConfig(name={self.name!r}, enabled={self.enabled}, "
            f"configured={self.configured})"
        )


@dataclass(frozen=True)
class NotificationConfig:
    """The whole effective configuration, as the service consumes it."""

    enabled: bool
    events: frozenset[str]
    progress_interval_minutes: int
    channels: tuple[ChannelConfig, ...]

    def active_channels(self) -> list[ChannelConfig]:
        """Channels that are switched on AND fully configured."""
        return [c for c in self.channels if c.enabled and c.configured]


# --- the store ---------------------------------------------------------------


def _coerce(data: Any) -> dict[str, Any]:
    """Merge ``data`` over the defaults, discarding anything of the wrong type."""
    out = deepcopy(_DEFAULTS)
    if not isinstance(data, dict):
        return out
    if isinstance(data.get("enabled"), bool):
        out["enabled"] = data["enabled"]
    events = data.get("events")
    if isinstance(events, dict):
        for key in EVENT_KEYS:
            if isinstance(events.get(key), bool):
                out["events"][key] = events[key]
    interval = data.get("progress_interval_minutes")
    if (
        isinstance(interval, int)
        and not isinstance(interval, bool)
        and INTERVAL_MIN <= interval <= INTERVAL_MAX
    ):
        out["progress_interval_minutes"] = interval
    channels = data.get("channels")
    if isinstance(channels, dict):
        for name in CHANNEL_KEYS:
            entry = channels.get(name)
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("enabled"), bool):
                out["channels"][name]["enabled"] = entry["enabled"]
            for field in _CHANNEL_FIELDS[name]:
                if isinstance(entry.get(field), str):
                    out["channels"][name][field] = entry[field]
    return out


class NotificationStore:
    """Reads and writes ``~/.strix/notifications.json`` under one process-wide lock."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        resolver: Resolver | None = None,
        platform: str | None = None,
    ) -> None:
        self.path: Path = (
            path if path is not None else Path.home() / ".strix" / "notifications.json"
        )
        self._resolver = resolver
        self._platform = platform if platform is not None else sys.platform
        self._lock = threading.Lock()

    # --- file access (callers hold the lock) --------------------------------

    def _read_raw(self) -> tuple[dict[str, Any], str]:
        """Return (coerced config, file_status). ``unreadable`` yields defaults."""
        if not self.path.exists():
            return _coerce(None), "missing"
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return _coerce(None), "unreadable"
        if not isinstance(data, dict):
            return _coerce(None), "unreadable"
        return _coerce(data), "ok"

    def _display_path(self) -> str:
        default = Path.home() / ".strix" / "notifications.json"
        return "~/.strix/notifications.json" if self.path == default else str(self.path)

    # --- reads --------------------------------------------------------------

    def view(self, *, editable: bool) -> dict[str, Any]:
        """The UI view: flags and hints only, never a webhook URL or bot token."""
        with self._lock:
            cfg, status = self._read_raw()
        channels: dict[str, Any] = {}
        for name in CHANNEL_KEYS:
            entry = cfg["channels"][name]
            channel = ChannelConfig(name=name, **entry)
            info: dict[str, Any] = {"enabled": entry["enabled"], "configured": channel.configured}
            if name == "telegram":
                info["token_set"] = bool(entry["bot_token"])
                info["chat_id"] = entry["chat_id"]
            else:
                url = entry["webhook_url"]
                info["host"] = (urlsplit(url).hostname or None) if url else None
            channels[name] = info
        return {
            "enabled": cfg["enabled"],
            "events": dict(cfg["events"]),
            "progress_interval_minutes": cfg["progress_interval_minutes"],
            "interval_min": INTERVAL_MIN,
            "interval_max": INTERVAL_MAX,
            "channels": channels,
            "editable": editable,
            "file_status": status,
            "config_path": self._display_path(),
            "platform": self._platform,
        }

    def load(self) -> NotificationConfig:
        """The effective config for the sender. Disabled defaults if the file is unreadable."""
        with self._lock:
            cfg, status = self._read_raw()
        if status == "unreadable":
            cfg = _coerce(None)
        return _to_config(cfg)

    # --- writes -------------------------------------------------------------

    def apply(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and save a partial update; return the refreshed view.

        Order: syntax/DNS validation of every provided value OUTSIDE the lock,
        then under the lock re-read, merge, check that every enabled channel is
        complete, and write atomically. An unreadable file is never overwritten.
        """
        updates, clear = self._parse_body(body)
        with self._lock:
            cfg, status = self._read_raw()
            if status == "unreadable":
                raise LaunchError(
                    "config_unreadable",
                    "the notifications file is not valid JSON; it was left unchanged",
                    status=409,
                )
            self._merge(cfg, updates, clear)
            self._require_complete(cfg)
            write_secret_text(self.path, json.dumps({"version": 1, **cfg}, indent=2))
            self._audit(cfg)
        return self.view(editable=True)

    def _parse_body(self, body: Mapping[str, Any]) -> tuple[dict[str, Any], set[str]]:
        if not isinstance(body, dict):
            raise LaunchError("invalid_json", "request body must be a JSON object", status=400)
        unknown = set(body) - _TOP_LEVEL_KEYS
        if unknown:
            names = ", ".join(sorted(map(str, unknown)))
            raise LaunchError("unknown_field", f"unknown field(s): {names}", status=400)
        updates: dict[str, Any] = {}
        if "enabled" in body:
            updates["enabled"] = _require_bool(body["enabled"], "enabled")
        if "progress_interval_minutes" in body:
            updates["progress_interval_minutes"] = validate_interval(
                body["progress_interval_minutes"]
            )
        if "events" in body:
            events = body["events"]
            if not isinstance(events, dict) or set(events) - set(EVENT_KEYS):
                raise LaunchError("invalid_json", "events must name only known events", status=400)
            updates["events"] = {k: _require_bool(v, f"events.{k}") for k, v in events.items()}
        channels_update: dict[str, dict[str, Any]] = {}
        if "channels" in body:
            channels = body["channels"]
            if not isinstance(channels, dict) or set(channels) - set(CHANNEL_KEYS):
                raise LaunchError(
                    "invalid_json", "channels must name only known channels", status=400
                )
            for name, entry in channels.items():
                channels_update[name] = self._parse_channel(name, entry)
        updates["channels"] = channels_update
        clear = self._parse_clear(body.get("clear"))
        for name, entry in channels_update.items():
            for field in entry:
                if f"{name}.{field}" in clear:
                    raise LaunchError(
                        "invalid_json", f"{name}.{field} cannot be both set and cleared", status=400
                    )
        return updates, clear

    def _parse_channel(self, name: str, entry: object) -> dict[str, Any]:
        allowed = {"enabled", *_CHANNEL_FIELDS[name]}
        if not isinstance(entry, dict) or set(entry) - allowed:
            raise LaunchError("invalid_json", f"{name} has unknown or malformed fields", status=400)
        out: dict[str, Any] = {}
        if "enabled" in entry:
            out["enabled"] = _require_bool(entry["enabled"], f"{name}.enabled")
        if "webhook_url" in entry:
            out["webhook_url"] = validate_webhook_url(entry["webhook_url"], self._resolver)
        if "bot_token" in entry:
            out["bot_token"] = validate_bot_token(entry["bot_token"])
        if "chat_id" in entry:
            out["chat_id"] = validate_chat_id(entry["chat_id"])
        return out

    @staticmethod
    def _parse_clear(raw: object) -> set[str]:
        if raw is None:
            return set()
        if not isinstance(raw, list) or any(not isinstance(i, str) for i in raw):
            raise LaunchError("invalid_json", "clear must be a list of field names", status=400)
        bad = set(raw) - CLEARABLE
        if bad:
            raise LaunchError(
                "unknown_field", f"cannot clear: {', '.join(sorted(bad))}", status=400
            )
        return set(raw)

    @staticmethod
    def _merge(cfg: dict[str, Any], updates: Mapping[str, Any], clear: set[str]) -> None:
        for key in ("enabled", "progress_interval_minutes"):
            if key in updates:
                cfg[key] = updates[key]
        cfg["events"].update(updates.get("events", {}))
        for name, entry in updates.get("channels", {}).items():
            cfg["channels"][name].update(entry)
        for dotted in clear:
            name, field = dotted.split(".", 1)
            cfg["channels"][name][field] = ""

    @staticmethod
    def _require_complete(cfg: Mapping[str, Any]) -> None:
        """An enabled channel must have everything it needs to send."""
        for name in CHANNEL_KEYS:
            entry = cfg["channels"][name]
            if entry["enabled"] and not ChannelConfig(name=name, **entry).configured:
                needs = "a bot token and chat id" if name == "telegram" else "a webhook URL"
                raise LaunchError(
                    "channel_incomplete",
                    f"{_CHANNEL_LABELS[name]} needs {needs} before it can be enabled",
                    status=400,
                )

    @staticmethod
    def _audit(cfg: Mapping[str, Any]) -> None:
        """One line per save: which switches are on. No URL, token, or chat id."""
        active = [n for n in CHANNEL_KEYS if cfg["channels"][n]["enabled"]]
        logger.info(
            "viewer notifications saved enabled=%s channels=%s events=%s",
            cfg["enabled"],
            ",".join(active) or "none",
            ",".join(k for k in EVENT_KEYS if cfg["events"][k]) or "none",
        )

    # --- test support -------------------------------------------------------

    def resolve_test_channel(self, channel: str, body: Mapping[str, Any]) -> ChannelConfig:
        """The channel config for a test send: saved values overlaid with typed ones.

        Lets the user test what they just typed before saving. Typed values are
        validated exactly like a save; the result must be complete.
        """
        if channel not in CHANNEL_KEYS:
            raise LaunchError(
                "invalid_channel", "channel must be slack, teams, or telegram", status=400
            )
        typed = {k: v for k, v in body.items() if k != "channel"}
        overrides = self._parse_channel(channel, typed)
        with self._lock:
            cfg, _status = self._read_raw()
        merged = {**cfg["channels"][channel], **overrides}
        resolved = ChannelConfig(name=channel, **merged)
        if not resolved.configured:
            needs = "a bot token and chat id" if channel == "telegram" else "a webhook URL"
            raise LaunchError(
                "channel_incomplete",
                f"{_CHANNEL_LABELS[channel]} needs {needs} before it can be tested",
                status=400,
            )
        return resolved


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise LaunchError("invalid_json", f"{label} must be true or false", status=400)
    return value


def _to_config(cfg: Mapping[str, Any]) -> NotificationConfig:
    return NotificationConfig(
        enabled=bool(cfg["enabled"]),
        events=frozenset(k for k in EVENT_KEYS if cfg["events"][k]),
        progress_interval_minutes=int(cfg["progress_interval_minutes"]),
        channels=tuple(ChannelConfig(name=n, **cfg["channels"][n]) for n in CHANNEL_KEYS),
    )


__all__ = [
    "CHANNEL_KEYS",
    "EVENT_KEYS",
    "INTERVAL_DEFAULT",
    "INTERVAL_MAX",
    "INTERVAL_MIN",
    "ChannelConfig",
    "HostBlockedError",
    "NotificationConfig",
    "NotificationStore",
    "check_public_host",
    "validate_bot_token",
    "validate_chat_id",
    "validate_interval",
    "validate_webhook_url",
]
