"""Tests for viewer launch-request parsing (BR-6..BR-10, FR-5..FR-7, NFR-3)."""

from __future__ import annotations

import pytest

from strix.interface.viewer.launch_errors import LaunchError
from strix.interface.viewer.launch_request import (
    LaunchLimits,
    LaunchRequest,
    parse_launch_request,
)


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"target": "example.com", "authorized": True}
    body.update(overrides)
    return body


LIMITS = LaunchLimits()


def test_parses_defaults() -> None:
    result = parse_launch_request(_body(), LIMITS)
    assert isinstance(result, LaunchRequest)
    assert result.target == "example.com"
    assert result.scan_mode == "quick"
    assert result.max_budget_usd == LIMITS.default_budget_usd
    assert result.instruction is None
    assert result.authorized is True
    assert result.allow_private is False


# --- authorized -------------------------------------------------------------


@pytest.mark.parametrize("value", ["true", 1, False, None])
def test_authorized_must_be_json_true(value: object) -> None:
    with pytest.raises(LaunchError) as exc:
        parse_launch_request(_body(authorized=value), LIMITS)
    assert exc.value.code == "authorization_required"


def test_authorized_absent_rejected() -> None:
    with pytest.raises(LaunchError) as exc:
        parse_launch_request({"target": "example.com"}, LIMITS)
    assert exc.value.code == "authorization_required"


# --- scan_mode --------------------------------------------------------------


@pytest.mark.parametrize("mode", ["quick", "standard", "deep"])
def test_valid_scan_modes(mode: str) -> None:
    assert parse_launch_request(_body(scan_mode=mode), LIMITS).scan_mode == mode


def test_invalid_scan_mode() -> None:
    with pytest.raises(LaunchError) as exc:
        parse_launch_request(_body(scan_mode="turbo"), LIMITS)
    assert exc.value.code == "invalid_scan_mode"


# --- budget -----------------------------------------------------------------


def test_budget_within_range() -> None:
    assert parse_launch_request(_body(max_budget_usd=12.5), LIMITS).max_budget_usd == 12.5


@pytest.mark.parametrize("value", ["5", True, None])
def test_budget_must_be_number_not_bool_or_string(value: object) -> None:
    with pytest.raises(LaunchError) as exc:
        parse_launch_request(_body(max_budget_usd=value), LIMITS)
    assert exc.value.code == "invalid_budget"


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_budget_out_of_range(value: float) -> None:
    with pytest.raises(LaunchError) as exc:
        parse_launch_request(_body(max_budget_usd=value), LIMITS)
    assert exc.value.code == "invalid_budget"


def test_budget_exceeds_max() -> None:
    with pytest.raises(LaunchError) as exc:
        parse_launch_request(_body(max_budget_usd=100.0), LIMITS)
    assert exc.value.code == "budget_exceeds_max"


def test_limits_from_env_override() -> None:
    limits = LaunchLimits.from_env({"STRIX_VIEWER_MAX_BUDGET_USD": "200"})
    assert limits.max_budget_usd == 200.0
    assert parse_launch_request(_body(max_budget_usd=150.0), limits).max_budget_usd == 150.0


@pytest.mark.parametrize("raw", ["not-a-number", "0", "-5", ""])
def test_limits_from_env_invalid_falls_back_to_50(raw: str) -> None:
    limits = LaunchLimits.from_env({"STRIX_VIEWER_MAX_BUDGET_USD": raw})
    assert limits.max_budget_usd == 50.0


def test_limits_from_env_absent_is_default() -> None:
    assert LaunchLimits.from_env({}).max_budget_usd == 50.0


# --- instruction ------------------------------------------------------------


def test_instruction_within_limit() -> None:
    assert parse_launch_request(_body(instruction="focus on auth"), LIMITS).instruction == (
        "focus on auth"
    )


def test_instruction_too_long() -> None:
    with pytest.raises(LaunchError) as exc:
        parse_launch_request(_body(instruction="x" * 4001), LIMITS)
    assert exc.value.code == "invalid_instruction"


def test_instruction_control_char_rejected() -> None:
    with pytest.raises(LaunchError) as exc:
        parse_launch_request(_body(instruction="bad\x00null"), LIMITS)
    assert exc.value.code == "invalid_instruction"


def test_instruction_allows_newline_and_tab() -> None:
    assert parse_launch_request(_body(instruction="a\nb\tc"), LIMITS).instruction == "a\nb\tc"


def test_instruction_whitespace_only_is_none() -> None:
    assert parse_launch_request(_body(instruction="   \n\t "), LIMITS).instruction is None


def test_instruction_wrong_type() -> None:
    with pytest.raises(LaunchError) as exc:
        parse_launch_request(_body(instruction=123), LIMITS)
    assert exc.value.code == "invalid_instruction"


# --- allow_private / target / unknown ---------------------------------------


def test_allow_private_must_be_bool() -> None:
    with pytest.raises(LaunchError) as exc:
        parse_launch_request(_body(allow_private="yes"), LIMITS)
    assert exc.value.code == "invalid_json"


def test_allow_private_true() -> None:
    assert parse_launch_request(_body(allow_private=True), LIMITS).allow_private is True


def test_unknown_field_rejected() -> None:
    with pytest.raises(LaunchError) as exc:
        parse_launch_request(_body(evil="x"), LIMITS)
    assert exc.value.code == "unknown_field"


def test_non_object_body_rejected() -> None:
    with pytest.raises(LaunchError) as exc:
        parse_launch_request([1, 2, 3], LIMITS)
    assert exc.value.code == "invalid_json"


def test_target_must_be_string() -> None:
    with pytest.raises(LaunchError) as exc:
        parse_launch_request({"target": 123, "authorized": True}, LIMITS)
    assert exc.value.code == "invalid_target"


def test_error_payload_shape() -> None:
    try:
        parse_launch_request(_body(scan_mode="turbo"), LIMITS)
    except LaunchError as exc:
        payload = exc.to_payload()
        assert payload["error"] == "invalid_scan_mode"
        assert "message" in payload
