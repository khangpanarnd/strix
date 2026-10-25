"""Tests for the viewer LLM *profiles* store (``llm_profiles.ProfileStore``).

The profiles store composes a real :class:`SettingsStore` on a temp config path
with an injected resolver and environ, so no real DNS, network or home-directory
file is touched. A recognisable key token is asserted never to appear in any
returned dict, log record or exception text.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

import pytest

from strix.interface.viewer.launch_errors import LaunchError
from strix.interface.viewer.llm_profiles import (
    ACTIVE_VAR,
    MAX_PROFILES,
    PRESETS,
    PROFILE_ENTRY_NAMES,
    PROFILES_VAR,
    ProfileStore,
)
from strix.interface.viewer.llm_settings import MARKER, STRIP_SET, SettingsStore


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


_SECRET = "sk-RECOGNISABLE-SECRET-0123456789"  # noqa: S105 - test-only fake secret
_SECRET_TWO = "sk-RECOGNISABLE-SECRET-TWO-987654"  # noqa: S105 - test-only fake secret


def _resolver(mapping: dict[str, list[str]] | None = None) -> Callable[[str, float], list[str]]:
    table = mapping or {}

    def resolve(host: str, _timeout: float) -> list[str]:
        if host in table:
            return table[host]
        # A public address so https bases validate without real DNS.
        return ["93.184.216.34"]

    return resolve


def _store(
    tmp_path: Path,
    *,
    environ: dict[str, str] | None = None,
    resolver: Callable[[str, float], list[str]] | None = None,
) -> ProfileStore:
    settings = SettingsStore(
        tmp_path / "cli-config.json",
        environ=environ or {},
        resolver=resolver or _resolver(),
        platform="linux",
    )
    return ProfileStore(settings)


def _write(path: Path, env: dict[str, Any], *, extra_top: dict[str, Any] | None = None) -> None:
    data: dict[str, Any] = {"env": env}
    if extra_top:
        data.update(extra_top)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _env(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))["env"]


def _profiles(path: Path) -> list[dict[str, Any]]:
    return json.loads(_env(path)[PROFILES_VAR])


# --- constants / presets ----------------------------------------------------


def test_profile_entry_names_match_settings() -> None:
    assert frozenset({PROFILES_VAR, ACTIVE_VAR}) == PROFILE_ENTRY_NAMES


def test_presets_are_static_and_well_formed() -> None:
    ids = [p["id"] for p in PRESETS]
    assert "openai" in ids and "custom" in ids and "ollama" in ids
    assert len(ids) == len(set(ids))
    ollama = next(p for p in PRESETS if p["id"] == "ollama")
    assert ollama["needs_key"] is False
    assert ollama["default_base"] == "http://localhost:11434"


# --- view: empty / migration ------------------------------------------------


def test_view_empty_when_no_file(tmp_path: Path) -> None:
    view = _store(tmp_path).view(editable=True)
    assert view["profiles"] == []
    assert view["active_id"] is None
    assert view["max_profiles"] == MAX_PROFILES
    assert view["managed"] is False
    assert [p["id"] for p in view["presets"]] == [p["id"] for p in PRESETS]


def test_view_migrates_managed_flat_values_to_virtual_default(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(
        tmp_path / "cli-config.json",
        {
            MARKER: "1",
            "STRIX_LLM": "anthropic/claude-sonnet-4-5",
            "LLM_API_KEY": _SECRET,
            "LLM_API_BASE": "https://api.example.com",
            "STRIX_REASONING_EFFORT": "high",
        },
    )
    view = store.view(editable=True)
    assert len(view["profiles"]) == 1
    default = view["profiles"][0]
    assert default["id"] == "default"
    assert default["name"] == "Default"
    assert default["active"] is True
    assert default["model"] == "anthropic/claude-sonnet-4-5"
    assert default["api_key_set"] is True
    assert view["active_id"] == "default"
    # The virtual default is NOT persisted on a bare GET.
    assert PROFILES_VAR not in _env(tmp_path / "cli-config.json")


def test_no_migration_when_unmanaged(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(tmp_path / "cli-config.json", {"STRIX_LLM": "openai/gpt-5"})
    view = store.view(editable=True)
    assert view["profiles"] == []
    assert view["active_id"] is None


# --- create -----------------------------------------------------------------


def test_create_mints_id_and_persists(tmp_path: Path) -> None:
    store = _store(tmp_path)
    view = store.save({"name": "Prod", "preset": "openai", "model": "openai/gpt-5"})
    assert len(view["profiles"]) == 1
    created = view["profiles"][0]
    assert re.fullmatch(r"p-[0-9a-f]{8}", created["id"])
    assert created["name"] == "Prod"
    assert created["preset"] == "openai"
    assert created["active"] is False  # a create never activates
    assert view["active_id"] is None
    stored = _profiles(tmp_path / "cli-config.json")
    assert stored[0]["model"] == "openai/gpt-5"


def test_create_requires_a_model(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(LaunchError) as exc:
        store.save({"name": "NoModel"})
    assert exc.value.code == "invalid_model"


def test_create_requires_a_name(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(LaunchError) as exc:
        store.save({"model": "openai/gpt-5"})
    assert exc.value.code == "invalid_profile_name"


# --- name rules -------------------------------------------------------------


def test_name_must_be_unique_case_insensitive(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save({"name": "Prod", "model": "openai/gpt-5"})
    with pytest.raises(LaunchError) as exc:
        store.save({"name": "prod", "model": "openai/gpt-5"})
    assert exc.value.code == "profile_name_taken"


@pytest.mark.parametrize("name", ["", "x" * 61, "has\x01control"])
def test_invalid_names_rejected(tmp_path: Path, name: str) -> None:
    store = _store(tmp_path)
    with pytest.raises(LaunchError) as exc:
        store.save({"name": name, "model": "openai/gpt-5"})
    assert exc.value.code == "invalid_profile_name"


def test_max_twenty_profiles(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(MAX_PROFILES):
        store.save({"name": f"p{i}", "model": "openai/gpt-5"})
    with pytest.raises(LaunchError) as exc:
        store.save({"name": "overflow", "model": "openai/gpt-5"})
    assert exc.value.code == "too_many_profiles"


# --- update -----------------------------------------------------------------


def test_update_leaves_omitted_fields_unchanged(tmp_path: Path) -> None:
    store = _store(tmp_path)
    view = store.save({"name": "Prod", "model": "openai/gpt-5", "api_key": _SECRET})
    pid = view["profiles"][0]["id"]
    store.save({"id": pid, "reasoning_effort": "low"})
    stored = _profiles(tmp_path / "cli-config.json")[0]
    assert stored["model"] == "openai/gpt-5"
    assert stored["api_key"] == _SECRET
    assert stored["reasoning_effort"] == "low"


def test_update_unknown_id_is_404(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(LaunchError) as exc:
        store.save({"id": "p-deadbeef", "model": "openai/gpt-5"})
    assert exc.value.code == "profile_not_found"
    assert exc.value.status == 404


def test_update_clear_removes_field(tmp_path: Path) -> None:
    store = _store(tmp_path)
    view = store.save(
        {
            "name": "Prod",
            "model": "openai/gpt-5",
            "api_base": "https://api.example.com",
            "api_key": _SECRET,
        }
    )
    pid = view["profiles"][0]["id"]
    # Clearing the key lets the base change without re-entering it.
    store.save({"id": pid, "clear": ["api_key", "api_base"]})
    stored = _profiles(tmp_path / "cli-config.json")[0]
    assert "api_key" not in stored
    assert "api_base" not in stored


# --- base-change rule (per profile) -----------------------------------------


def test_base_change_rule_blocks_without_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    view = store.save(
        {
            "name": "Prod",
            "model": "openai/gpt-5",
            "api_base": "https://api.example.com",
            "api_key": _SECRET,
        }
    )
    pid = view["profiles"][0]["id"]
    with pytest.raises(LaunchError) as exc:
        store.save({"id": pid, "api_base": "https://other.example.com"})
    assert exc.value.code == "key_required_for_base_change"


def test_base_change_rule_allows_with_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    view = store.save(
        {
            "name": "Prod",
            "model": "openai/gpt-5",
            "api_base": "https://api.example.com",
            "api_key": _SECRET,
        }
    )
    pid = view["profiles"][0]["id"]
    store.save({"id": pid, "api_base": "https://other.example.com", "api_key": _SECRET_TWO})
    stored = _profiles(tmp_path / "cli-config.json")[0]
    assert stored["api_base"] == "https://other.example.com"


def test_base_change_rule_is_per_profile(tmp_path: Path) -> None:
    # A second profile with no key may set a base freely.
    store = _store(tmp_path)
    store.save({"name": "WithKey", "model": "openai/gpt-5", "api_key": _SECRET})
    view = store.save({"name": "NoKey", "model": "openai/gpt-5"})
    pid = next(p["id"] for p in view["profiles"] if p["name"] == "NoKey")
    store.save({"id": pid, "api_base": "https://api.example.com"})
    stored = next(p for p in _profiles(tmp_path / "cli-config.json") if p["name"] == "NoKey")
    assert stored["api_base"] == "https://api.example.com"


# --- activate / mirror ------------------------------------------------------


def test_activate_mirrors_to_canonical_names(tmp_path: Path) -> None:
    store = _store(tmp_path)
    view = store.save({"name": "Prod", "model": "deepseek/deepseek-chat", "api_key": _SECRET})
    pid = view["profiles"][0]["id"]
    out = store.activate({"id": pid})
    assert out["active_id"] == pid
    env = _env(tmp_path / "cli-config.json")
    assert env["STRIX_LLM"] == "deepseek/deepseek-chat"
    assert env["LLM_API_KEY"] == _SECRET
    assert env[MARKER] == "1"
    assert env[ACTIVE_VAR] == pid


def test_activate_unknown_id_is_404(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(LaunchError) as exc:
        store.activate({"id": "p-00000000"})
    assert exc.value.code == "profile_not_found"
    assert exc.value.status == 404


def test_mirror_removes_absent_fields_and_aliases(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Seed an alias in the file to prove it is stripped on mirror.
    _write(tmp_path / "cli-config.json", {"OPENAI_API_KEY": "stale", "KEEP_ME": "yes"})
    view = store.save({"name": "Prod", "model": "openai/gpt-5"})
    pid = view["profiles"][0]["id"]
    store.activate({"id": pid})
    env = _env(tmp_path / "cli-config.json")
    assert "OPENAI_API_KEY" not in env  # alias stripped
    assert "LLM_API_KEY" not in env  # profile has no key -> absent field removed
    assert "LLM_API_BASE" not in env
    assert env["KEEP_ME"] == "yes"  # unrelated entry preserved


def test_editing_the_active_profile_remirrors(tmp_path: Path) -> None:
    store = _store(tmp_path)
    view = store.save({"name": "Prod", "model": "openai/gpt-5"})
    pid = view["profiles"][0]["id"]
    store.activate({"id": pid})
    store.save({"id": pid, "model": "anthropic/claude-sonnet-4-5"})
    env = _env(tmp_path / "cli-config.json")
    assert env["STRIX_LLM"] == "anthropic/claude-sonnet-4-5"


# --- flat PUT keeps the active profile in sync ------------------------------


def test_flat_put_syncs_active_profile(tmp_path: Path) -> None:
    store = _store(tmp_path)
    view = store.save({"name": "Prod", "model": "openai/gpt-5"})
    pid = view["profiles"][0]["id"]
    store.activate({"id": pid})
    # A flat PUT through the underlying settings store.
    store._settings.apply({"model": "gemini/gemini-2.5-pro"})
    env = _env(tmp_path / "cli-config.json")
    assert env["STRIX_LLM"] == "gemini/gemini-2.5-pro"
    stored = next(p for p in _profiles(tmp_path / "cli-config.json") if p["id"] == pid)
    assert stored["model"] == "gemini/gemini-2.5-pro"  # list and mirror agree


# --- migration persistence on first write -----------------------------------


def test_migration_persists_on_first_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write(
        tmp_path / "cli-config.json",
        {MARKER: "1", "STRIX_LLM": "openai/gpt-5", "LLM_API_KEY": _SECRET},
    )
    # Creating a new profile persists the virtual default alongside it.
    store.save({"name": "Second", "model": "anthropic/claude-sonnet-4-5"})
    stored = _profiles(tmp_path / "cli-config.json")
    names = {p["name"] for p in stored}
    assert names == {"Default", "Second"}
    default = next(p for p in stored if p["id"] == "default")
    assert default["model"] == "openai/gpt-5"
    assert default["api_key"] == _SECRET


# --- delete -----------------------------------------------------------------


def test_delete_removes_profile(tmp_path: Path) -> None:
    store = _store(tmp_path)
    view = store.save({"name": "Prod", "model": "openai/gpt-5"})
    pid = view["profiles"][0]["id"]
    out = store.delete({"id": pid})
    assert out["profiles"] == []


def test_cannot_delete_active_profile(tmp_path: Path) -> None:
    store = _store(tmp_path)
    view = store.save({"name": "Prod", "model": "openai/gpt-5"})
    pid = view["profiles"][0]["id"]
    store.activate({"id": pid})
    with pytest.raises(LaunchError) as exc:
        store.delete({"id": pid})
    assert exc.value.code == "cannot_delete_active_profile"
    assert exc.value.status == 409


def test_delete_unknown_id_is_404(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(LaunchError) as exc:
        store.delete({"id": "p-00000000"})
    assert exc.value.code == "profile_not_found"


# --- reset removes everything -----------------------------------------------


def test_reset_removes_profile_entries_and_marker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    view = store.save({"name": "Prod", "model": "openai/gpt-5", "api_key": _SECRET})
    pid = view["profiles"][0]["id"]
    store.activate({"id": pid})
    _write(  # add an unrelated entry to prove it survives
        tmp_path / "cli-config.json",
        {**_env(tmp_path / "cli-config.json"), "UNRELATED": "keep"},
    )
    store._settings.reset(confirm=True)
    env = _env(tmp_path / "cli-config.json")
    assert PROFILES_VAR not in env
    assert ACTIVE_VAR not in env
    assert MARKER not in env
    assert all(name not in env for name in STRIP_SET)
    assert env["UNRELATED"] == "keep"


# --- corrupt file / preservation --------------------------------------------


def test_corrupt_file_is_untouched(tmp_path: Path) -> None:
    config = tmp_path / "cli-config.json"
    config.write_text("{ not json", encoding="utf-8")
    store = _store(tmp_path)
    with pytest.raises(LaunchError) as exc:
        store.save({"name": "Prod", "model": "openai/gpt-5"})
    assert exc.value.code == "config_unreadable"
    assert exc.value.status == 409
    assert config.read_text(encoding="utf-8") == "{ not json"


def test_corrupt_profiles_entry_is_config_unreadable(tmp_path: Path) -> None:
    _write(tmp_path / "cli-config.json", {PROFILES_VAR: "not-a-json-list"})
    store = _store(tmp_path)
    with pytest.raises(LaunchError) as exc:
        store.view(editable=True)
    assert exc.value.code == "config_unreadable"


def test_other_entries_and_top_level_keys_preserved(tmp_path: Path) -> None:
    _write(
        tmp_path / "cli-config.json",
        {"UNRELATED": "keep"},
        extra_top={"other_top": {"nested": 1}},
    )
    store = _store(tmp_path)
    store.save({"name": "Prod", "model": "openai/gpt-5"})
    data = json.loads((tmp_path / "cli-config.json").read_text(encoding="utf-8"))
    assert data["env"]["UNRELATED"] == "keep"
    assert data["other_top"] == {"nested": 1}


# --- case-insensitivity -----------------------------------------------------


def test_lookups_are_case_insensitive(tmp_path: Path) -> None:
    store = _store(tmp_path)
    view = store.save({"name": "Prod", "model": "openai/gpt-5"})
    pid = view["profiles"][0]["id"]
    # Activation and lookup by upper-cased id both work.
    out = store.activate({"id": pid.upper()})
    assert out["active_id"] == pid


# --- child_env uses the active profile --------------------------------------


def test_child_env_strips_active_profile_provider_var(tmp_path: Path) -> None:
    # Activate a deepseek profile; the derived DEEPSEEK_API_KEY must be stripped.
    store = _store(tmp_path, environ={"DEEPSEEK_API_KEY": "shell", "KEEP": "yes"})
    view = store.save({"name": "DS", "model": "deepseek/deepseek-chat", "api_key": _SECRET})
    pid = view["profiles"][0]["id"]
    store.activate({"id": pid})
    base = dict.fromkeys(STRIP_SET, "x")
    base.update({"DEEPSEEK_API_KEY": "shell", "KEEP": "yes"})
    env = store._settings.child_env(base)
    assert all(name not in env for name in STRIP_SET)
    assert "DEEPSEEK_API_KEY" not in env  # derived provider var stripped
    assert env["KEEP"] == "yes"


def test_child_env_follows_reactivation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = store.save({"name": "A", "model": "zai/glm-4.6", "api_key": _SECRET})["profiles"][0]["id"]
    b = store.save({"name": "B", "model": "dashscope/qwen-max", "api_key": _SECRET_TWO})
    bid = next(p["id"] for p in b["profiles"] if p["name"] == "B")
    store.activate({"id": a})
    base = {"ZAI_API_KEY": "x", "DASHSCOPE_API_KEY": "y", "KEEP": "z"}
    env = store._settings.child_env(base)
    assert "ZAI_API_KEY" not in env  # active = A (zai)
    store.activate({"id": bid})
    env = store._settings.child_env(base)
    assert "DASHSCOPE_API_KEY" not in env  # active = B (dashscope)


# --- no key leak ------------------------------------------------------------


def test_key_never_in_returned_views(tmp_path: Path) -> None:
    store = _store(tmp_path)
    view = store.save({"name": "Prod", "model": "openai/gpt-5", "api_key": _SECRET})
    pid = view["profiles"][0]["id"]
    for out in (view, store.activate({"id": pid}), store.view(editable=True)):
        assert _SECRET not in json.dumps(out)
        assert out["profiles"][0]["api_key_set"] is True


def test_key_never_in_logs_or_exceptions(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = _store(tmp_path)
    with caplog.at_level(logging.DEBUG):
        view = store.save({"name": "Prod", "model": "openai/gpt-5", "api_key": _SECRET})
        pid = view["profiles"][0]["id"]
        store.activate({"id": pid})
        # Trigger the base-change error with a saved key.
        store.save(
            {
                "id": pid,
                "api_base": "https://api.example.com",
                "api_key": _SECRET,
            }
        )
        with pytest.raises(LaunchError) as exc:
            store.save({"id": pid, "api_base": "https://other.example.com"})
    assert _SECRET not in str(exc.value)
    assert _SECRET not in caplog.text
