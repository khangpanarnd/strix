"""Parse and type-check the launch request body (BR-6..BR-10, FR-5..FR-7).

The parser is pure: it validates a decoded JSON body against the launch limits
and returns a typed :class:`LaunchRequest`, or raises :class:`LaunchError`. It
never resolves DNS or touches the target's address — that is the validator's
job (``target_validation``).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from strix.interface.viewer.launch_errors import LaunchError


if TYPE_CHECKING:
    from collections.abc import Mapping


_DEFAULT_MAX_BUDGET_USD = 50.0
_DEFAULT_BUDGET_USD = 5.0
_INSTRUCTION_MAX = 4000
_MODES = ("quick", "standard", "deep")

_ALLOWED_FIELDS = frozenset(
    {"target", "scan_mode", "max_budget_usd", "instruction", "authorized", "allow_private"}
)

# Control characters allowed in an instruction (newline and tab); every other
# C0/C1 control character is rejected.
_ALLOWED_CONTROL = {"\n", "\t"}


@dataclass(frozen=True)
class LaunchLimits:
    """The configurable bounds the parser enforces on a launch request."""

    max_budget_usd: float = _DEFAULT_MAX_BUDGET_USD
    default_budget_usd: float = _DEFAULT_BUDGET_USD
    instruction_max: int = _INSTRUCTION_MAX
    modes: tuple[str, ...] = _MODES

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> LaunchLimits:
        """Build limits, letting ``STRIX_VIEWER_MAX_BUDGET_USD`` raise/lower the cap.

        An invalid or non-positive override falls back to the default maximum so
        a bad environment value can never remove the budget ceiling (BR-8).
        """
        env = os.environ if environ is None else environ
        max_budget = _DEFAULT_MAX_BUDGET_USD
        raw = env.get("STRIX_VIEWER_MAX_BUDGET_USD")
        if raw is not None:
            try:
                parsed = float(raw)
            except (TypeError, ValueError):
                parsed = 0.0
            if math.isfinite(parsed) and parsed > 0:
                max_budget = parsed
        return cls(max_budget_usd=max_budget)


@dataclass(frozen=True)
class LaunchRequest:
    """A validated launch request, ready to drive target validation and spawn."""

    target: str
    scan_mode: str
    max_budget_usd: float
    instruction: str | None
    authorized: bool
    allow_private: bool


def _parse_authorized(body: Mapping[str, object]) -> bool:
    # Must be the JSON boolean true; "true", 1 and absent are all rejected (BR-6).
    if body.get("authorized") is not True:
        raise LaunchError(
            "authorization_required",
            "you must confirm you are authorized to test this target",
            status=400,
        )
    return True


def _parse_scan_mode(body: Mapping[str, object], limits: LaunchLimits) -> str:
    mode = body.get("scan_mode", "quick")
    if mode not in limits.modes:
        raise LaunchError(
            "invalid_scan_mode",
            f"scan_mode must be one of {', '.join(limits.modes)}",
            status=400,
        )
    return str(mode)


def _parse_budget(body: Mapping[str, object], limits: LaunchLimits) -> float:
    if "max_budget_usd" not in body:
        return limits.default_budget_usd
    value = body["max_budget_usd"]
    # bool is a subtype of int in Python; reject it so true/false is not a budget.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LaunchError("invalid_budget", "max_budget_usd must be a number", status=400)
    budget = float(value)
    if not math.isfinite(budget) or budget <= 0:
        raise LaunchError(
            "invalid_budget", "max_budget_usd must be a finite number above 0", status=400
        )
    if budget > limits.max_budget_usd:
        raise LaunchError(
            "budget_exceeds_max",
            f"max_budget_usd must not exceed {limits.max_budget_usd:g}",
            status=400,
        )
    return budget


def _parse_instruction(body: Mapping[str, object], limits: LaunchLimits) -> str | None:
    if "instruction" not in body or body["instruction"] is None:
        return None
    value = body["instruction"]
    if not isinstance(value, str):
        raise LaunchError("invalid_instruction", "instruction must be a string", status=400)
    if len(value) > limits.instruction_max:
        raise LaunchError(
            "invalid_instruction",
            f"instruction must be at most {limits.instruction_max} characters",
            status=400,
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LaunchError(
            "invalid_instruction", "instruction must be valid UTF-8", status=400
        ) from exc
    for char in value:
        if char in _ALLOWED_CONTROL:
            continue
        if ord(char) < 0x20 or ord(char) == 0x7F:
            raise LaunchError(
                "invalid_instruction",
                "instruction must not contain control characters",
                status=400,
            )
    # An all-whitespace instruction carries no intent, so treat it as absent.
    return value if value.strip() else None


def _parse_allow_private(body: Mapping[str, object]) -> bool:
    value = body.get("allow_private", False)
    if not isinstance(value, bool):
        raise LaunchError("invalid_json", "allow_private must be a boolean", status=400)
    return value


def parse_launch_request(body: object, limits: LaunchLimits) -> LaunchRequest:
    """Validate a decoded JSON launch body against ``limits``.

    Raises :class:`LaunchError` (always HTTP 400 here) on any violation; returns
    a typed :class:`LaunchRequest` on success. Does not resolve DNS.
    """
    if not isinstance(body, dict):
        raise LaunchError("invalid_json", "request body must be a JSON object", status=400)

    unknown = set(body) - _ALLOWED_FIELDS
    if unknown:
        raise LaunchError(
            "unknown_field",
            f"unknown field(s): {', '.join(sorted(unknown))}",
            status=400,
        )

    target = body.get("target")
    if not isinstance(target, str):
        raise LaunchError("invalid_target", "target must be a string", status=400)

    authorized = _parse_authorized(body)
    scan_mode = _parse_scan_mode(body, limits)
    max_budget_usd = _parse_budget(body, limits)
    instruction = _parse_instruction(body, limits)
    allow_private = _parse_allow_private(body)

    return LaunchRequest(
        target=target,
        scan_mode=scan_mode,
        max_budget_usd=max_budget_usd,
        instruction=instruction,
        authorized=authorized,
        allow_private=allow_private,
    )
