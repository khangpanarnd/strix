"""Tests for viewer target validation (FR-9..FR-15, BR-11..BR-17).

Every test injects a fake resolver and classifier, so no real DNS or network
I/O happens.
"""

from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import TYPE_CHECKING

import pytest

from strix.interface.viewer.launch_errors import LaunchError
from strix.interface.viewer.target_validation import ValidatedTarget, validate_target


if TYPE_CHECKING:
    from collections.abc import Callable


def _resolver(mapping: dict[str, list[str]]) -> Callable[[str, float], list[str]]:
    def resolve(host: str, _timeout: float) -> list[str]:
        if host not in mapping:
            raise OSError("no such host")
        return mapping[host]

    return resolve


def _classifier(result: str = "web_application") -> Callable[[str], str]:
    return lambda _target: result


def _validate(raw: str, **kwargs: object) -> ValidatedTarget:
    kwargs.setdefault("resolver", _resolver({"example.com": ["93.184.216.34"]}))
    kwargs.setdefault("classifier", _classifier())
    return validate_target(raw, **kwargs)  # type: ignore[arg-type]


# --- accepted forms ---------------------------------------------------------


def test_accepts_https_url() -> None:
    result = _validate("https://example.com/path")
    assert result.normalized == "https://example.com/path"
    assert result.host == "example.com"
    assert result.addresses == ("93.184.216.34",)


def test_accepts_bare_domain_normalizes_to_https() -> None:
    result = _validate("example.com")
    assert result.normalized == "https://example.com"
    assert result.host == "example.com"


def test_accepts_bare_domain_with_port() -> None:
    result = _validate("example.com:8443")
    assert result.normalized == "https://example.com:8443"


def test_accepts_public_ip_literal() -> None:
    result = _validate(
        "93.184.216.34",
        resolver=_resolver({}),
        classifier=_classifier("ip_address"),
    )
    assert result.normalized == "93.184.216.34"
    assert result.addresses == ("93.184.216.34",)


def test_accepts_bracketed_ipv6() -> None:
    result = _validate(
        "[2606:2800:220:1:248:1893:25c8:1946]",
        resolver=_resolver({}),
        classifier=_classifier("ip_address"),
    )
    assert result.host == "2606:2800:220:1:248:1893:25c8:1946"
    assert result.normalized == "2606:2800:220:1:248:1893:25c8:1946"


def test_trailing_dot_and_case_normalized_in_host() -> None:
    result = _validate(
        "EXAMPLE.com.",
        resolver=_resolver({"example.com": ["93.184.216.34"]}),
    )
    assert result.host == "example.com"


# --- FR-10 syntax/form rejections -------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://example.com",
        "https://user:pass@example.com",
        "https://example.com#frag",
        "git@github.com:org/repo.git",
        "git://host/repo",
        "https://host/org/repo.git",
        "example.com/path",
        "-rf",
        "postman://abc",
        "./local/path",
    ],
)
def test_rejects_invalid_forms(raw: str) -> None:
    with pytest.raises(LaunchError) as exc:
        _validate(raw, classifier=_classifier("web_application"))
    assert exc.value.code in ("invalid_target",)
    assert exc.value.status == 400


def test_rejects_non_string() -> None:
    with pytest.raises(LaunchError) as exc:
        validate_target(123, resolver=_resolver({}), classifier=_classifier())
    assert exc.value.code == "invalid_target"


def test_rejects_non_web_classification() -> None:
    with pytest.raises(LaunchError) as exc:
        _validate(
            "example.com",
            classifier=_classifier("repository"),
        )
    assert exc.value.code == "invalid_target"
    assert "repository or local path" in exc.value.message


# --- private / metadata -----------------------------------------------------


def test_blocks_private_address() -> None:
    with pytest.raises(LaunchError) as exc:
        _validate(
            "internal.example.com",
            resolver=_resolver({"internal.example.com": ["10.0.0.5"]}),
        )
    assert exc.value.code == "target_blocked_private"


def test_allows_private_address_with_flag() -> None:
    result = _validate(
        "internal.example.com",
        allow_private=True,
        resolver=_resolver({"internal.example.com": ["10.0.0.5"]}),
    )
    assert result.addresses == ("10.0.0.5",)


def test_localhost_blocked_by_name_even_unresolved() -> None:
    with pytest.raises(LaunchError) as exc:
        _validate("localhost", resolver=_resolver({"localhost": ["93.184.216.34"]}))
    assert exc.value.code == "target_blocked_private"


def test_metadata_host_name_blocked_before_dns() -> None:
    calls: list[str] = []

    def resolver(host: str, _timeout: float) -> list[str]:
        calls.append(host)
        return ["93.184.216.34"]

    with pytest.raises(LaunchError) as exc:
        validate_target("metadata.google.internal", resolver=resolver, classifier=_classifier())
    assert exc.value.code == "target_blocked_metadata"
    assert calls == []  # never resolved


def test_metadata_address_blocked_even_with_allow_private() -> None:
    with pytest.raises(LaunchError) as exc:
        _validate(
            "sneaky.example.com",
            allow_private=True,
            resolver=_resolver({"sneaky.example.com": ["169.254.169.254"]}),
        )
    assert exc.value.code == "target_blocked_metadata"


def test_one_bad_address_among_several_rejects_whole() -> None:
    with pytest.raises(LaunchError) as exc:
        _validate(
            "mixed.example.com",
            resolver=_resolver({"mixed.example.com": ["93.184.216.34", "10.0.0.1"]}),
        )
    assert exc.value.code == "target_blocked_private"


# --- resolution failures ----------------------------------------------------


def test_unresolvable_host() -> None:
    with pytest.raises(LaunchError) as exc:
        _validate("nope.example.com", resolver=_resolver({}))
    assert exc.value.code == "target_unresolvable"


def test_timeout_is_unresolvable() -> None:
    def resolver(_host: str, _timeout: float) -> list[str]:
        raise FutureTimeoutError

    with pytest.raises(LaunchError) as exc:
        validate_target("slow.example.com", resolver=resolver, classifier=_classifier())
    assert exc.value.code == "target_unresolvable"


def test_empty_resolution_is_unresolvable() -> None:
    with pytest.raises(LaunchError) as exc:
        _validate("empty.example.com", resolver=_resolver({"empty.example.com": []}))
    assert exc.value.code == "target_unresolvable"


# --- message safety ---------------------------------------------------------


def test_message_echo_is_bounded_and_sanitized() -> None:
    hostile = "ftp://" + "a" * 200 + "\x1b[31m"
    with pytest.raises(LaunchError) as exc:
        _validate(hostile, classifier=_classifier())
    # No control chars echoed, and the echo is bounded.
    assert "\x1b" not in exc.value.message
    assert len([c for c in exc.value.message if c == "a"]) <= 80


# --- address classification (IP literals, every class named in BR-13/BR-14) --

_PRIVATE_LITERALS = [
    "http://127.0.0.1/",
    "http://127.1.2.3:8080/",
    "http://10.1.2.3/",
    "http://172.16.0.1/",
    "http://172.31.255.255/",
    "http://192.168.1.1/",
    "http://169.254.1.1/",
    "http://0.0.0.0/",
    "http://224.0.0.1/",
    "http://[::1]/",
    "http://[fc00::1]/",
    "http://[fd12:3456::1]/",
    "http://[fe80::1]/",
    "http://[::ffff:10.0.0.1]/",
    "http://[::ffff:127.0.0.1]/",
]

_METADATA_LITERALS = [
    "http://169.254.169.254/",
    "http://169.254.169.254/latest/meta-data/",
    "http://169.254.170.2/",
    "http://100.100.100.200/",
    "http://[fd00:ec2::254]/",
    "http://[::ffff:169.254.169.254]/",
]


@pytest.mark.parametrize("target", _PRIVATE_LITERALS)
def test_private_class_ip_literals_blocked_without_flag(target: str) -> None:
    with pytest.raises(LaunchError) as exc:
        _validate(target, resolver=_resolver({}))
    assert exc.value.code == "target_blocked_private"


@pytest.mark.parametrize("target", _PRIVATE_LITERALS)
def test_private_class_ip_literals_allowed_with_flag(target: str) -> None:
    result = _validate(target, allow_private=True, resolver=_resolver({}))
    assert result.normalized == target


@pytest.mark.parametrize("target", _METADATA_LITERALS)
@pytest.mark.parametrize("allow_private", [False, True])
def test_metadata_ip_literals_always_blocked(target: str, allow_private: bool) -> None:
    with pytest.raises(LaunchError) as exc:
        _validate(target, allow_private=allow_private, resolver=_resolver({}))
    assert exc.value.code == "target_blocked_metadata"


@pytest.mark.parametrize(
    "name", ["metadata", "instance-data", "metadata.azure.com", "METADATA.GOOGLE.INTERNAL."]
)
def test_metadata_host_names_blocked_even_with_allow_private(name: str) -> None:
    with pytest.raises(LaunchError) as exc:
        _validate(name, allow_private=True, resolver=_resolver({}))
    assert exc.value.code == "target_blocked_metadata"


@pytest.mark.parametrize(
    "target",
    [
        "http://a@127.0.0.1/",
        "http://127.0.0.1@evil.example.com/",
        "https://user:pass@example.com/",
        "https://example.com/#frag",
        "-evil.example.com",
        "git@github.com:org/repo.git",
        "git://example.com/org/repo",
        "postman://abc123",
        "file:///etc/passwd",
        "ftp://example.com/",
        "https://example.com/a b",
        "https://exam\u00e9ple.com/",
    ],
)
def test_hostile_forms_rejected(target: str) -> None:
    with pytest.raises(LaunchError) as exc:
        _validate(target, resolver=_resolver({}))
    assert exc.value.code in {"invalid_target", "target_unresolvable"}
