"""Tests for the LLM settings field validators (FR-21..FR-25, BR-6..BR-11, BR-17).

Every api_base test injects a fake resolver so no real DNS or network I/O
happens. No test contains a real key; the one key-echo test asserts a
recognisable token never appears in any error message.
"""

from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import TYPE_CHECKING

import pytest

from strix.interface.viewer.launch_errors import LaunchError
from strix.interface.viewer.llm_settings_validation import (
    model_warnings,
    validate_api_base,
    validate_api_key,
    validate_model,
    validate_reasoning_effort,
)


if TYPE_CHECKING:
    from collections.abc import Callable


def _resolver(mapping: dict[str, list[str]]) -> Callable[[str, float], list[str]]:
    def resolve(host: str, _timeout: float) -> list[str]:
        if host not in mapping:
            raise OSError("no such host")
        return mapping[host]

    return resolve


# --- validate_model (BR-6, FR-21) -------------------------------------------


def test_model_accepts_and_trims() -> None:
    assert validate_model("  anthropic/claude-sonnet-4-5  ") == "anthropic/claude-sonnet-4-5"


@pytest.mark.parametrize("raw", ["", "   ", "a" * 201])
def test_model_length_rejected(raw: str) -> None:
    with pytest.raises(LaunchError) as exc:
        validate_model(raw)
    assert exc.value.code == "invalid_model"
    assert exc.value.status == 400


@pytest.mark.parametrize("raw", ["gpt 5", "model\twith-tab", "bad\x00null", "line\nbreak"])
def test_model_whitespace_and_control_rejected(raw: str) -> None:
    with pytest.raises(LaunchError) as exc:
        validate_model(raw)
    assert exc.value.code == "invalid_model"


def test_model_non_string_rejected() -> None:
    with pytest.raises(LaunchError) as exc:
        validate_model(123)
    assert exc.value.code == "invalid_model"


# --- validate_reasoning_effort (BR-7, FR-22) --------------------------------


@pytest.mark.parametrize(
    "raw", ["none", "minimal", "low", "medium", "high", "xhigh", "max", "HIGH", " High "]
)
def test_reasoning_effort_accepted_case_insensitive(raw: str) -> None:
    assert validate_reasoning_effort(raw) == raw.strip().lower()


@pytest.mark.parametrize("raw", ["", "extreme", "higher", "0"])
def test_reasoning_effort_rejected(raw: str) -> None:
    with pytest.raises(LaunchError) as exc:
        validate_reasoning_effort(raw)
    assert exc.value.code == "invalid_reasoning_effort"


def test_reasoning_effort_non_string_rejected() -> None:
    with pytest.raises(LaunchError) as exc:
        validate_reasoning_effort(5)
    assert exc.value.code == "invalid_reasoning_effort"


# --- validate_api_key (BR-8, FR-23, BR-12) ----------------------------------


def test_api_key_accepts_and_trims() -> None:
    assert validate_api_key("  sk-abc123  ") == "sk-abc123"


@pytest.mark.parametrize("raw", ["", "   "])
def test_api_key_empty_rejected(raw: str) -> None:
    with pytest.raises(LaunchError) as exc:
        validate_api_key(raw)
    assert exc.value.code == "invalid_api_key"


def test_api_key_too_long_rejected() -> None:
    with pytest.raises(LaunchError) as exc:
        validate_api_key("a" * 4097)
    assert exc.value.code == "invalid_api_key"


def test_api_key_control_char_rejected() -> None:
    with pytest.raises(LaunchError) as exc:
        validate_api_key("sk-\x01bad")
    assert exc.value.code == "invalid_api_key"


def test_api_key_non_ascii_rejected_header_safe() -> None:
    with pytest.raises(LaunchError) as exc:
        validate_api_key("sk-caf\u00e9")
    assert exc.value.code == "invalid_api_key"
    assert "HTTP header" in exc.value.message


def test_api_key_error_never_echoes_key() -> None:
    recognisable = "sk-SUPER-SECRET-KEY-\u00e9"
    with pytest.raises(LaunchError) as exc:
        validate_api_key(recognisable)
    # The raw value, even partially, must never appear in the message (BR-12).
    assert "SUPER-SECRET-KEY" not in exc.value.message
    assert "sk-" not in exc.value.message


def test_api_key_length_error_does_not_leak_length() -> None:
    # A too-long key must not have its (distinctive) submitted length echoed.
    submitted = "a" * 5003
    with pytest.raises(LaunchError) as exc:
        validate_api_key(submitted)
    assert "5003" not in exc.value.message
    assert "aaaa" not in exc.value.message


# --- validate_api_base (BR-9, FR-24) ----------------------------------------


def _base(raw: str, mapping: dict[str, list[str]] | None = None) -> str:
    return validate_api_base(raw, resolver=_resolver(mapping or {}))


def test_api_base_accepts_https_public() -> None:
    assert (
        validate_api_base(
            "https://api.example.com/v1",
            resolver=_resolver({"api.example.com": ["93.184.216.34"]}),
        )
        == "https://api.example.com/v1"
    )


def test_api_base_accepts_http_loopback_literal() -> None:
    assert _base("http://127.0.0.1:11434") == "http://127.0.0.1:11434"


def test_api_base_accepts_http_private_literal() -> None:
    assert _base("http://192.168.1.20:8000") == "http://192.168.1.20:8000"


def test_api_base_accepts_http_localhost_name_without_dns() -> None:
    # localhost resolves to loopback by name; the resolver is never called.
    assert _base("http://localhost:11434") == "http://localhost:11434"


def test_api_base_accepts_http_private_resolved_name() -> None:
    assert (
        validate_api_base(
            "http://model.lan:8000",
            resolver=_resolver({"model.lan": ["10.0.0.5"]}),
        )
        == "http://model.lan:8000"
    )


def test_api_base_http_public_requires_https() -> None:
    with pytest.raises(LaunchError) as exc:
        validate_api_base(
            "http://api.example.com",
            resolver=_resolver({"api.example.com": ["93.184.216.34"]}),
        )
    assert exc.value.code == "api_base_requires_https"


def test_api_base_https_public_accepted() -> None:
    assert (
        validate_api_base(
            "https://api.example.com",
            resolver=_resolver({"api.example.com": ["93.184.216.34"]}),
        )
        == "https://api.example.com"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "https://169.254.169.254",
        "http://169.254.169.254/latest/meta-data/",
        "https://[fd00:ec2::254]",
        "https://metadata.google.internal",
        "http://metadata",
    ],
)
def test_api_base_metadata_always_blocked(raw: str) -> None:
    with pytest.raises(LaunchError) as exc:
        _base(raw)
    assert exc.value.code == "api_base_blocked"


def test_api_base_metadata_by_resolved_address_blocked() -> None:
    with pytest.raises(LaunchError) as exc:
        validate_api_base(
            "https://sneaky.example.com",
            resolver=_resolver({"sneaky.example.com": ["169.254.169.254"]}),
        )
    assert exc.value.code == "api_base_blocked"


def test_api_base_unresolvable() -> None:
    with pytest.raises(LaunchError) as exc:
        _base("https://nope.example.com")
    assert exc.value.code == "api_base_unresolvable"


def test_api_base_dns_timeout_is_unresolvable() -> None:
    def resolver(_host: str, _timeout: float) -> list[str]:
        raise FutureTimeoutError

    with pytest.raises(LaunchError) as exc:
        validate_api_base("https://slow.example.com", resolver=resolver)
    assert exc.value.code == "api_base_unresolvable"


def test_api_base_empty_resolution_is_unresolvable() -> None:
    with pytest.raises(LaunchError) as exc:
        validate_api_base(
            "https://empty.example.com", resolver=_resolver({"empty.example.com": []})
        )
    assert exc.value.code == "api_base_unresolvable"


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://example.com",
        "https://user:pass@example.com",
        "https://example.com#frag",
        "http://user@127.0.0.1",
        "not a url",
        "https://",
        "",
        "   ",
    ],
)
def test_api_base_invalid_syntax(raw: str) -> None:
    with pytest.raises(LaunchError) as exc:
        _base(raw)
    assert exc.value.code in {"invalid_api_base", "api_base_unresolvable"}


def test_api_base_too_long_rejected() -> None:
    with pytest.raises(LaunchError) as exc:
        _base("https://example.com/" + "a" * 2048)
    assert exc.value.code == "invalid_api_base"


def test_api_base_non_string_rejected() -> None:
    with pytest.raises(LaunchError) as exc:
        validate_api_base(123)
    assert exc.value.code == "invalid_api_base"


def test_api_base_mapped_ipv6_metadata_blocked() -> None:
    with pytest.raises(LaunchError) as exc:
        _base("http://[::ffff:169.254.169.254]")
    assert exc.value.code == "api_base_blocked"


def test_api_base_one_public_address_among_private_requires_https() -> None:
    # http:// needs EVERY resolved address loopback/private; a public one fails.
    with pytest.raises(LaunchError) as exc:
        validate_api_base(
            "http://mixed.example.com",
            resolver=_resolver({"mixed.example.com": ["10.0.0.1", "93.184.216.34"]}),
        )
    assert exc.value.code == "api_base_requires_https"


# --- model_warnings (DD-4, FR-21) -------------------------------------------


@pytest.mark.parametrize("model", ["gpt-5", "chatgpt-4o", "o3-mini", "text-embedding", "ft:gpt"])
def test_model_warnings_openai_prefix_no_warning(model: str) -> None:
    assert model_warnings(model, None) == []


def test_model_warnings_provider_slash_no_warning() -> None:
    assert model_warnings("anthropic/claude-sonnet-4-5", None) == []


def test_model_warnings_with_base_no_warning() -> None:
    assert model_warnings("my-local-model", "http://localhost:11434") == []


def test_model_warnings_bare_unfamiliar_warns() -> None:
    assert model_warnings("some-random-model", None) == ["model_unfamiliar"]
