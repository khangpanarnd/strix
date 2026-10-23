"""Target validation for the viewer launch endpoint (section 3, BR-11..BR-17).

A browser-supplied target is syntax-checked, address-class-checked, resolved
(for domains) and classified before any scan is spawned. Private and cloud
metadata destinations are blocked; the resolver and classifier are injectable
so tests perform no real DNS or network I/O.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from urllib.parse import urlsplit

from strix.interface.utils import infer_target_type
from strix.interface.viewer.launch_errors import LaunchError


_MAX_TARGET_LEN = 2048
_MAX_HOST_LEN = 253
_RESOLVE_TIMEOUT = 3.0
_MESSAGE_ECHO_LEN = 80

# Host names that address a cloud metadata service; blocked before any DNS.
_METADATA_HOSTS = frozenset(
    {"metadata.google.internal", "metadata", "instance-data", "metadata.azure.com"}
)

# Literal addresses of the known cloud metadata services (after IPv4-unmapping).
_METADATA_ADDRESSES = frozenset(
    {"169.254.169.254", "169.254.170.2", "fd00:ec2::254", "100.100.100.200"}
)


@dataclass(frozen=True)
class ValidatedTarget:
    """A target that passed every check, ready to spawn a scan against."""

    normalized: str
    host: str
    addresses: tuple[str, ...]


# A resolver maps (host, timeout) -> list of address strings; the default below
# wraps socket.getaddrinfo on a bounded thread pool so a slow lookup times out.
Resolver = Callable[[str, float], list[str]]
Classifier = Callable[[str], str]

_RESOLVER_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="viewer-dns")


def _echo(raw: object) -> str:
    """At most 80 characters of ``raw`` with control characters stripped.

    Used in error messages so a hostile input cannot reflect control sequences
    or large bodies back through the response (BR-17).
    """
    text = raw if isinstance(raw, str) else str(raw)
    cleaned = "".join(ch for ch in text if ch.isprintable())
    return cleaned[:_MESSAGE_ECHO_LEN]


def _invalid_target(raw: object, reason: str) -> LaunchError:
    return LaunchError("invalid_target", f"{reason}: {_echo(raw)}", status=400)


def _unresolvable(raw: object) -> LaunchError:
    return LaunchError("target_unresolvable", f"could not resolve target: {_echo(raw)}", status=400)


def _default_resolver(host: str, timeout: float) -> list[str]:
    """Resolve A and AAAA records for ``host`` with a hard ``timeout``.

    ``socket.getaddrinfo`` cannot be interrupted, so it runs on a bounded daemon
    pool and the caller abandons the worker on timeout; launches are rate limited
    so abandoned workers cannot pile up.
    """
    future = _RESOLVER_POOL.submit(socket.getaddrinfo, host, None)
    infos = future.result(timeout=timeout)
    return [str(info[4][0]) for info in infos]


def _default_classifier(target: str) -> str:
    """Classify via the CLI's own rule, without its outbound git probe (DD-7)."""
    return infer_target_type(target, probe_git=False)[0]


def _unmap(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Return the embedded IPv4 of an ``::ffff:a.b.c.d`` address, else ``addr``."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _is_cloud_metadata(address: str) -> bool:
    try:
        addr = _unmap(ipaddress.ip_address(address))
    except ValueError:
        return False
    return str(addr) in _METADATA_ADDRESSES


def _is_private_class(address: str) -> bool:
    try:
        addr = _unmap(ipaddress.ip_address(address))
    except ValueError:
        return False
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_unspecified
        or addr.is_multicast
        or addr.is_reserved
    )


def _is_private_host_name(host: str) -> bool:
    return host == "localhost" or host.endswith((".localhost", ".local"))


def _parse_host(raw: object) -> tuple[str, str | None]:
    """Split a validated target string into (normalized host, scheme-or-None).

    The host is lowercased with a trailing dot stripped; ``scheme`` is set only
    when the input was a URL. Raises ``invalid_target`` on any disallowed syntax.
    """
    if not isinstance(raw, str):
        raise _invalid_target(raw, "target must be a string")
    text = raw
    try:
        text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise _invalid_target(raw, "target must be ASCII") from exc
    if not 1 <= len(text) <= _MAX_TARGET_LEN:
        raise _invalid_target(raw, "target length out of range")
    if text.startswith("-"):
        raise _invalid_target(raw, "target must not start with '-'")
    if any(ord(ch) < 0x20 or ch == " " for ch in text):
        raise _invalid_target(raw, "target must not contain whitespace or control characters")

    host, scheme = _host_from_url(raw, text) if "://" in text else _host_from_bare(raw, text)
    normalized_host = host.lower().rstrip(".")
    if not normalized_host:
        raise _invalid_target(raw, "target has an empty host")
    return normalized_host, scheme


def _host_from_url(raw: object, text: str) -> tuple[str, str]:
    split = urlsplit(text)
    if split.scheme not in ("http", "https"):
        raise _invalid_target(raw, "only http and https URLs are allowed")
    if split.username or split.password:
        raise _invalid_target(raw, "credentials are not allowed in the target")
    if split.fragment:
        raise _invalid_target(raw, "fragments are not allowed in the target")
    if "%" in split.netloc:
        raise _invalid_target(raw, "'%' is not allowed in the host")
    try:
        _ = split.port
    except ValueError as exc:
        raise _invalid_target(raw, "invalid port") from exc
    host = split.hostname or ""
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise _invalid_target(raw, "non-ASCII host") from exc
    if not host:
        raise _invalid_target(raw, "target has an empty host")
    return host, split.scheme


def _host_from_bare(raw: object, text: str) -> tuple[str, None]:
    for forbidden in ("/", "\\", "?", "#", "@"):
        if forbidden in text:
            raise _invalid_target(raw, "repository or path-style targets are not allowed")
    if text.endswith(".git"):
        raise _invalid_target(raw, "repository targets are not allowed")
    if text.startswith("["):
        closing = text.find("]")
        if closing == -1:
            raise _invalid_target(raw, "unterminated IPv6 bracket")
        host = text[1:closing]
        rest = text[closing + 1 :]
        if rest and not rest.startswith(":"):
            raise _invalid_target(raw, "invalid characters after IPv6 address")
        return host, None
    if text.count(":") > 1:
        # A bare IPv6 literal has more than one colon.
        return text, None
    host = text.rsplit(":", 1)[0] if ":" in text else text
    return host, None


def _resolve_addresses(raw: object, host: str, resolver: Resolver) -> list[str]:
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None
    if ip_obj is not None:
        return [str(ip_obj)]

    labels = host.split(".")
    if len(host) > _MAX_HOST_LEN or "." not in host or any(not label for label in labels):
        raise _invalid_target(raw, "invalid host name")
    try:
        addresses = resolver(host, _RESOLVE_TIMEOUT)
    except FutureTimeoutError as exc:
        raise _unresolvable(raw) from exc
    except (OSError, ValueError) as exc:
        raise _unresolvable(raw) from exc
    if not addresses:
        raise _unresolvable(raw)
    return list(addresses)


def _check_addresses(raw: object, host: str, addresses: list[str], *, allow_private: bool) -> None:
    for address in addresses:
        if _is_cloud_metadata(address):
            raise LaunchError(
                "target_blocked_metadata",
                f"target resolves to a cloud metadata address: {_echo(raw)}",
                status=400,
            )
        if not allow_private and _is_private_class(address):
            raise LaunchError(
                "target_blocked_private",
                f"target resolves to a private address: {_echo(raw)}",
                status=400,
            )
    if not allow_private and _is_private_host_name(host):
        raise LaunchError(
            "target_blocked_private",
            f"target is a private host name: {_echo(raw)}",
            status=400,
        )


def _normalize(text: str, host: str, scheme: str | None) -> str:
    """Build the canonical target the scan is spawned against (section 3 step 6)."""
    if scheme is not None:
        return text
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # A bare IP literal (v4 or v6) is passed through without a port.
        return host
    if text.startswith("["):
        closing = text.find("]")
        port_part = text[closing + 1 :]
    elif text.count(":") == 1:
        port_part = ":" + text.rsplit(":", 1)[1]
    else:
        port_part = ""
    host_segment = f"[{host}]" if ":" in host else host
    return f"https://{host_segment}{port_part}"


def validate_target(
    raw: object,
    *,
    allow_private: bool = False,
    resolver: Resolver | None = None,
    classifier: Classifier | None = None,
) -> ValidatedTarget:
    """Validate, resolve and classify a browser-supplied scan target.

    Raises :class:`LaunchError` with one of ``invalid_target``,
    ``target_blocked_private``, ``target_blocked_metadata`` or
    ``target_unresolvable`` (all HTTP 400). On success returns the canonical
    target, its host, and every resolved address.
    """
    resolve = resolver or _default_resolver
    classify = classifier or _default_classifier

    host, scheme = _parse_host(raw)

    if host in _METADATA_HOSTS:
        raise LaunchError(
            "target_blocked_metadata",
            f"target is a cloud metadata host name: {_echo(raw)}",
            status=400,
        )

    # localhost / *.localhost / *.local are private-class by name, so they are
    # blocked before DNS when allow_private is off (section 3 step 5). With the
    # flag set they fall through to normal resolution.
    if not allow_private and _is_private_host_name(host):
        raise LaunchError(
            "target_blocked_private",
            f"target is a private host name: {_echo(raw)}",
            status=400,
        )

    addresses = _resolve_addresses(raw, host, resolve)
    _check_addresses(raw, host, addresses, allow_private=allow_private)
    text = raw if isinstance(raw, str) else str(raw)
    normalized = _normalize(text, host, scheme)

    classification = classify(normalized)
    if classification not in ("web_application", "ip_address"):
        raise _invalid_target(raw, "this looks like a repository or local path")

    return ValidatedTarget(normalized=normalized, host=host, addresses=tuple(addresses))
