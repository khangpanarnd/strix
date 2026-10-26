"""HTTP tests for ``GET /api/log`` (the viewer's streamed server log).

A real ``ThreadingHTTPServer`` over a temp runs directory, like the other viewer
API tests. The important ones: the route is session-gated, ``?run=`` cannot
escape the runs directory, the cursor works over HTTP, and a saved LLM key is
masked even though it is in neither the viewer's nor the scan child's environment.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

import pytest

from strix.interface.viewer.llm_settings import SettingsStore
from strix.interface.viewer.server import serve


if TYPE_CHECKING:
    from pathlib import Path


SAVED_KEY = (
    "local-provider-key-ZX81-secret"  # not sk- shaped; only the config-file rule can catch it
)


def _line(msg: str, level: str = "INFO") -> str:
    return f"2026-10-26 13:20:13.123 {level:<7} scan-1 agent-1 strix.core.runner: {msg}\n"


def _bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assets = tmp_path / "bundle"
    assets.mkdir(exist_ok=True)
    (assets / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    monkeypatch.setattr("strix.interface.viewer.server.bundle_dir", lambda: assets)


def _make_run(runs: Path, name: str, log: str | None) -> Path:
    run = runs / name
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run_name": name, "status": "running"}), "utf-8")
    if log is not None:
        (run / "strix.log").write_text(log, encoding="utf-8")
    return run


def _cookie(url: str, token: str) -> str:
    with urllib.request.urlopen(f"{url}/?token={token}") as resp:  # noqa: S310  # nosec B310
        return str(resp.headers.get("Set-Cookie", "")).split(";", 1)[0]


def _get(url: str, path: str, cookie: str | None = None) -> tuple[int, Any]:
    headers = {"Cookie": cookie} if cookie else {}
    req = urllib.request.Request(url + path, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310  # nosec B310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, (json.loads(body) if body else None)


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    _bundle(tmp_path, monkeypatch)
    runs = tmp_path / "strix_runs"
    runs.mkdir()
    config = tmp_path / "cli-config.json"
    config.write_text(json.dumps({"env": {"LLM_API_KEY": SAVED_KEY, "STRIX_LLM": "x/y"}}), "utf-8")
    store = SettingsStore(path=config, environ={})
    httpd, url, token = serve(None, open_browser=False, runs_dir=runs, settings_store=store)
    try:
        yield runs, url, token
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_requires_the_session_cookie(served: Any) -> None:
    runs, url, _token = served
    _make_run(runs, "web-1", _line("hello"))
    status, body = _get(url, "/api/log")
    assert status == 403
    assert body["error"] == "forbidden"


def test_no_run_is_404(served: Any) -> None:
    _runs, url, token = served
    status, body = _get(url, "/api/log", _cookie(url, token))
    assert status == 404
    assert body["error"] == "no_run"


def test_returns_the_latest_runs_log_with_a_cursor(served: Any) -> None:
    runs, url, token = served
    log = _line("starting") + _line("scanning", "WARNING")
    run = _make_run(runs, "web-1", log)

    status, body = _get(url, "/api/log", _cookie(url, token))

    assert status == 200
    assert body["exists"] is True
    assert [e["message"] for e in body["entries"]] == ["starting", "scanning"]
    assert body["entries"][1]["level"] == "WARNING"
    assert body["entries"][0]["agent"] == "agent-1"
    # Bytes on disk, which differ from len(log) where text mode writes \r\n (Windows).
    assert body["offset"] == (run / "strix.log").stat().st_size


def test_after_returns_only_new_lines(served: Any) -> None:
    runs, url, token = served
    run = _make_run(runs, "web-1", _line("one"))
    cookie = _cookie(url, token)
    first = _get(url, "/api/log", cookie)[1]

    with (run / "strix.log").open("a", encoding="utf-8") as handle:
        handle.write(_line("two"))
    second = _get(url, f"/api/log?after={first['offset']}", cookie)[1]

    assert [e["message"] for e in second["entries"]] == ["two"]
    assert _get(url, f"/api/log?after={second['offset']}", cookie)[1]["entries"] == []


def test_run_parameter_selects_a_specific_run(served: Any) -> None:
    runs, url, token = served
    _make_run(runs, "web-a", _line("from-a"))
    _make_run(runs, "web-b", _line("from-b"))
    cookie = _cookie(url, token)

    body = _get(url, "/api/log?run=web-a", cookie)[1]

    assert [e["message"] for e in body["entries"]] == ["from-a"]


@pytest.mark.parametrize("run", ["../outside", "..", "web-1/..", "%2e%2e%2fetc", "/etc", "nope"])
def test_run_parameter_cannot_escape_the_runs_directory(served: Any, run: str) -> None:
    runs, url, token = served
    _make_run(runs, "web-1", _line("inside"))
    outside = runs.parent / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "run.json").write_text("{}", "utf-8")
    (outside / "strix.log").write_text(_line("OUTSIDE-SECRET"), "utf-8")

    status, body = _get(url, f"/api/log?run={run}", _cookie(url, token))

    assert status == 404
    assert "OUTSIDE-SECRET" not in json.dumps(body)


def test_a_run_without_a_log_yet_reports_not_exists(served: Any) -> None:
    runs, url, token = served
    _make_run(runs, "web-1", None)
    status, body = _get(url, "/api/log", _cookie(url, token))
    assert status == 200
    assert body["exists"] is False
    assert body["entries"] == []


@pytest.mark.parametrize("after", ["-1", "abc", "1.5", "0x10"])
def test_invalid_cursor_is_400(served: Any, after: str) -> None:
    runs, url, token = served
    _make_run(runs, "web-1", _line("x"))
    status, body = _get(url, f"/api/log?after={after}", _cookie(url, token))
    assert status == 400
    assert body["error"] == "invalid_cursor"


def test_a_saved_llm_key_is_masked_in_the_stream(served: Any) -> None:
    runs, url, token = served
    _make_run(
        runs,
        "web-1",
        _line(f"401 from provider, key={SAVED_KEY}", "ERROR")
        + f"  repr(headers)={{'Authorization': 'Bearer {SAVED_KEY}'}}\n",
    )

    status, body = _get(url, "/api/log", _cookie(url, token))

    assert status == 200
    dumped = json.dumps(body)
    assert SAVED_KEY not in dumped
    assert "[REDACTED]" in dumped


def test_a_saved_key_is_masked_across_a_cursor_boundary(served: Any) -> None:
    runs, url, token = served
    run = _make_run(runs, "web-1", _line("first"))
    cookie = _cookie(url, token)
    offset = _get(url, "/api/log", cookie)[1]["offset"]
    with (run / "strix.log").open("a", encoding="utf-8") as handle:
        handle.write(_line(f"leaked {SAVED_KEY}"))

    body = _get(url, f"/api/log?after={offset}", cookie)[1]

    assert SAVED_KEY not in json.dumps(body)
