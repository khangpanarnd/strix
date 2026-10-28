"""Compatibility tests: the viewer store and the real CLI loader (DD-1, NFR-6).

These exercise the REAL ``strix.config.loader`` against a file the viewer store
wrote, to prove the managed marker (an unknown ``env`` entry) survives a CLI
rewrite, the CLI reads the saved model/key/base, env still wins, and importing
the loader does not pull LiteLLM into the process.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from strix.config import loader
from strix.interface.viewer.llm_settings import MARKER, SettingsStore


if TYPE_CHECKING:
    from pathlib import Path


_LLM_ENV_KEYS = [
    "STRIX_LLM",
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "LLM_API_BASE",
    "OPENAI_API_BASE",
    "OPENAI_BASE_URL",
    "LITELLM_BASE_URL",
    "OLLAMA_API_BASE",
    "STRIX_REASONING_EFFORT",
    MARKER,
]


@pytest.fixture(autouse=True)
def _reset_loader_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset loader globals and clear LLM env vars for deterministic runs."""
    for key in _LLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(loader, "_cached", None)
    monkeypatch.setattr(loader, "_override", None)


def _store(tmp_path: Path) -> SettingsStore:
    # No resolver/network is needed: these tests never save an api_base.
    return SettingsStore(tmp_path / "cli-config.json", environ={})


def test_cli_loader_reads_viewer_saved_values(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply(
        {"model": "anthropic/claude-sonnet-4-5", "api_key": "sk-compat", "reasoning_effort": "low"}
    )

    loader.apply_config_override(tmp_path / "cli-config.json")
    settings = loader.load_settings()

    assert settings.llm.model == "anthropic/claude-sonnet-4-5"
    assert settings.llm.api_key == "sk-compat"
    assert settings.llm.reasoning_effort == "low"


def test_marker_survives_cli_persist_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.apply({"model": "file-model", "api_key": "sk-marker"})

    # A terminal run rewrites the file via persist_current; the shell changes
    # only an unrelated var, so the stale-connection drop should not fire.
    loader.apply_config_override(tmp_path / "cli-config.json")
    loader.load_settings()
    monkeypatch.setenv("PERPLEXITY_API_KEY", "env-pplx")
    loader.persist_current()

    env = json.loads((tmp_path / "cli-config.json").read_text(encoding="utf-8"))["env"]
    # The marker is an unknown entry and must survive the CLI rewrite (DD-1, A1).
    assert env[MARKER] == "1"
    assert env["STRIX_LLM"] == "file-model"
    assert env["LLM_API_KEY"] == "sk-marker"
    assert env["PERPLEXITY_API_KEY"] == "env-pplx"


def test_cli_env_still_wins_over_viewer_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.apply({"model": "file-model", "api_key": "sk-file"})

    monkeypatch.setenv("STRIX_LLM", "env-model")
    loader.apply_config_override(tmp_path / "cli-config.json")

    # env wins per the CLI's precedence; the file value is not surfaced.
    assert loader.load_settings().llm.model == "env-model"


def test_loader_import_does_not_pull_litellm() -> None:
    # The viewer process must never import LiteLLM (project.md c3). Importing the
    # loader in a fresh subprocess must leave litellm absent from sys.modules.
    code = (
        "import sys; import strix.config.loader; "
        "assert 'litellm' not in sys.modules, sorted(m for m in sys.modules if 'litellm' in m); "
        "print('ok')"
    )
    result = subprocess.run(  # noqa: S603 — fixed args, no shell, trusted interpreter
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_viewer_store_import_does_not_pull_litellm() -> None:
    # The store module itself must not pull LiteLLM transitively either.
    code = (
        "import sys; import strix.interface.viewer.llm_settings; "
        "assert 'litellm' not in sys.modules, sorted(m for m in sys.modules if 'litellm' in m); "
        "print('ok')"
    )
    result = subprocess.run(  # noqa: S603 — fixed args, no shell, trusted interpreter
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
