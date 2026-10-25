"""End-to-end test of the LLM *profiles* → launch path (no viewer restart).

ONE real :class:`ThreadingHTTPServer` with a real :class:`SettingsStore` +
:class:`ProfileStore` on a temp config path and a real :class:`ScanLauncher`
whose ``child_env_builder`` / ``pre_launch_check`` come from that store — wired
exactly as :func:`strix.interface.viewer.cli._serve_launch_mode`. The launcher's
OS seams are faked (a ``FakePopen``, a stub resolver / classifier), so no scan,
DNS or network happens.

The test saves two profiles, activates A then launches (child env stripped),
activates B then launches again (still stripped), and asserts the on-disk
canonical entries now carry B's model — all within the same server process.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

from strix.interface.viewer.launch_request import LaunchLimits
from strix.interface.viewer.llm_connection_test import ConnectionTester
from strix.interface.viewer.llm_profiles import ProfileStore
from strix.interface.viewer.llm_settings import SettingsStore
from strix.interface.viewer.scan_launcher import ScanLauncher
from strix.interface.viewer.server import serve


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


SECRET_A = "sk-PROFILEAAAAAAAAAAAAAAAAAAAAAAA"  # noqa: S105 - test-only fake secret
SECRET_B = "sk-PROFILEBBBBBBBBBBBBBBBBBBBBBBB"  # noqa: S105 - test-only fake secret

NINE_NAMES = {
    "STRIX_LLM",
    "LLM_API_KEY",
    "LLM_API_BASE",
    "STRIX_REASONING_EFFORT",
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "OPENAI_BASE_URL",
    "LITELLM_BASE_URL",
    "OLLAMA_API_BASE",
}


class FakePopen:
    _next_pid = 60_000

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.argv = argv
        self.kwargs = kwargs
        FakePopen._next_pid += 1
        self.pid = FakePopen._next_pid
        self._exit_code: int | None = None
        self.stdout: io.StringIO | None = io.StringIO("")

    def finish(self, code: int) -> None:
        self._exit_code = code

    def poll(self) -> int | None:
        return self._exit_code

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        if self._exit_code is None:
            self._exit_code = 0
        return self._exit_code

    def send_signal(self, sig: int) -> None:
        _ = sig

    def kill(self) -> None:
        if self._exit_code is None:
            self._exit_code = -9


def _ok_resolver(_host: str, _timeout: float) -> list[str]:
    return ["93.184.216.34"]


def _web_classifier(_target: str) -> str:
    return "web_application"


def _bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assets = tmp_path / "bundle"
    assets.mkdir(exist_ok=True)
    (assets / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    monkeypatch.setattr("strix.interface.viewer.server.bundle_dir", lambda: assets)


def _session_cookie(url: str, token: str) -> str:
    bootstrap = f"{url}/?token={token}"
    with urllib.request.urlopen(bootstrap) as resp:  # noqa: S310  # nosec B310
        raw = str(resp.headers.get("Set-Cookie", ""))
    return raw.split(";", 1)[0]


def _request(
    url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
    cookie: str | None = None,
) -> tuple[int, bytes]:
    hdrs: dict[str, str] = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    if cookie:
        hdrs["Cookie"] = cookie
    req = urllib.request.Request(url + path, data=data, headers=hdrs, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310  # nosec B310
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class _Wiring:
    def __init__(self) -> None:
        self.httpd: Any = None
        self.url: str = ""
        self.token: str = ""
        self.config: Path
        self.spawned: list[FakePopen] = []


def _wire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Wiring:
    _bundle(tmp_path, monkeypatch)
    runs_dir = tmp_path / "strix_runs"
    runs_dir.mkdir(exist_ok=True)
    config = tmp_path / "cli-config.json"
    wiring = _Wiring()
    wiring.config = config

    def popen(argv: list[str], **kwargs: Any) -> FakePopen:
        proc = FakePopen(argv, **kwargs)
        wiring.spawned.append(proc)
        return proc

    store = SettingsStore(path=config, environ={}, resolver=_ok_resolver)
    profile_store = ProfileStore(store)
    tester = ConnectionTester()
    launcher = ScanLauncher(
        runs_dir,
        LaunchLimits(),
        popen=popen,
        resolver=_ok_resolver,
        classifier=_web_classifier,
        child_env_builder=store.child_env,
        pre_launch_check=store.pre_launch_check,
    )
    httpd, url, token = serve(
        None,
        host="127.0.0.1",
        open_browser=False,
        runs_dir=runs_dir,
        launcher=launcher,
        settings_store=store,
        connection_tester=tester,
        profile_store=profile_store,
    )
    wiring.httpd = httpd
    wiring.url = url
    wiring.token = token
    wiring.launcher = launcher  # type: ignore[attr-defined]
    return wiring


def _finish_last_child(wiring: _Wiring) -> None:
    wiring.spawned[-1].finish(0)
    assert wiring.launcher.active_run() is None  # type: ignore[attr-defined]


def test_two_profiles_activate_switch_launch_no_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The viewer's own shell environment (read by child_env via os.environ).
    monkeypatch.setenv("DEEPSEEK_API_KEY", "shell")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "shell")
    monkeypatch.setenv("PATH_PROBE", "keep-me")

    wiring = _wire(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(wiring.url, wiring.token)

        # Save profile A (deepseek) and profile B (anthropic).
        status, payload = _request(
            wiring.url,
            "/api/settings/profiles/save",
            method="POST",
            body={"name": "A", "model": "deepseek/deepseek-chat", "api_key": SECRET_A},
            cookie=cookie,
        )
        assert status == 200
        pid_a = json.loads(payload)["profiles"][0]["id"]
        status, payload = _request(
            wiring.url,
            "/api/settings/profiles/save",
            method="POST",
            body={"name": "B", "model": "anthropic/claude-sonnet-4-5", "api_key": SECRET_B},
            cookie=cookie,
        )
        assert status == 200
        pid_b = next(p["id"] for p in json.loads(payload)["profiles"] if p["name"] == "B")

        # Activate A, launch: child env stripped; derived DEEPSEEK_API_KEY gone.
        status, _ = _request(
            wiring.url,
            "/api/settings/profiles/activate",
            method="POST",
            body={"id": pid_a},
            cookie=cookie,
        )
        assert status == 200
        status, _ = _request(
            wiring.url,
            "/api/scans",
            method="POST",
            body={"target": "https://example.com", "authorized": True},
            cookie=cookie,
        )
        assert status == 202
        env_a = wiring.spawned[0].kwargs["env"]
        for name in NINE_NAMES:
            assert name not in env_a
        assert "DEEPSEEK_API_KEY" not in env_a
        assert SECRET_A not in "".join(env_a.values())
        assert env_a.get("PATH_PROBE") == "keep-me"
        _finish_last_child(wiring)

        # Activate B, launch again: still stripped; derived ANTHROPIC_API_KEY gone.
        status, _ = _request(
            wiring.url,
            "/api/settings/profiles/activate",
            method="POST",
            body={"id": pid_b},
            cookie=cookie,
        )
        assert status == 200
        status, _ = _request(
            wiring.url,
            "/api/scans",
            method="POST",
            body={"target": "https://example.com", "authorized": True},
            cookie=cookie,
        )
        assert status == 202
        env_b = wiring.spawned[1].kwargs["env"]
        for name in NINE_NAMES:
            assert name not in env_b
        assert "ANTHROPIC_API_KEY" not in env_b
        assert SECRET_B not in "".join(env_b.values())
        _finish_last_child(wiring)

        # The saved file's canonical entries now carry B's model — no restart.
        env = json.loads(wiring.config.read_text(encoding="utf-8"))["env"]
        assert env["STRIX_LLM"] == "anthropic/claude-sonnet-4-5"
        assert env["STRIX_VIEWER_LLM_ACTIVE"] == pid_b
    finally:
        wiring.httpd.shutdown()
        wiring.httpd.server_close()
