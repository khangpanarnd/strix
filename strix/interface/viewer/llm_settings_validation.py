"""Field validators for the viewer LLM settings page (FR-21..FR-25, BR-6..BR-11, BR-17).

Each validator syntax-checks one browser-supplied field and raises
:class:`LaunchError` with a stable code and HTTP 400 on failure (the error
table in ``business-rules.md`` §8). Messages are sanitised through the one
shared ``echo_input`` helper and never echo an API key (BR-12, BR-17).

The API-base validator reuses the address classifiers, the bounded DNS
resolver and the metadata/private-class rules already proven in
``target_validation`` (its private helpers are imported here on purpose:
both modules live in the same package and must agree bit-for-bit on what
counts as a metadata or private address, so re-deriving the rule would risk
divergence). The header-safety rule is copied — not imported — from
``strix.interface.scan_setup.check_header_safe_credentials`` because importing
anything from the scan engine would pull LiteLLM into the viewer process,
which the project rules forbid (``memory/project.md`` c3; DD-4).
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from strix.interface.viewer.launch_errors import LaunchError

# Imported from the sibling validator so metadata/private classification and
# DNS bounds stay identical across the two browser-input surfaces (see module
# docstring). These are package-internal helpers, reused by design.
from strix.interface.viewer.target_validation import (
    _METADATA_HOSTS,
    _RESOLVE_TIMEOUT,
    _default_resolver,
    _is_cloud_metadata,
    _is_private_class,
    _is_private_host_name,
    echo_input,
)


if TYPE_CHECKING:
    from collections.abc import Callable

    Resolver = Callable[[str, float], list[str]]


# --- field limits and allowed values (BR-6..BR-9) ---------------------------

_MODEL_MIN_LEN = 1
_MODEL_MAX_LEN = 200
_API_KEY_MIN_LEN = 1
_API_KEY_MAX_LEN = 4096
_API_BASE_MAX_LEN = 2048

# The seven reasoning-effort values, matching ``settings.ReasoningEffort``.
REASONING_EFFORT_VALUES: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

# OpenAI-style bare-model prefixes (DD-4). A bare model name matching none of
# these, with no effective API base, earns the advisory ``model_unfamiliar``
# warning rather than a hard rejection.
_OPENAI_MODEL_PREFIX = re.compile(r"^(gpt-|chatgpt-|o[0-9]|text-|ft:|omni-|codex-)")

_MODEL_UNFAMILIAR = "model_unfamiliar"


def _is_control_char(char: str) -> bool:
    """True for C0/C1 control characters (U+0000..U+001F, U+007F..U+009F)."""
    codepoint = ord(char)
    return codepoint <= 0x1F or 0x7F <= codepoint <= 0x9F


def validate_model(raw: object) -> str:
    """Return the trimmed model name, or raise ``invalid_model`` (BR-6, FR-21).

    1 to 200 characters after trimming; no whitespace, no control characters.
    An empty string is invalid (clearing is only through ``clear``; BR-11).
    """
    if not isinstance(raw, str):
        raise LaunchError("invalid_model", "model must be a string", status=400)
    model = raw.strip()
    if not _MODEL_MIN_LEN <= len(model) <= _MODEL_MAX_LEN:
        raise LaunchError(
            "invalid_model",
            f"model must be 1 to 200 characters: {echo_input(raw)}",
            status=400,
        )
    if any(ch.isspace() or _is_control_char(ch) for ch in model):
        raise LaunchError(
            "invalid_model",
            f"model must not contain whitespace or control characters: {echo_input(raw)}",
            status=400,
        )
    return model


def validate_reasoning_effort(raw: object) -> str:
    """Return the lower-cased reasoning effort, or raise ``invalid_reasoning_effort``.

    One of the seven allowed values, matched case-insensitively (BR-7, FR-22).
    """
    if not isinstance(raw, str):
        raise LaunchError(
            "invalid_reasoning_effort", "reasoning effort must be a string", status=400
        )
    value = raw.strip().lower()
    if value not in REASONING_EFFORT_VALUES:
        raise LaunchError(
            "invalid_reasoning_effort",
            f"reasoning effort must be one of {', '.join(REASONING_EFFORT_VALUES)}: "
            f"{echo_input(raw)}",
            status=400,
        )
    return value


def validate_api_key(raw: object) -> str:
    """Return the trimmed API key, or raise ``invalid_api_key`` (BR-8, FR-23).

    1 to 4096 characters after trimming; no control characters; header-safe.
    The error message NEVER echoes the key or any part of it (BR-12): only the
    offending character's Unicode name and position are reported, mirroring the
    CLI's header-safety rule (``scan_setup.check_header_safe_credentials``)
    without importing it (that import would pull LiteLLM into the viewer).
    """
    if not isinstance(raw, str):
        raise LaunchError("invalid_api_key", "api key must be a string", status=400)
    key = raw.strip()
    if not _API_KEY_MIN_LEN <= len(key) <= _API_KEY_MAX_LEN:
        # No echo: the length alone must not leak (BR-12 forbids length too).
        raise LaunchError("invalid_api_key", "api key must be 1 to 4096 characters", status=400)
    if any(_is_control_char(ch) for ch in key):
        raise LaunchError(
            "invalid_api_key", "api key must not contain control characters", status=400
        )
    _check_header_safe(key)
    return key


def _check_header_safe(value: str) -> None:
    """Raise ``invalid_api_key`` if ``value`` has a non-ASCII (non-header) char.

    This is the CLI's header-safety rule copied from
    ``scan_setup.check_header_safe_credentials`` / ``_first_non_ascii``: a
    credential sent as an HTTP header must be ASCII. The character is named by
    position but the value itself is never echoed (BR-12, FR-23).
    """
    for position, char in enumerate(value):
        if ord(char) > 0x7F:
            raise LaunchError(
                "invalid_api_key",
                "api key contains a character that cannot be sent in an HTTP header: "
                f"U+{ord(char):04X} ({unicodedata.name(char, 'unnamed character')}) "
                f"at position {position + 1}",
                status=400,
            )


def validate_api_base(raw: object, *, resolver: Resolver | None = None) -> str:
    """Return the trimmed API base URL, or raise ``api_base_*`` (BR-9, FR-24).

    Rules, in order:

    * at most 2048 characters; scheme ``http`` or ``https``; a host; no
      userinfo; no fragment; else ``invalid_api_base``;
    * a literal or resolved cloud-metadata address, or a metadata host name
      -> ``api_base_blocked`` (always, regardless of scheme);
    * ``http://`` is accepted only when every resolved address is loopback or
      private-class (``localhost`` / ``*.localhost`` count as loopback by
      name), else ``api_base_requires_https``;
    * ``https://`` is accepted for any non-metadata host;
    * DNS failure or timeout -> ``api_base_unresolvable``.

    DNS is bounded to 3 seconds and every A/AAAA address is checked. The
    resolver is injectable so tests perform no real lookups.
    """
    resolve = resolver or _default_resolver
    if not isinstance(raw, str):
        raise LaunchError("invalid_api_base", "api base must be a string", status=400)
    base = raw.strip()
    if not base:
        # Empty is invalid here; clearing a base is done through ``clear`` (BR-11).
        raise LaunchError("invalid_api_base", "api base must not be empty", status=400)
    if len(base) > _API_BASE_MAX_LEN:
        raise LaunchError(
            "invalid_api_base",
            f"api base must be at most 2048 characters: {echo_input(raw)}",
            status=400,
        )
    if any(_is_control_char(ch) or ch.isspace() for ch in base):
        raise LaunchError(
            "invalid_api_base",
            f"api base must not contain whitespace or control characters: {echo_input(raw)}",
            status=400,
        )

    host, scheme = _parse_base_host(raw, base)

    # Metadata host names are blocked before any DNS (as target_validation does).
    if host in _METADATA_HOSTS:
        raise LaunchError(
            "api_base_blocked",
            f"api base is a cloud metadata host name: {echo_input(raw)}",
            status=400,
        )

    addresses = _resolve_base(raw, host, resolve)
    _check_base_addresses(raw, host, scheme, addresses)
    return base


def _parse_base_host(raw: object, base: str) -> tuple[str, str]:
    """Split the base URL into (lower-cased host, scheme), rejecting bad syntax."""
    split = urlsplit(base)
    if split.scheme not in ("http", "https"):
        raise LaunchError(
            "invalid_api_base",
            f"api base must use http or https: {echo_input(raw)}",
            status=400,
        )
    if split.username or split.password:
        raise LaunchError(
            "invalid_api_base",
            f"api base must not contain credentials: {echo_input(raw)}",
            status=400,
        )
    if split.fragment:
        raise LaunchError(
            "invalid_api_base",
            f"api base must not contain a fragment: {echo_input(raw)}",
            status=400,
        )
    try:
        _ = split.port
    except ValueError as exc:
        raise LaunchError(
            "invalid_api_base", f"api base has an invalid port: {echo_input(raw)}", status=400
        ) from exc
    host = (split.hostname or "").lower().rstrip(".")
    if not host:
        raise LaunchError(
            "invalid_api_base", f"api base has an empty host: {echo_input(raw)}", status=400
        )
    return host, split.scheme


def _resolve_base(raw: object, host: str, resolve: Resolver) -> list[str]:
    """Resolve ``host`` to addresses, or raise ``api_base_unresolvable``.

    An IP literal resolves to itself; a name goes through the bounded resolver.
    Any lookup failure, timeout or empty result is ``api_base_unresolvable``.
    """
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None
    if ip_obj is not None:
        return [str(ip_obj)]

    # localhost / *.localhost resolve to loopback by name without touching DNS.
    if _is_private_host_name(host):
        return []

    try:
        addresses = resolve(host, _RESOLVE_TIMEOUT)
    except (OSError, ValueError, FutureTimeoutError) as exc:
        raise LaunchError(
            "api_base_unresolvable",
            f"could not resolve api base host: {echo_input(raw)}",
            status=400,
        ) from exc
    if not addresses:
        raise LaunchError(
            "api_base_unresolvable",
            f"could not resolve api base host: {echo_input(raw)}",
            status=400,
        )
    return list(addresses)


def _check_base_addresses(raw: object, host: str, scheme: str, addresses: list[str]) -> None:
    """Apply the metadata / http-is-local-only rules to resolved addresses."""
    for address in addresses:
        if _is_cloud_metadata(address):
            raise LaunchError(
                "api_base_blocked",
                f"api base resolves to a cloud metadata address: {echo_input(raw)}",
                status=400,
            )
    if scheme == "https":
        return
    # http:// requires every resolved address to be loopback/private, or a
    # private host name (localhost / *.localhost, which resolve to [] above).
    is_local_by_name = _is_private_host_name(host)
    all_private = bool(addresses) and all(_is_private_class(addr) for addr in addresses)
    if not (is_local_by_name or all_private):
        raise LaunchError(
            "api_base_requires_https",
            f"http is only allowed for loopback or private hosts: {echo_input(raw)}",
            status=400,
        )


def model_warnings(model: str, effective_api_base: str | None) -> list[str]:
    """Return advisory warning ids for ``model`` (DD-4, FR-21).

    Emits ``model_unfamiliar`` when the (already-validated) model is a bare
    name (no ``/``), does not match the OpenAI-style prefix heuristic, and no
    API base is effective. The warning is advisory only; the connection test is
    the authoritative check.
    """
    warnings: list[str] = []
    if "/" in model:
        return warnings
    if effective_api_base:
        return warnings
    if _OPENAI_MODEL_PREFIX.match(model):
        return warnings
    warnings.append(_MODEL_UNFAMILIAR)
    return warnings
