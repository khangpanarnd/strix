"""End-to-end test of the LLM settings → launch path (Step 6.2; FR-16, FR-17, NFR-5).

ONE real :class:`ThreadingHTTPServer` is run with a real :class:`SettingsStore`
on a temp config path and a real :class:`ScanLauncher` whose ``child_env_builder``
and ``pre_launch_check`` come from that store — wired exactly as
:func:`strix.interface.viewer.cli._serve_launch_mode` wires them. The launcher's
OS seams are faked (a ``FakePopen`` instead of a real process, a stub resolver
and classifier), so no scan, DNS or network ever happens.

The test proves the "no restart" contract: a save made through the API between
two launches is picked up by the next launch's child environment, with the same
server process throughout. It also proves the managed strip set removes every
LLM variable (and the derived provider variable) while leaving the rest of the
child's environment — and its cwd/argv — untouched, and that a saved base that
points at a cloud-metadata address refuses the launch before any spawn.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

from strix.interface.viewer.launch_request import LaunchLimits
from strix.interface.viewer.llm_connection_test import ConnectionTester
from strix.interface.viewer.llm_settings import SettingsStore
from strix.interface.viewer.scan_launcher import ScanLauncher
from strix.interface.viewer.server import serve


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


SECRET_KEY = "sk-ANTHROPICSECRET1234567890abcdef"  # noqa: S105 - test-only fake secret

# The nine strip-set names (four canonical + five aliases).
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
    """A stand-in for subprocess.Popen; records argv/kwargs, finishes on demand."""

    _next_pid = 50_000

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
    """Holds the one server, store, launcher and the list of spawned children."""

    def __init__(self) -> None:
        self.httpd: Any = None
        self.url: str = ""
        self.token: str = ""
        self.store: SettingsStore
        self.launcher: ScanLauncher
        self.spawned: list[FakePopen] = []


def _wire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolver: Any = _ok_resolver,
) -> _Wiring:
    """Wire the store + launcher + server exactly as ``cli._serve_launch_mode`` does."""
    _bundle(tmp_path, monkeypatch)
    runs_dir = tmp_path / "strix_runs"
    runs_dir.mkdir(exist_ok=True)
    config = tmp_path / "cli-config.json"

    wiring = _Wiring()

    def popen(argv: list[str], **kwargs: Any) -> FakePopen:
        proc = FakePopen(argv, **kwargs)
        wiring.spawned.append(proc)
        return proc

    # The store reads os.environ at child_env time (it is called with os.environ
    # by the launcher's _spawn); its own view uses environ={} so the file is the
    # only managed source.
    store = SettingsStore(path=config, environ={}, resolver=resolver)
    tester = ConnectionTester()
    launcher = ScanLauncher(
        runs_dir,
        LaunchLimits(),
        popen=popen,
        resolver=resolver,
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
    )
    wiring.httpd = httpd
    wiring.url = url
    wiring.token = token
    wiring.store = store
    wiring.launcher = launcher
    return wiring


def _finish_last_child(wiring: _Wiring) -> None:
    """Finish the most recent child and confirm the launcher frees the slot."""
    wiring.spawned[-1].finish(0)
    # ``active_run`` consults process handles first; a finished child frees the slot.
    assert wiring.launcher.active_run() is None


def test_save_then_launch_no_restart_strips_llm_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The viewer's own shell environment (what child_env reads via os.environ).
    monkeypatch.setenv("STRIX_LLM", "old")
    monkeypatch.setenv("LLM_API_KEY", "old")
    monkeypatch.setenv("OPENAI_API_KEY", "old")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "shell")
    monkeypatch.setenv("PATH_PROBE", "keep-me")

    wiring = _wire(tmp_path, monkeypatch)
    try:
        cookie = _session_cookie(wiring.url, wiring.token)

        # (a) Launch #1 before any save: unmanaged pass-through (FR-18).
        status, _ = _request(
            wiring.url,
            "/api/scans",
            method="POST",
            body={"target": "https://example.com", "authorized": True},
            cookie=cookie,
        )
        assert status == 202
        env1 = wiring.spawned[0].kwargs["env"]
        assert env1["ANTHROPIC_API_KEY"] == "shell"
        assert env1["STRIX_LLM"] == "old"
        assert env1["LLM_API_KEY"] == "old"
        cwd1 = wiring.spawned[0].kwargs["cwd"]
        argv1 = wiring.spawned[0].argv
        _finish_last_child(wiring)

        # (b) PUT a save: an anthropic model, a key and a reasoning effort.
        status, _ = _request(
            wiring.url,
            "/api/settings",
            method="PUT",
            body={
                "model": "anthropic/claude-sonnet-4-5",
                "api_key": SECRET_KEY,
                "reasoning_effort": "high",
            },
            cookie=cookie,
        )
        assert status == 200

        # (c) Launch #2: managed now. The child's env has NONE of the nine names,
        # no ANTHROPIC_API_KEY (the derived provider var for anthropic/...), and
        # no secret value. Proves "no restart": the same server picked up the save.
        status, _ = _request(
            wiring.url,
            "/api/scans",
            method="POST",
            body={"target": "https://example.com", "authorized": True},
            cookie=cookie,
        )
        assert status == 202
        env2 = wiring.spawned[1].kwargs["env"]
        for name in NINE_NAMES:
            assert name not in env2, f"{name} must be stripped for a managed launch"
        assert "ANTHROPIC_API_KEY" not in env2
        assert SECRET_KEY not in "".join(env2.values())
        # The child's cwd and argv are unchanged by the settings feature.
        assert wiring.spawned[1].kwargs["cwd"] == cwd1
        assert wiring.spawned[1].argv[:3] == argv1[:3]
        # An unrelated variable still passes through.
        assert env2.get("PATH_PROBE") == "keep-me"
        _finish_last_child(wiring)

        # (d) Reset and launch #3: the shell values return (unmanaged again).
        status, _ = _request(
            wiring.url,
            "/api/settings/reset",
            method="POST",
            body={"confirm": True},
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
        env3 = wiring.spawned[2].kwargs["env"]
        assert env3["ANTHROPIC_API_KEY"] == "shell"
        assert env3["STRIX_LLM"] == "old"
        assert env3["LLM_API_KEY"] == "old"
    finally:
        wiring.httpd.shutdown()
        wiring.httpd.server_close()


def test_saved_metadata_base_refuses_launch_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (e) A saved base that rebinds to the cloud-metadata address makes the
    # pre-launch check refuse the launch with 400 llm_base_blocked and no spawn.
    monkeypatch.setenv("STRIX_LLM", "old")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "shell")

    # The resolver returns a benign address at save time and the metadata address
    # at launch time, so the pre-launch re-resolution catches the rebind (DD-9).
    state = {"metadata": False}

    def flipping_resolver(_host: str, _timeout: float) -> list[str]:
        return ["169.254.169.254"] if state["metadata"] else ["93.184.216.34"]

    wiring = _wire(tmp_path, monkeypatch, resolver=flipping_resolver)
    try:
        cookie = _session_cookie(wiring.url, wiring.token)
        status, _ = _request(
            wiring.url,
            "/api/settings",
            method="PUT",
            body={
                "model": "anthropic/claude-sonnet-4-5",
                "api_key": SECRET_KEY,
                "api_base": "https://proxy.example.com",
            },
            cookie=cookie,
        )
        assert status == 200

        state["metadata"] = True
        status, payload = _request(
            wiring.url,
            "/api/scans",
            method="POST",
            body={"target": "https://example.com", "authorized": True},
            cookie=cookie,
        )
        assert status == 400
        assert json.loads(payload)["error"] == "llm_base_blocked"
        # No child was ever spawned.
        assert wiring.spawned == []
        assert wiring.launcher.active_run() is None
    finally:
        wiring.httpd.shutdown()
        wiring.httpd.server_close()
