"""Tests for the ``--run-name`` option, ``probe_git`` and the win32 break handler."""

from __future__ import annotations

import importlib
import signal
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest


if TYPE_CHECKING:
    from pathlib import Path


cli_args: Any = importlib.import_module("strix.interface.cli_args")
cli_main: Any = importlib.import_module("strix.interface.main")
scan_setup: Any = importlib.import_module("strix.interface.scan_setup")
utils: Any = importlib.import_module("strix.interface.utils")


def _stub_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli_args,
        "load_settings",
        lambda: SimpleNamespace(runtime=SimpleNamespace(max_local_copy_mb=1024)),
        raising=False,
    )


# --- --run-name parsing -----------------------------------------------------


def test_run_name_valid_is_stored_as_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys, "argv", ["strix", "-t", "https://example.com/", "-n", "--run-name", "my-run_01.2"]
    )

    args = cli_args.parse_arguments()

    assert args.run_name_override == "my-run_01.2"
    # The generated-name slot stays None until prepare_run resolves it.
    assert args.run_name is None


def test_run_name_default_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["strix", "-t", "https://example.com/", "-n"])

    args = cli_args.parse_arguments()

    assert args.run_name_override is None


@pytest.mark.parametrize(
    "bad_name",
    ["-leading", ".ok-but-start-bad".replace(".", "."), "has space", "a" * 65, "", "bad/slash"],
)
def test_run_name_invalid_is_rejected(
    bad_name: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["strix", "-t", "https://example.com/", "-n", "--run-name", bad_name]
    )

    with pytest.raises(SystemExit):
        cli_args.parse_arguments()

    assert "--run-name" in capsys.readouterr().err


def test_run_name_rejects_combination_with_resume(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["strix", "--resume", "old-run", "--run-name", "new-run"])

    with pytest.raises(SystemExit):
        cli_args.parse_arguments()

    assert "Cannot combine --run-name with --resume" in capsys.readouterr().err


# --- prepare_run honoring the override --------------------------------------


def _args_for_prepare(run_name_override: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        run_name_override=run_name_override,
        resume=None,
        targets_info=[],
        local_sources=[],
        instruction=None,
        scope_mode="full",
        diff_base=None,
        non_interactive=True,
    )


def test_prepare_run_uses_override_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scan_setup, "_persist_run_record", lambda _args: None)
    monkeypatch.setattr(scan_setup, "attach_workspace_mount", lambda _args: None)
    monkeypatch.setattr(
        scan_setup,
        "resolve_diff_scope_context",
        lambda **_: SimpleNamespace(metadata={"active": False}, instruction_block=None),
    )
    monkeypatch.setattr(scan_setup, "collect_local_sources", lambda _: [])
    monkeypatch.setattr(scan_setup, "stage_api_specs", lambda *_: [])
    args = _args_for_prepare("chosen-name")

    scan_setup.prepare_run(args)

    assert args.run_name == "chosen-name"


def test_prepare_run_rejects_existing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strix_runs" / "taken").mkdir(parents=True)
    args = _args_for_prepare("taken")

    with pytest.raises(ValueError, match="already exists"):
        scan_setup.prepare_run(args)


def test_prepare_run_without_override_generates_a_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scan_setup, "_persist_run_record", lambda _args: None)
    monkeypatch.setattr(scan_setup, "attach_workspace_mount", lambda _args: None)
    monkeypatch.setattr(
        scan_setup,
        "resolve_diff_scope_context",
        lambda **_: SimpleNamespace(metadata={"active": False}, instruction_block=None),
    )
    monkeypatch.setattr(scan_setup, "collect_local_sources", lambda _: [])
    monkeypatch.setattr(scan_setup, "stage_api_specs", lambda *_: [])
    monkeypatch.setattr(scan_setup, "generate_run_name", lambda _info: "generated_abcd")
    args = _args_for_prepare(None)

    scan_setup.prepare_run(args)

    assert args.run_name == "generated_abcd"


# --- infer_target_type probe_git --------------------------------------------


def test_infer_target_type_probe_git_false_makes_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_requests = MagicMock()
    monkeypatch.setattr(utils, "requests", mock_requests)

    target_type, details = utils.infer_target_type("https://github.com/org/repo", probe_git=False)

    assert target_type == "web_application"
    assert details == {"target_url": "https://github.com/org/repo"}
    mock_requests.get.assert_not_called()


def test_infer_target_type_default_still_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = MagicMock(return_value=True)
    monkeypatch.setattr(utils, "_is_http_git_repo", probe)

    target_type, _details = utils.infer_target_type("https://host.example/org/repo")

    assert target_type == "repository"
    probe.assert_called_once()


# --- win32 break handler ----------------------------------------------------


def test_break_handler_installed_only_on_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, Any]] = []
    monkeypatch.setattr(cli_main.sys, "platform", "win32")
    monkeypatch.setattr(cli_main.signal, "SIGBREAK", 21, raising=False)
    monkeypatch.setattr(
        cli_main.signal, "signal", lambda sig, handler: calls.append((sig, handler))
    )

    cli_main._install_windows_break_handler()

    assert calls == [(21, signal.default_int_handler)]


def test_break_handler_noop_off_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(cli_main.sys, "platform", "linux")
    monkeypatch.setattr(cli_main.signal, "signal", lambda *a: calls.append(a))

    cli_main._install_windows_break_handler()

    assert calls == []
