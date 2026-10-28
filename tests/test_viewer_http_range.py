"""Tests for the HTTP Range header parser (RFC 9110 single byte ranges)."""

from __future__ import annotations

import pytest

from strix.interface.viewer.http_range import UNSATISFIABLE, parse_byte_range


SIZE = 1000


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("bytes=0-499", (0, 499)),
        ("bytes=500-999", (500, 999)),
        ("bytes=500-", (500, 999)),
        ("bytes=0-", (0, 999)),
        ("bytes=-500", (500, 999)),  # the last 500 bytes
        ("bytes=-1", (999, 999)),
        ("bytes=0-0", (0, 0)),
        ("bytes=999-999", (999, 999)),
        ("bytes=0-99999", (0, 999)),  # end past EOF is clamped
        ("bytes=-99999", (0, 999)),  # a suffix longer than the file is the whole file
        ("  bytes=10-20  ", (10, 20)),
    ],
)
def test_valid_ranges(header: str, expected: tuple[int, int]) -> None:
    assert parse_byte_range(header, SIZE) == expected


@pytest.mark.parametrize(
    "header",
    [
        "bytes=1000-",  # starts at EOF
        "bytes=1000-1500",
        "bytes=5000-6000",
        "bytes=-0",  # a zero-length suffix
    ],
)
def test_ranges_outside_the_file_are_unsatisfiable(header: str) -> None:
    assert parse_byte_range(header, SIZE) == UNSATISFIABLE


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "bytes=",
        "bytes=-",
        "bytes=abc-def",
        "bytes=1-2-3",
        "bytes=0-99,200-299",  # several ranges: whole file instead
        "items=0-10",  # not bytes
        "Bytes=0-10",  # the unit is case-sensitive in this implementation
        "bytes 0-10",
        "bytes=5-2",  # last < first is ignored per the RFC
        "bytes=-5-",
        "bytes=0x10-0x20",
        "bytes=\uff11-\uff12",  # fullwidth digits are not ASCII digits
    ],
)
def test_unusable_headers_mean_send_everything(header: str | None) -> None:
    assert parse_byte_range(header, SIZE) is None


def test_an_empty_file_has_no_satisfiable_range() -> None:
    assert parse_byte_range("bytes=0-", 0) == UNSATISFIABLE
    assert parse_byte_range("bytes=-5", 0) == UNSATISFIABLE
    assert parse_byte_range("bytes=0-0", 0) == UNSATISFIABLE


def test_huge_numbers_do_not_overflow_or_crash() -> None:
    assert parse_byte_range("bytes=0-" + "9" * 40, SIZE) == (0, 999)
    assert parse_byte_range("bytes=" + "9" * 40 + "-", SIZE) == UNSATISFIABLE
    assert parse_byte_range("bytes=-" + "9" * 40, SIZE) == (0, 999)
