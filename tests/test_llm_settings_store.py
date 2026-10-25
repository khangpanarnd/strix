"""Tests for the viewer LLM settings store (FR-5..FR-18, BR-1..BR-28, BR-35, DD-1..DD-3).

The store is exercised against a temp config path with an injected resolver,
environ and platform, so no real DNS, network or home-directory file is
touched. A recognisable key token is used in several tests and asserted never
to appear in any returned dict, log record or exception text (BR-12, NFR-1).
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

import pytest

from strix.interface.viewer.launch_errors import LaunchError
from strix.interface.viewer.llm_settings import (
    ALIASES,
    CANONICAL,
    MARKER,
    MODEL_SUGGESTIONS,
    REASONING_CHOICES,
    STRIP_SET,
    SettingsStore,
    provider_key_var,
)


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# A recognisable secret used to prove it never leaks anywhere.
_SECRET = "sk-RECOGNISABLE-SECRET-0123456789"


def _resolver(mapping: dict[str, list[str]]) -> Callable[[str, float], list[str]]:
    def resolve(host: str, _timeout: float) -> list[str]:
        if host not in mapping:
            raise OSError("no such host")
        return mapping[host]

    return resolve


def _write(path: Path, env: dict[str, Any], *, extra_top: dict[str, Any] | None = None) -> None:
    data: dict[str, Any] = {"env": env}
    if extra_top:
        data.update(extra_top)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _read_env(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))["env"]


def _store(
    tmp_path: Path,
    *,
    environ: dict[str, str] | None = None,
    resolver: Callable[[str, float], list[str]] | None = None,
    platform: str = "linux",
) -> SettingsStore:
    return SettingsStore(
        tmp_path / "cli-config.json",
        environ=environ or {},
        resolver=resolver or _resolver({}),
        platform=platform,
    )


# --- constants --------------------------------------------------------------


def test_strip_set_is_the_nine_names() -> None:
    assert frozenset(CANONICAL.values()) | frozenset(ALIASES) == STRIP_SET
    assert len(STRIP_SET) == 9


def test_marker_name() -> None:
    assert MARKER == "STRIX_VIEWER_LLM_MANAGED"


# --- view: sources (FR-5, DD-6) ---------------------------------------------


def test_view_defaults_when_no_file(tmp_path: Path) -> None:
    view = _store(tmp_path).view(editable=True)
    assert view["file_status"] == "missing"
    assert view["managed"] is False
    assert view["fields"]["model"] == {"value": None, "source": "default"}
    assert view["fields"]["api_key"] == {"set": False, "source": "default"}
    assert view["fields"]["reasoning_effort"]["value"] == "high"
    assert view["reasoning_choices"] == list(REASONING_CHOICES)
    assert view["model_suggestions"] == list(MODEL_SUGGESTIONS)
    assert view["platform"] == "linux"
    assert view["config_path"] == str(tmp_path / "cli-config.json")


def test_view_source_environment_when_unmanaged(tmp_path: Path) -> None:
    store = _store(tmp_path, environ={"STRIX_LLM": "env-model", "OPENAI_API_KEY": _SECRET})
    _write(tmp_path / "cli-config.json", {"STRIX_LLM": "file-model"})
    view = store.view(editable=True)
    # env wins over file when unmanaged.
    assert view["fields"]["model"] == {"value": "env-model", "source": "environment"}
    assert view["fields"]["api_key"] == {"set": True, "source": "environment"}


def test_view_source_file_when_unmanaged_no_env(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(tmp_path / "cli-config.json", {"STRIX_LLM": "file-model"})
    view = store.view(editable=True)
    assert view["fields"]["model"] == {"value": "file-model", "source": "file"}


def test_view_source_ui_when_managed(tmp_path: Path) -> None:
    store = _store(tmp_path, environ={"STRIX_LLM": "env-model", "LLM_API_KEY": _SECRET})
    _write(
        tmp_path / "cli-config.json",
        {MARKER: "1", "STRIX_LLM": "ui-model", "LLM_API_KEY": _SECRET},
    )
    view = store.view(editable=True)
    # Managed ignores the shell entirely.
    assert view["managed"] is True
    assert view["fields"]["model"] == {"value": "ui-model", "source": "ui"}
    assert view["fields"]["api_key"] == {"set": True, "source": "ui"}


def test_view_managed_cleared_field_is_default(tmp_path: Path) -> None:
    store = _store(tmp_path, environ={"STRIX_LLM": "env-model"})
    _write(tmp_path / "cli-config.json", {MARKER: "1"})
    view = store.view(editable=True)
    # Managed with no stored model -> default, not the shell value (BR-28).
    assert view["fields"]["model"] == {"value": None, "source": "default"}


def test_view_case_insensitive_names(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Hand-edited file with mixed-case names.
    (tmp_path / "cli-config.json").write_text(
        json.dumps({"env": {"strix_viewer_llm_managed": "1", "Strix_Llm": "mixed"}}),
        encoding="utf-8",
    )
    view = store.view(editable=True)
    assert view["managed"] is True
    assert view["fields"]["model"] == {"value": "mixed", "source": "ui"}


def test_view_unreadable_file(tmp_path: Path) -> None:
    (tmp_path / "cli-config.json").write_text("{not json", encoding="utf-8")
    view = _store(tmp_path).view(editable=True)
    assert view["file_status"] == "unreadable"


def test_view_scan_active_passed_through(tmp_path: Path) -> None:
    view = _store(tmp_path).view(editable=False, scan_active=True)
    assert view["editable"] is False
    assert view["scan_active"] is True


# --- apply: validation and shape (BR-5, BR-10) ------------------------------


def test_apply_unknown_field(tmp_path: Path) -> None:
    with pytest.raises(LaunchError) as exc:
        _store(tmp_path).apply({"nope": "x"})
    assert exc.value.code == "unknown_field"


def test_apply_wrong_clear_type(tmp_path: Path) -> None:
    with pytest.raises(LaunchError) as exc:
        _store(tmp_path).apply({"clear": "api_key"})
    assert exc.value.code == "invalid_json"


def test_apply_conflicting_set_and_clear(tmp_path: Path) -> None:
    with pytest.raises(LaunchError) as exc:
        _store(tmp_path).apply({"model": "m", "clear": ["model"]})
    assert exc.value.code == "conflicting_fields"


def test_apply_non_object_body(tmp_path: Path) -> None:
    with pytest.raises(LaunchError) as exc:
        _store(tmp_path).apply(["not", "a", "dict"])  # type: ignore[arg-type]
    assert exc.value.code == "invalid_json"


# --- apply: first save, canonical write, marker, alias removal --------------


def test_apply_first_save_writes_canonical_and_marker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    view = store.apply(
        {"model": "anthropic/claude-sonnet-4-5", "api_key": _SECRET, "reasoning_effort": "high"}
    )
    env = _read_env(tmp_path / "cli-config.json")
    assert env[MARKER] == "1"
    assert env["STRIX_LLM"] == "anthropic/claude-sonnet-4-5"
    assert env["LLM_API_KEY"] == _SECRET
    assert env["STRIX_REASONING_EFFORT"] == "high"
    assert view["managed"] is True
    assert view["warnings"] == []


def test_apply_removes_aliases_of_touched_field(tmp_path: Path) -> None:
    _write(
        tmp_path / "cli-config.json",
        {MARKER: "1", "OPENAI_API_KEY": "old", "OPENAI_API_BASE": "http://old"},
    )
    store = _store(tmp_path)
    store.apply({"api_key": _SECRET})
    env = _read_env(tmp_path / "cli-config.json")
    assert "OPENAI_API_KEY" not in env
    assert env["LLM_API_KEY"] == _SECRET
    # api_base not touched -> its alias stays.
    assert env["OPENAI_API_BASE"] == "http://old"


def test_apply_preserves_other_entries_and_top_level_keys(tmp_path: Path) -> None:
    _write(
        tmp_path / "cli-config.json",
        {"STRIX_IMAGE": "custom-image", "PERPLEXITY_API_KEY": "pplx"},
        extra_top={"other_top": {"keep": "me"}},
    )
    store = _store(tmp_path)
    store.apply({"model": "m1"})
    data = json.loads((tmp_path / "cli-config.json").read_text(encoding="utf-8"))
    assert data["env"]["STRIX_IMAGE"] == "custom-image"
    assert data["env"]["PERPLEXITY_API_KEY"] == "pplx"
    assert data["other_top"] == {"keep": "me"}


def test_apply_independent_save_keeps_other_fields(tmp_path: Path) -> None:
    _write(
        tmp_path / "cli-config.json",
        {MARKER: "1", "STRIX_LLM": "m", "LLM_API_KEY": _SECRET, "LLM_API_BASE": "https://b"},
    )
    store = _store(tmp_path, resolver=_resolver({}))
    store.apply({"model": "m2"})
    env = _read_env(tmp_path / "cli-config.json")
    assert env["STRIX_LLM"] == "m2"
    assert env["LLM_API_KEY"] == _SECRET
    assert env["LLM_API_BASE"] == "https://b"


def test_apply_first_save_adopts_file_values_not_shell_key(tmp_path: Path) -> None:
    # Unmanaged file holds model+key; the shell holds a different key. First save
    # of only reasoning adopts the file model and the FILE key (never the shell).
    _write(tmp_path / "cli-config.json", {"OPENAI_API_KEY": "file-key", "STRIX_LLM": "file-model"})
    store = _store(tmp_path, environ={"LLM_API_KEY": "shell-key"})
    store.apply({"reasoning_effort": "low"})
    env = _read_env(tmp_path / "cli-config.json")
    assert env[MARKER] == "1"
    assert env["STRIX_LLM"] == "file-model"
    assert env["LLM_API_KEY"] == "file-key"
    assert env["STRIX_REASONING_EFFORT"] == "low"


def test_apply_corrupt_file_is_untouched(tmp_path: Path) -> None:
    path = tmp_path / "cli-config.json"
    path.write_text("{not json", encoding="utf-8")
    before = path.read_bytes()
    store = _store(tmp_path)
    with pytest.raises(LaunchError) as exc:
        store.apply({"model": "m"})
    assert exc.value.code == "config_unreadable"
    assert exc.value.status == 409
    assert path.read_bytes() == before


def test_apply_model_unfamiliar_warning(tmp_path: Path) -> None:
    store = _store(tmp_path)
    view = store.apply({"model": "weird-local-model"})
    assert view["warnings"] == ["model_unfamiliar"]


# --- BR-13 base-change rule -------------------------------------------------


def test_base_change_with_saved_key_needs_key(tmp_path: Path) -> None:
    _write(tmp_path / "cli-config.json", {MARKER: "1", "LLM_API_KEY": _SECRET})
    store = _store(tmp_path, resolver=_resolver({}))
    with pytest.raises(LaunchError) as exc:
        store.apply({"api_base": "http://localhost:11434"})
    assert exc.value.code == "key_required_for_base_change"


def test_base_change_with_key_succeeds(tmp_path: Path) -> None:
    _write(tmp_path / "cli-config.json", {MARKER: "1", "LLM_API_KEY": "old"})
    store = _store(tmp_path, resolver=_resolver({}))
    store.apply({"api_base": "http://localhost:11434", "api_key": _SECRET})
    env = _read_env(tmp_path / "cli-config.json")
    assert env["LLM_API_BASE"] == "http://localhost:11434"
    assert env["LLM_API_KEY"] == _SECRET


def test_base_change_with_file_key_under_alias(tmp_path: Path) -> None:
    # Key in file under the OPENAI_API_KEY alias, shell holds LLM_API_KEY.
    _write(tmp_path / "cli-config.json", {MARKER: "1", "OPENAI_API_KEY": "file-key"})
    store = _store(tmp_path, environ={"LLM_API_KEY": "shell-key"}, resolver=_resolver({}))
    with pytest.raises(LaunchError) as exc:
        store.apply({"api_base": "http://localhost:11434"})
    # A saved key (under the alias) is present, so the base change needs a key.
    assert exc.value.code == "key_required_for_base_change"


def test_base_change_no_saved_key_allowed(tmp_path: Path) -> None:
    _write(tmp_path / "cli-config.json", {MARKER: "1", "STRIX_LLM": "m"})
    store = _store(tmp_path, resolver=_resolver({}))
    store.apply({"api_base": "http://localhost:11434"})
    assert _read_env(tmp_path / "cli-config.json")["LLM_API_BASE"] == "http://localhost:11434"


def test_key_and_base_cleared_then_new_base_allowed(tmp_path: Path) -> None:
    # After both key and base are cleared, a new base needs no key (BR-13).
    _write(
        tmp_path / "cli-config.json",
        {MARKER: "1", "LLM_API_KEY": _SECRET, "LLM_API_BASE": "https://old"},
    )
    store = _store(tmp_path, resolver=_resolver({}))
    store.apply({"clear": ["api_key", "api_base"]})
    env = _read_env(tmp_path / "cli-config.json")
    assert "LLM_API_KEY" not in env
    assert "LLM_API_BASE" not in env
    # Now no saved key -> a fresh base is accepted without a key.
    store.apply({"api_base": "http://localhost:11434"})
    assert _read_env(tmp_path / "cli-config.json")["LLM_API_BASE"] == "http://localhost:11434"


# --- BR-14 first-save key rule ----------------------------------------------


def test_first_save_shell_only_key_needs_key(tmp_path: Path) -> None:
    # Unmanaged, key only in the shell: first save must carry a key.
    store = _store(tmp_path, environ={"LLM_API_KEY": "shell-key"})
    with pytest.raises(LaunchError) as exc:
        store.apply({"model": "m"})
    assert exc.value.code == "key_required_first_save"


def test_first_save_shell_only_key_with_key_succeeds(tmp_path: Path) -> None:
    store = _store(tmp_path, environ={"OPENAI_API_KEY": "shell-key"})
    store.apply({"model": "m", "api_key": _SECRET})
    env = _read_env(tmp_path / "cli-config.json")
    assert env["LLM_API_KEY"] == _SECRET


def test_first_save_file_key_no_shell_does_not_require_key(tmp_path: Path) -> None:
    # Key is in the file (not shell-only) -> first save without a key is fine.
    _write(tmp_path / "cli-config.json", {"LLM_API_KEY": "file-key"})
    store = _store(tmp_path)
    store.apply({"model": "m"})
    env = _read_env(tmp_path / "cli-config.json")
    assert env["LLM_API_KEY"] == "file-key"  # adopted from the file


# --- clear ------------------------------------------------------------------


def test_clear_removes_canonical_and_alias(tmp_path: Path) -> None:
    _write(
        tmp_path / "cli-config.json",
        {MARKER: "1", "LLM_API_BASE": "https://b", "OPENAI_BASE_URL": "https://b2"},
    )
    store = _store(tmp_path)
    store.apply({"clear": ["api_base"]})
    env = _read_env(tmp_path / "cli-config.json")
    assert "LLM_API_BASE" not in env
    assert "OPENAI_BASE_URL" not in env


# --- reset (BR-22) ----------------------------------------------------------


def test_reset_requires_confirm(tmp_path: Path) -> None:
    with pytest.raises(LaunchError):
        _store(tmp_path).reset(confirm=False)


def test_reset_removes_marker_and_strip_set_only(tmp_path: Path) -> None:
    _write(
        tmp_path / "cli-config.json",
        {MARKER: "1", "STRIX_LLM": "m", "LLM_API_KEY": _SECRET, "PERPLEXITY_API_KEY": "pplx"},
    )
    store = _store(tmp_path)
    store.reset(confirm=True)
    env = _read_env(tmp_path / "cli-config.json")
    assert MARKER not in env
    assert "STRIX_LLM" not in env
    assert "LLM_API_KEY" not in env
    assert env["PERPLEXITY_API_KEY"] == "pplx"


# --- child_env (section 6, BR-25, BR-26) ------------------------------------


def test_child_env_unmanaged_passthrough(tmp_path: Path) -> None:
    store = _store(tmp_path)
    base = {"STRIX_LLM": "x", "LLM_API_KEY": "y", "PATH": "/bin"}
    assert store.child_env(base) == base


def test_child_env_managed_strips_strip_set(tmp_path: Path) -> None:
    _write(tmp_path / "cli-config.json", {MARKER: "1", "STRIX_LLM": "m", "LLM_API_KEY": _SECRET})
    store = _store(tmp_path)
    base = {
        "STRIX_LLM": "shell-model",
        "OPENAI_API_KEY": "shell-key",
        "LLM_API_BASE": "http://shell",
        "PATH": "/bin",
    }
    env = store.child_env(base)
    assert env == {"PATH": "/bin"}


def test_child_env_strips_case_insensitively(tmp_path: Path) -> None:
    _write(tmp_path / "cli-config.json", {MARKER: "1", "STRIX_LLM": "m"})
    store = _store(tmp_path)
    env = store.child_env({"strix_llm": "shell", "Path": "/bin"})
    assert "strix_llm" not in env
    assert env["Path"] == "/bin"


def test_child_env_strips_provider_key_when_file_names_are_lower_case(tmp_path: Path) -> None:
    # A hand-edited file may use lower-case names; the store upper-cases them on
    # read, so the derived provider variable must still be found and stripped.
    _write(
        tmp_path / "cli-config.json",
        {
            "strix_viewer_llm_managed": "1",
            "strix_llm": "anthropic/claude-x",
            "llm_api_key": _SECRET,
        },
    )
    store = _store(tmp_path)
    env = store.child_env({"ANTHROPIC_API_KEY": "shell-key", "PATH": "/bin"})
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "/bin"


def test_child_env_strips_derived_provider_key_when_ui_key_saved(tmp_path: Path) -> None:
    _write(
        tmp_path / "cli-config.json",
        {MARKER: "1", "STRIX_LLM": "anthropic/claude-sonnet-4-5", "LLM_API_KEY": _SECRET},
    )
    store = _store(tmp_path)
    base = {"ANTHROPIC_API_KEY": "shell-anthropic", "PATH": "/bin"}
    env = store.child_env(base)
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "/bin"


def test_child_env_keeps_provider_key_when_no_ui_key(tmp_path: Path) -> None:
    _write(
        tmp_path / "cli-config.json",
        {MARKER: "1", "STRIX_LLM": "anthropic/claude-sonnet-4-5"},
    )
    store = _store(tmp_path)
    env = store.child_env({"ANTHROPIC_API_KEY": "shell", "PATH": "/bin"})
    # No saved UI key -> the provider variable is left alone.
    assert env["ANTHROPIC_API_KEY"] == "shell"


def test_child_env_unreadable_file_is_unmanaged(tmp_path: Path) -> None:
    (tmp_path / "cli-config.json").write_text("{not json", encoding="utf-8")
    store = _store(tmp_path)
    base = {"STRIX_LLM": "x", "PATH": "/bin"}
    assert store.child_env(base) == base


# --- provider_key_var (DD-3) ------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("anthropic/claude-sonnet-4-5", "ANTHROPIC_API_KEY"),
        ("gemini/gemini-2.5-pro", "GEMINI_API_KEY"),
        ("openrouter/auto", "OPENROUTER_API_KEY"),
        ("together_ai/model", "TOGETHERAI_API_KEY"),
        ("perplexity/sonar", "PERPLEXITYAI_API_KEY"),
        ("mistralai/mistral-large", "MISTRAL_API_KEY"),
        ("litellm/anthropic/claude", "ANTHROPIC_API_KEY"),
        ("any-llm/gemini/pro", "GEMINI_API_KEY"),
        ("openai/gpt-5", None),
        ("chatgpt/gpt-4o", None),
        ("ollama/llama3.3", None),
        ("gpt-5", None),
        ("", None),
        (None, None),
    ],
)
def test_provider_key_var(model: str | None, expected: str | None) -> None:
    result = provider_key_var(model)
    assert result == expected
    if result is not None:
        assert re.match(r"^[A-Z0-9_]+_API_KEY$", result)


# --- pre_launch_check (section 6a, BR-36) -----------------------------------


def test_pre_launch_check_unmanaged_is_noop(tmp_path: Path) -> None:
    _write(tmp_path / "cli-config.json", {"LLM_API_BASE": "https://169.254.169.254"})
    _store(tmp_path).pre_launch_check()  # unmanaged -> no raise


def test_pre_launch_check_no_base_is_noop(tmp_path: Path) -> None:
    _write(tmp_path / "cli-config.json", {MARKER: "1", "STRIX_LLM": "m"})
    _store(tmp_path).pre_launch_check()


def test_pre_launch_check_metadata_name_blocked(tmp_path: Path) -> None:
    _write(
        tmp_path / "cli-config.json",
        {MARKER: "1", "LLM_API_BASE": "http://metadata.google.internal"},
    )
    with pytest.raises(LaunchError) as exc:
        _store(tmp_path).pre_launch_check()
    assert exc.value.code == "llm_base_blocked"


def test_pre_launch_check_resolved_metadata_blocked(tmp_path: Path) -> None:
    _write(
        tmp_path / "cli-config.json", {MARKER: "1", "LLM_API_BASE": "https://sneaky.example.com"}
    )
    store = _store(tmp_path, resolver=_resolver({"sneaky.example.com": ["169.254.169.254"]}))
    with pytest.raises(LaunchError) as exc:
        store.pre_launch_check()
    assert exc.value.code == "llm_base_blocked"


def test_pre_launch_check_clean_base_passes(tmp_path: Path) -> None:
    _write(tmp_path / "cli-config.json", {MARKER: "1", "LLM_API_BASE": "https://api.example.com"})
    store = _store(tmp_path, resolver=_resolver({"api.example.com": ["93.184.216.34"]}))
    store.pre_launch_check()  # no raise


def test_pre_launch_check_unresolvable_base_passes(tmp_path: Path) -> None:
    # Unresolvable is not metadata; the scan's later resolution is the backstop.
    _write(tmp_path / "cli-config.json", {MARKER: "1", "LLM_API_BASE": "https://gone.example.com"})
    store = _store(tmp_path, resolver=_resolver({}))
    store.pre_launch_check()


# --- effective_test_values (FR-27, BR-15) -----------------------------------


def test_effective_test_values_typed_override_saved(tmp_path: Path) -> None:
    _write(
        tmp_path / "cli-config.json",
        {
            MARKER: "1",
            "STRIX_LLM": "saved",
            "LLM_API_KEY": _SECRET,
            "LLM_API_BASE": "https://saved",
        },
    )
    store = _store(tmp_path, resolver=_resolver({"typed.example.com": ["93.184.216.34"]}))
    values = store.effective_test_values(
        {"model": "typed", "api_key": "typed-key", "api_base": "https://typed.example.com"}
    )
    assert values == {
        "model": "typed",
        "api_key": "typed-key",
        "api_base": "https://typed.example.com",
        "reasoning_effort": None,
    }


def test_effective_test_values_blank_key_same_base_uses_saved(tmp_path: Path) -> None:
    _write(
        tmp_path / "cli-config.json",
        {MARKER: "1", "LLM_API_KEY": _SECRET, "LLM_API_BASE": "https://saved.example.com"},
    )
    store = _store(tmp_path, resolver=_resolver({"saved.example.com": ["93.184.216.34"]}))
    values = store.effective_test_values({"api_base": "https://saved.example.com"})
    assert values["api_key"] == _SECRET


def test_effective_test_values_blank_key_different_base_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path / "cli-config.json",
        {MARKER: "1", "LLM_API_KEY": _SECRET, "LLM_API_BASE": "https://saved.example.com"},
    )
    store = _store(tmp_path, resolver=_resolver({"other.example.com": ["93.184.216.34"]}))
    with pytest.raises(LaunchError) as exc:
        store.effective_test_values({"api_base": "https://other.example.com"})
    assert exc.value.code == "key_required_for_base_change"


def test_effective_test_values_never_writes(tmp_path: Path) -> None:
    path = tmp_path / "cli-config.json"
    _write(path, {MARKER: "1", "LLM_API_KEY": _SECRET})
    before = path.read_bytes()
    store = _store(tmp_path, resolver=_resolver({}))
    store.effective_test_values({"model": "typed"})
    assert path.read_bytes() == before


# --- helpers ----------------------------------------------------------------


def test_saved_key_present_and_base(tmp_path: Path) -> None:
    _write(
        tmp_path / "cli-config.json",
        {MARKER: "1", "OPENAI_API_KEY": _SECRET, "LLM_API_BASE": "https://b"},
    )
    store = _store(tmp_path)
    assert store.saved_key_present() is True
    assert store.saved_base() == "https://b"


# --- NFR-1: the key never leaks (BR-12) -------------------------------------


def test_key_never_appears_in_any_output(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:

    caplog.set_level(logging.DEBUG, logger="strix.interface.viewer.llm_settings")
    store = _store(tmp_path, resolver=_resolver({"host.example.com": ["93.184.216.34"]}))

    save_view = store.apply(
        {
            "model": "anthropic/claude-sonnet-4-5",
            "api_key": _SECRET,
            "api_base": "https://host.example.com",
        }
    )
    get_view = store.view(editable=True)
    reset_view_before = store.view(editable=True)

    # Also exercise a failing apply (base change without key) for its message.
    error_text = ""
    try:
        store.apply({"api_base": "http://localhost:11434"})
    except LaunchError as exc:
        error_text = exc.message + json.dumps(exc.to_payload())

    blobs = [
        json.dumps(save_view),
        json.dumps(get_view),
        json.dumps(reset_view_before),
        error_text,
        caplog.text,
    ]
    for blob in blobs:
        assert _SECRET not in blob
        assert "RECOGNISABLE-SECRET" not in blob
    # Sanity: the key really is in the file (so the assertions mean something).
    assert _SECRET in (tmp_path / "cli-config.json").read_text(encoding="utf-8")
