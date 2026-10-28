"""Tests for the incremental ``strix.log`` reader behind the viewer's server log."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from strix.interface.viewer import run_log
from strix.interface.viewer.redaction import make_redactor, redact
from strix.interface.viewer.run_log import (
    LOG_FILE_NAME,
    InvalidCursorError,
    parse_cursor,
    parse_line,
    read_log_chunk,
)
from strix.telemetry.logging import set_agent_id, set_scan_id, setup_scan_logging


if TYPE_CHECKING:
    from pathlib import Path


def _line(
    msg: str, *, level: str = "INFO", agent: str = "-", logger: str = "strix.core.runner"
) -> str:
    return f"2026-10-26 13:20:13.123 {level:<7} scan-1 {agent} {logger}: {msg}"


def _write(run_dir: Path, text: str, *, mode: str = "w") -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / LOG_FILE_NAME
    with path.open(mode, encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


def _messages(chunk: dict) -> list[str]:
    return [e["message"] for e in chunk["entries"]]


# --- parsing -------------------------------------------------------------------


def test_parse_line_extracts_every_field() -> None:
    entry = parse_line(
        _line("hello world", level="WARNING", agent="agent-7", logger="strix.tools.x")
    )
    assert entry["level"] == "WARNING"
    assert entry["agent"] == "agent-7"
    assert entry["logger"] == "strix.tools.x"
    assert entry["message"] == "hello world"
    assert "continuation" not in entry


def test_parse_line_no_agent_is_none() -> None:
    assert parse_line(_line("x"))["agent"] is None


def test_parse_line_keeps_colons_in_the_message() -> None:
    assert parse_line(_line("url=http://a:80/b: done"))["message"] == "url=http://a:80/b: done"


def test_parse_line_empty_message() -> None:
    entry = parse_line("2026-10-26 13:20:13.123 INFO    scan-1 - strix.core.runner:")
    assert entry["message"] == ""
    assert entry["level"] == "INFO"


@pytest.mark.parametrize(
    "line",
    [
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
        "ValueError: boom",
        "",
    ],
)
def test_unparseable_lines_are_continuations(line: str) -> None:
    entry = parse_line(line)
    assert entry["continuation"] is True
    assert entry["message"] == line


def test_timestamp_is_returned_as_utc() -> None:
    # The scan stamps local time; the reader must convert it, whatever this machine's zone is.
    entry = parse_line(_line("x"))
    expected = (
        datetime(2026, 10, 26, 13, 20, 13, 123000)
        .astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    assert entry["ts"] == expected
    assert entry["ts"].endswith("Z")


def test_impossible_date_gives_no_timestamp() -> None:
    entry = parse_line("2026-13-45 99:99:99.000 INFO    s - strix: x")
    assert entry["ts"] is None
    assert entry["message"] == "x"


def test_long_messages_are_clipped() -> None:
    entry = parse_line(_line("a" * (run_log.MAX_MESSAGE_CHARS + 500)))
    assert len(entry["message"]) < run_log.MAX_MESSAGE_CHARS + 50
    assert entry["message"].endswith("[truncated]")


# --- cursor --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, None), ("", None), ("0", 0), ("12345", 12345)]
)
def test_parse_cursor_valid(raw: str | None, expected: int | None) -> None:
    assert parse_cursor(raw) == expected


@pytest.mark.parametrize("raw", ["-1", "abc", "1.5", "1e3", " 5x", "0x10"])
def test_parse_cursor_invalid(raw: str) -> None:
    with pytest.raises(InvalidCursorError):
        parse_cursor(raw)


# --- reading -------------------------------------------------------------------


def test_missing_file_reports_not_exists(tmp_path: Path) -> None:
    chunk = read_log_chunk(tmp_path, None)
    assert chunk["exists"] is False
    assert chunk["entries"] == []
    assert chunk["offset"] == 0


def test_run_dir_that_does_not_exist(tmp_path: Path) -> None:
    assert read_log_chunk(tmp_path / "nope", None)["exists"] is False


def test_tail_read_returns_everything_in_a_small_file(tmp_path: Path) -> None:
    text = "".join(_line(f"m{i}") + "\n" for i in range(5))
    _write(tmp_path, text)

    chunk = read_log_chunk(tmp_path, None)

    assert _messages(chunk) == ["m0", "m1", "m2", "m3", "m4"]
    assert chunk["offset"] == len(text.encode())
    assert chunk["size"] == len(text.encode())
    assert chunk["more"] is False
    assert chunk["truncated_head"] is False
    assert chunk["reset"] is False
    assert chunk["updated_at"].endswith("Z")
    assert chunk["now"].endswith("Z")


def test_cursor_returns_only_what_is_new(tmp_path: Path) -> None:
    _write(tmp_path, _line("one") + "\n")
    first = read_log_chunk(tmp_path, None)

    _write(tmp_path, _line("two") + "\n" + _line("three") + "\n", mode="a")
    second = read_log_chunk(tmp_path, first["offset"])

    assert _messages(first) == ["one"]
    assert _messages(second) == ["two", "three"]


def test_nothing_new_returns_no_entries_and_the_same_offset(tmp_path: Path) -> None:
    _write(tmp_path, _line("one") + "\n")
    first = read_log_chunk(tmp_path, None)

    again = read_log_chunk(tmp_path, first["offset"])

    assert again["exists"] is True
    assert again["entries"] == []
    assert again["offset"] == first["offset"]
    assert again["updated_at"] is not None


def test_a_partial_last_line_is_held_back_then_delivered_whole(tmp_path: Path) -> None:
    done = _line("complete") + "\n"
    half = _line("half written")
    _write(tmp_path, done + half[:30])

    first = read_log_chunk(tmp_path, None)
    assert _messages(first) == ["complete"]
    assert first["offset"] == len(done.encode())

    _write(tmp_path, half[30:] + "\n", mode="a")
    second = read_log_chunk(tmp_path, first["offset"])
    assert _messages(second) == ["half written"]


def test_only_a_partial_line_returns_nothing_and_keeps_the_cursor(tmp_path: Path) -> None:
    _write(tmp_path, _line("typing")[:20])
    chunk = read_log_chunk(tmp_path, 0)
    assert chunk["entries"] == []
    assert chunk["offset"] == 0
    assert chunk["more"] is False


def test_following_a_growing_log_never_skips_or_repeats_a_line(tmp_path: Path) -> None:
    # Writes of awkward sizes, split at arbitrary byte positions, polled between each.
    payload = "".join(_line(f"line-{i:03d} " + "é" * (i % 7)) + "\n" for i in range(120)).encode()
    run_dir = tmp_path
    run_dir.mkdir(exist_ok=True)
    path = run_dir / LOG_FILE_NAME
    path.write_bytes(b"")

    seen: list[str] = []
    cursor: int | None = None
    position = 0
    step = 37  # deliberately not aligned to lines or to the 2-byte characters
    while position < len(payload):
        with path.open("ab") as handle:
            handle.write(payload[position : position + step])
        position += step
        chunk = read_log_chunk(run_dir, cursor)
        seen += _messages(chunk)
        cursor = chunk["offset"]
    seen += _messages(read_log_chunk(run_dir, cursor))

    assert seen == [f"line-{i:03d} " + "é" * (i % 7) for i in range(120)]


def test_crlf_line_endings_are_handled(tmp_path: Path) -> None:
    # Python's FileHandler writes \r\n on Windows.
    text = _line("one") + "\r\n" + _line("two") + "\r\n"
    _write(tmp_path, text)
    chunk = read_log_chunk(tmp_path, None)
    assert _messages(chunk) == ["one", "two"]
    assert chunk["offset"] == len(text.encode())


def test_traceback_lines_come_back_as_continuations(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _line("failed", level="ERROR")
        + '\nTraceback (most recent call last):\n  File "a.py", line 3\nValueError: boom\n',
    )
    entries = read_log_chunk(tmp_path, None)["entries"]
    assert [bool(e.get("continuation")) for e in entries] == [False, True, True, True]
    assert entries[0]["level"] == "ERROR"


def test_a_replaced_shorter_file_resets_the_cursor(tmp_path: Path) -> None:
    _write(tmp_path, "".join(_line(f"old-{i}") + "\n" for i in range(50)))
    stale = read_log_chunk(tmp_path, None)["offset"]

    _write(tmp_path, _line("fresh") + "\n")
    chunk = read_log_chunk(tmp_path, stale)

    assert chunk["reset"] is True
    assert _messages(chunk) == ["fresh"]


def test_a_cursor_beyond_the_end_is_a_reset_not_an_error(tmp_path: Path) -> None:
    _write(tmp_path, _line("a") + "\n")
    chunk = read_log_chunk(tmp_path, 10**9)
    assert chunk["reset"] is True
    assert _messages(chunk) == ["a"]


def test_a_long_log_tails_the_most_recent_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_log, "TAIL_LINES", 10)
    _write(tmp_path, "".join(_line(f"m{i}") + "\n" for i in range(100)))

    chunk = read_log_chunk(tmp_path, None)

    assert _messages(chunk) == [f"m{i}" for i in range(90, 100)]
    assert chunk["truncated_head"] is True
    assert chunk["more"] is False


def test_tail_that_starts_mid_file_drops_the_cut_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = [_line(f"message-number-{i:03d}") for i in range(200)]
    text = "\n".join(lines) + "\n"
    _write(tmp_path, text)
    monkeypatch.setattr(run_log, "TAIL_BYTES", 500)

    chunk = read_log_chunk(tmp_path, None)

    messages = _messages(chunk)
    assert chunk["truncated_head"] is True
    assert messages[-1] == "message-number-199"
    # Every returned line is a whole, correctly parsed line: no fragment survived.
    assert all(
        m.startswith("message-number-") and len(m) == len("message-number-000") for m in messages
    )
    assert not any(e.get("continuation") for e in chunk["entries"])
    assert 1 < len(messages) < 200


def test_a_large_backlog_is_delivered_in_chunks_with_more_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_log, "MAX_CHUNK_BYTES", 400)
    expected = [f"entry-{i:03d}" for i in range(60)]
    _write(tmp_path, "".join(_line(m) + "\n" for m in expected))

    seen: list[str] = []
    cursor = 0
    polls = 0
    while True:
        chunk = read_log_chunk(tmp_path, cursor)
        seen += _messages(chunk)
        cursor = chunk["offset"]
        polls += 1
        assert polls < 200
        if not chunk["more"]:
            break

    assert seen == expected
    assert polls > 1


def test_one_giant_line_does_not_stall_the_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_log, "MAX_CHUNK_BYTES", 300)
    _write(tmp_path, _line("x" * 2000) + "\n" + _line("after") + "\n")

    seen: list[str] = []
    cursor = 0
    for _ in range(50):
        chunk = read_log_chunk(tmp_path, cursor)
        seen += _messages(chunk)
        cursor = chunk["offset"]
        if not chunk["more"]:
            break

    assert any(m == "after" for m in seen), "the stream must get past the huge line"


def test_invalid_utf8_is_replaced_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / LOG_FILE_NAME
    path.write_bytes(_line("ok").encode() + b"\n" + _line("bad ").encode() + b"\xff\xfe\n")
    messages = _messages(read_log_chunk(tmp_path, None))
    assert messages[0] == "ok"
    assert messages[1].startswith("bad ")


def test_empty_file_exists_with_no_entries(tmp_path: Path) -> None:
    _write(tmp_path, "")
    chunk = read_log_chunk(tmp_path, None)
    assert chunk["exists"] is True
    assert chunk["entries"] == []


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs privileges on Windows")
def test_a_symlinked_log_is_not_followed(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text(_line("TOP-SECRET") + "\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / LOG_FILE_NAME).symlink_to(secret)

    chunk = read_log_chunk(run_dir, None)

    assert chunk["exists"] is False
    assert "TOP-SECRET" not in repr(chunk)


def test_a_directory_named_strix_log_is_not_a_log(tmp_path: Path) -> None:
    (tmp_path / LOG_FILE_NAME).mkdir()
    assert read_log_chunk(tmp_path, None)["exists"] is False


# --- redaction -----------------------------------------------------------------


def test_secrets_in_messages_are_masked(tmp_path: Path) -> None:
    key = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    _write(tmp_path, _line(f"calling provider with {key} now") + "\n")

    chunk = read_log_chunk(tmp_path, None)

    assert key not in repr(chunk)
    assert "[REDACTED]" in chunk["entries"][0]["message"]


def test_a_saved_config_key_is_masked_even_though_it_is_not_in_the_environment(
    tmp_path: Path,
) -> None:
    saved_key = (
        "my-local-provider-key-9f8e7d6c"  # not sk- shaped: only the env-value rule can catch it
    )
    _write(tmp_path, _line(f"Authorization failed for {saved_key}") + "\n")
    redactor = make_redactor({"LLM_API_KEY": saved_key})

    chunk = read_log_chunk(tmp_path, None, redactor)

    assert saved_key not in repr(chunk)
    assert chunk["entries"][0]["message"] == "Authorization failed for [REDACTED]"


def test_continuation_lines_are_masked_too(tmp_path: Path) -> None:
    saved_key = "my-local-provider-key-9f8e7d6c"
    _write(tmp_path, _line("boom", level="ERROR") + f"\n  headers = {{'x': '{saved_key}'}}\n")
    chunk = read_log_chunk(tmp_path, None, make_redactor({"LLM_API_KEY": saved_key}))
    assert saved_key not in repr(chunk)


def test_make_redactor_matches_redact() -> None:
    env = {"SOME_TOKEN": "tok-1234567890", "HARMLESS": "short-word-value"}
    text = "using tok-1234567890 and Bearer abc.def and short-word-value"
    assert make_redactor(env)(text) == redact(text, env)
    assert "tok-1234567890" not in make_redactor(env)(text)
    assert "short-word-value" in make_redactor(env)(text)  # non-secret names are left alone


def test_make_redactor_handles_empty_text() -> None:
    assert make_redactor({})("") == ""


# --- contract with the real writer ---------------------------------------------


def test_reads_exactly_what_the_scans_own_logger_writes(tmp_path: Path) -> None:
    # The regex above was written against ``_FORMAT``; this guards against the writer
    # and the reader drifting apart by using the scan's real logging setup.
    teardown = setup_scan_logging(tmp_path, debug=False)
    try:
        log = logging.getLogger("strix.core.runner")
        set_scan_id("scan-abc")
        set_agent_id(None)
        log.info("Scan %s started", "scan-abc")
        set_agent_id("agent-xyz")
        log.warning("slow tool: %s", "nmap -sV")
        try:
            raise ValueError("boom")  # noqa: TRY301
        except ValueError:
            log.exception("tool failed")
        log.debug("a debug detail")
    finally:
        teardown()
        set_agent_id(None)

    chunk = read_log_chunk(tmp_path, None)
    entries = chunk["entries"]
    records = [e for e in entries if not e.get("continuation")]
    continuations = [e for e in entries if e.get("continuation")]

    assert [(r["level"], r["agent"], r["logger"], r["message"]) for r in records] == [
        ("INFO", None, "strix.core.runner", "Scan scan-abc started"),
        ("WARNING", "agent-xyz", "strix.core.runner", "slow tool: nmap -sV"),
        ("ERROR", "agent-xyz", "strix.core.runner", "tool failed"),
        ("DEBUG", "agent-xyz", "strix.core.runner", "a debug detail"),
    ]
    assert all(r["ts"] and r["ts"].endswith("Z") for r in records)
    # The traceback follows its ERROR record as continuation lines.
    assert any("ValueError: boom" in c["message"] for c in continuations)
    assert any("Traceback" in c["message"] for c in continuations)
    assert chunk["more"] is False
