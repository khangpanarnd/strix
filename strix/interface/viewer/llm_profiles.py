"""Viewer LLM *profiles* store — several named profiles, exactly one active.

This extends the single-settings feature (see ``llm_settings.SettingsStore``)
without changing it. ``ProfileStore`` *composes* a ``SettingsStore``: it reuses
that store's process-wide lock, its file read/write helpers, its field
validators and its mirror/strip machinery, so the CLI, ``child_env``,
``pre_launch_check`` and the launch path need no change (``llm-profiles-spec``).

Storage lives in the same ``~/.strix/cli-config.json`` ``env`` block:

* ``STRIX_VIEWER_LLM_PROFILES`` — a JSON **string** holding a list of up to
  twenty ``{id, name, preset, model, api_key?, api_base?, reasoning_effort?}``
  objects. ``id`` is ``p-`` + 8 hex (server-generated); ``name`` is unique
  case-insensitively, 1..60 chars, no control characters.
* ``STRIX_VIEWER_LLM_ACTIVE`` — the active profile id.

The ACTIVE profile is *mirrored* into the four canonical names exactly as the
single-settings code does (aliases removed, marker set, absent fields removed),
so everything downstream keeps working unchanged. A GET with no stored list but
managed flat values synthesises a virtual ``default`` profile from the flat
values (migration); the first profile write persists it.

Lock order (BR-35, unchanged): the store's lock is the inner lock. Every
public method here validates (and DNS-resolves the base) OUTSIDE the lock, then
does the read-modify-write under ``SettingsStore._lock``.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from strix.interface.viewer.launch_errors import LaunchError
from strix.interface.viewer.llm_settings import (
    MARKER,
    SettingsStore,
    provider_key_var,
)
from strix.interface.viewer.llm_settings_validation import (
    model_warnings,
    validate_api_base,
    validate_api_key,
    validate_model,
    validate_reasoning_effort,
)
from strix.utils.secret_files import write_secret_text


if TYPE_CHECKING:
    from collections.abc import Mapping


logger = logging.getLogger(__name__)


# --- constants (public contract) --------------------------------------------

#: The env name holding the JSON-string list of profiles (upper-case).
PROFILES_VAR = "STRIX_VIEWER_LLM_PROFILES"

#: The env name holding the active profile id (upper-case).
ACTIVE_VAR = "STRIX_VIEWER_LLM_ACTIVE"

#: The two new entries this store owns, removed on reset alongside the marker.
PROFILE_ENTRY_NAMES: frozenset[str] = frozenset({PROFILES_VAR, ACTIVE_VAR})

#: Upper limit on stored profiles (spec: at most twenty).
MAX_PROFILES = 20

#: Profile-name length bounds and the id format.
_NAME_MIN_LEN = 1
_NAME_MAX_LEN = 60
_ID_PREFIX = "p-"
_ID_HEX_CHARS = 8

#: The virtual migration profile id/name/preset (synthesised, never stored
#: until the first write persists it).
_DEFAULT_ID = "default"
_DEFAULT_NAME = "Default"
_DEFAULT_PRESET = "custom"

#: The per-profile logical fields that map to canonical names on mirror.
_PROFILE_FIELDS: tuple[str, ...] = ("model", "api_base", "reasoning_effort", "api_key")

#: Static preset catalogue returned by GET. Each preset only pre-fills the
#: form; stored data keeps just ``preset`` as a label. ``default_base`` is a
#: hint and may be null.
PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "openai",
        "label": "OpenAI",
        "model_prefix": "",
        "example_models": ["gpt-5", "gpt-5-mini"],
        "default_base": None,
        "needs_key": True,
        "note": None,
    },
    {
        "id": "anthropic",
        "label": "Anthropic",
        "model_prefix": "anthropic/",
        "example_models": ["claude-sonnet-4-5", "claude-opus-4-1"],
        "default_base": None,
        "needs_key": True,
        "note": None,
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "model_prefix": "deepseek/",
        "example_models": ["deepseek-chat", "deepseek-reasoner"],
        "default_base": None,
        "needs_key": True,
        "note": None,
    },
    {
        "id": "glm",
        "label": "GLM (Zhipu)",
        "model_prefix": "zai/",
        "example_models": ["glm-4.6", "glm-4.5"],
        "default_base": None,
        "needs_key": True,
        "note": "China users set base https://open.bigmodel.cn/api/paas/v4",
    },
    {
        "id": "qwen",
        "label": "Qwen (DashScope)",
        "model_prefix": "dashscope/",
        "example_models": ["qwen-max", "qwen-plus", "qwen3-coder-plus"],
        "default_base": None,
        "needs_key": True,
        "note": ("international base https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    },
    {
        "id": "gemini",
        "label": "Gemini",
        "model_prefix": "gemini/",
        "example_models": [],
        "default_base": None,
        "needs_key": True,
        "note": None,
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "model_prefix": "openrouter/",
        "example_models": [],
        "default_base": None,
        "needs_key": True,
        "note": None,
    },
    {
        "id": "moonshot",
        "label": "Moonshot",
        "model_prefix": "moonshot/",
        "example_models": ["kimi-k2"],
        "default_base": None,
        "needs_key": True,
        "note": None,
    },
    {
        "id": "ollama",
        "label": "Ollama",
        "model_prefix": "ollama/",
        "example_models": [],
        "default_base": "http://localhost:11434",
        "needs_key": False,
        "note": None,
    },
    {
        "id": "custom",
        "label": "Custom (OpenAI-compatible)",
        "model_prefix": "openai/",
        "example_models": [],
        "default_base": None,
        "needs_key": False,
        "note": "any OpenAI-compatible server; base required",
    },
)

_PRESET_IDS: frozenset[str] = frozenset(p["id"] for p in PRESETS)


class ProfileStore:
    """Named-profile layer over a single :class:`SettingsStore`.

    The settings store is injected and owns the lock, the file read/write
    helpers and the validators. This class adds only the profiles list, the
    active id and the mirror of the active profile to the canonical names.
    """

    def __init__(self, settings: SettingsStore) -> None:
        self._settings = settings

    # --- raw list access (callers hold the lock) ---------------------------

    @staticmethod
    def _parse_profiles(block: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return the stored profile list, or raise ``config_unreadable``.

        The entry is a JSON **string**; a missing entry yields ``[]``. Any
        malformed JSON, a non-list payload or a non-object element is treated
        as an unreadable config so the file is left untouched (409).
        """
        raw = block.get(PROFILES_VAR)
        if raw is None:
            return []
        if not isinstance(raw, str):
            raise LaunchError(
                "config_unreadable",
                "the profiles entry is not a JSON string; the file was left unchanged",
                status=409,
            )
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise LaunchError(
                "config_unreadable",
                "the profiles entry is not valid JSON; the file was left unchanged",
                status=409,
            ) from exc
        if not isinstance(parsed, list) or any(not isinstance(p, dict) for p in parsed):
            raise LaunchError(
                "config_unreadable",
                "the profiles entry is not a list of objects; the file was left unchanged",
                status=409,
            )
        return [dict(p) for p in parsed]

    @staticmethod
    def _active_id(block: Mapping[str, Any]) -> str | None:
        value = block.get(ACTIVE_VAR)
        return str(value) if value is not None else None

    @staticmethod
    def _find(profiles: list[dict[str, Any]], profile_id: str) -> dict[str, Any] | None:
        """Case-insensitive id lookup."""
        wanted = profile_id.lower()
        for profile in profiles:
            if str(profile.get("id", "")).lower() == wanted:
                return profile
        return None

    # --- migration ----------------------------------------------------------

    def _virtual_default(self, block: Mapping[str, Any]) -> dict[str, Any] | None:
        """Synthesise the virtual ``default`` profile from managed flat values.

        Returns ``None`` unless the file is managed (marker set) and at least
        one canonical field is present. The key is read only as a presence flag
        (``api_key`` stored as a sentinel so ``api_key_set`` reports true); the
        value itself is never surfaced.
        """
        if block.get(MARKER) != "1":
            return None
        fields = self._settings.canonical_fields(block)
        if not any(fields.get(f) is not None for f in _PROFILE_FIELDS):
            return None
        profile: dict[str, Any] = {
            "id": _DEFAULT_ID,
            "name": _DEFAULT_NAME,
            "preset": _DEFAULT_PRESET,
        }
        profile.update({f: fields[f] for f in _PROFILE_FIELDS if fields.get(f) is not None})
        return profile

    def _effective_profiles(
        self, block: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return (profiles, active_id), applying the migration synthesis.

        When the stored list is empty but a virtual default can be synthesised,
        the list is the single virtual default and it is active. Otherwise the
        stored list and stored active id are returned as-is.
        """
        profiles = self._parse_profiles(block)
        if profiles:
            return profiles, self._active_id(block)
        virtual = self._virtual_default(block)
        if virtual is not None:
            return [virtual], _DEFAULT_ID
        return [], None

    # --- view (GET /api/settings/profiles) ----------------------------------

    def view(self, *, editable: bool, scan_active: bool = False) -> dict[str, Any]:
        """Return the GET /api/settings/profiles JSON shape (spec).

        The lock covers only the file read so one view never mixes two file
        versions. The API key is reported as ``api_key_set`` only — never a
        value. The flat single-settings fields are kept via ``settings.view``.
        """
        flat = self._settings.view(editable=editable, scan_active=scan_active)
        with self._settings._lock:
            block, file_status = self._settings._read_env_block()
        if file_status == "unreadable":
            raise LaunchError(
                "config_unreadable",
                "the config file is not valid JSON; it was left unchanged",
                status=409,
            )
        profiles, active_id = self._effective_profiles(block)
        return {
            "profiles": [self._public(p, active_id) for p in profiles],
            "active_id": active_id,
            "presets": [dict(p) for p in PRESETS],
            "max_profiles": MAX_PROFILES,
            "managed": flat["managed"],
            "editable": editable,
            "file_status": file_status,
            "config_path": flat["config_path"],
            "platform": flat["platform"],
            "scan_active": scan_active,
            "reasoning_choices": flat["reasoning_choices"],
            "model_suggestions": flat["model_suggestions"],
        }

    @staticmethod
    def _public(profile: Mapping[str, Any], active_id: str | None) -> dict[str, Any]:
        """Public projection of one profile (never the key value)."""
        return {
            "id": profile.get("id"),
            "name": profile.get("name"),
            "preset": profile.get("preset"),
            "model": profile.get("model"),
            "api_base": profile.get("api_base"),
            "reasoning_effort": profile.get("reasoning_effort"),
            "api_key_set": profile.get("api_key") is not None,
            "active": active_id is not None
            and str(profile.get("id", "")).lower() == active_id.lower(),
        }

    # --- save (POST /api/settings/profiles/save) ----------------------------

    def save(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Create or update a profile; return the view plus ``warnings``.

        No ``id`` creates (key optional); with ``id`` updates (omitted fields
        unchanged). The base-change rule (BR-13 analogue) applies per profile.
        Saving does NOT activate a new profile; saving the ACTIVE profile
        re-mirrors it. The first-save rule means creating the very first profile
        does not copy a shell-only key — the user types it.
        """
        parsed = self._parse_save_body(body)

        with self._settings._lock:
            data, block, file_status = self._settings._read_full()
            if file_status == "unreadable":
                raise LaunchError(
                    "config_unreadable",
                    "the config file is not valid JSON; it was left unchanged",
                    status=409,
                )
            profiles, active_id = self._effective_profiles(block)

            if parsed["id"] is None:
                profile = self._create(profiles, parsed)
                profiles.append(profile)
            else:
                profile = self._update(profiles, parsed)

            new_block = self._persist(block, profiles, active_id)
            self._write(data, new_block)
            self._audit("save", profile)

        warnings = self._warnings_for(profile)
        result = self.view(editable=True)
        result["warnings"] = warnings
        return result

    def _parse_save_body(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Validate the save body shape and every provided field (outside lock)."""
        if not isinstance(body, dict):
            raise LaunchError("invalid_json", "request body must be a JSON object", status=400)
        allowed = {
            "id",
            "name",
            "preset",
            "model",
            "api_key",
            "api_base",
            "reasoning_effort",
            "clear",
        }
        for key in body:
            if key not in allowed:
                raise LaunchError("unknown_field", f"unknown field: {key}", status=400)

        profile_id = body.get("id")
        if profile_id is not None and not isinstance(profile_id, str):
            raise LaunchError("invalid_json", "id must be a string", status=400)

        preset = body.get("preset")
        if preset is not None and (not isinstance(preset, str) or preset not in _PRESET_IDS):
            raise LaunchError("invalid_json", "unknown preset", status=400)

        # ``name`` is required on create; on update it may be omitted (unchanged).
        name = body.get("name")
        validated_name = self._validate_name(name) if name is not None else None
        if profile_id is None and validated_name is None:
            raise LaunchError("invalid_profile_name", "a profile name is required", status=400)

        clear = self._parse_clear(body)
        provided = self._validate_provided(body)

        conflict = set(provided) & set(clear)
        if conflict:
            raise LaunchError(
                "conflicting_fields",
                f"field both set and cleared: {', '.join(sorted(conflict))}",
                status=400,
            )

        # A create requires a model (there is nothing to inherit).
        if profile_id is None and "model" not in provided:
            raise LaunchError(
                "invalid_model", "a model is required to create a profile", status=400
            )

        return {
            "id": profile_id,
            "name": validated_name,
            "preset": preset,
            "provided": provided,
            "clear": tuple(clear),
        }

    @staticmethod
    def _parse_clear(body: Mapping[str, Any]) -> list[str]:
        """Validate the ``clear`` list; only clearable fields are allowed."""
        raw_clear = body.get("clear", [])
        if not isinstance(raw_clear, list) or any(not isinstance(c, str) for c in raw_clear):
            raise LaunchError("invalid_json", "clear must be a list of field names", status=400)
        clear: list[str] = []
        for field in raw_clear:
            if field not in ("api_key", "api_base", "reasoning_effort"):
                raise LaunchError("unknown_field", f"cannot clear field: {field}", status=400)
            clear.append(field)
        return clear

    def _validate_provided(self, body: Mapping[str, Any]) -> dict[str, str]:
        """Syntax-validate every provided field (and DNS-resolve the base)."""
        provided: dict[str, str] = {}
        if "model" in body:
            provided["model"] = validate_model(body["model"])
        if "reasoning_effort" in body:
            provided["reasoning_effort"] = validate_reasoning_effort(body["reasoning_effort"])
        if "api_key" in body:
            provided["api_key"] = validate_api_key(body["api_key"])
        if "api_base" in body:
            provided["api_base"] = validate_api_base(
                body["api_base"], resolver=self._settings._resolver
            )
        return provided

    @staticmethod
    def _validate_name(raw: object) -> str:
        """Return the trimmed name, or raise ``invalid_profile_name``."""
        if not isinstance(raw, str):
            raise LaunchError("invalid_profile_name", "name must be a string", status=400)
        name = raw.strip()
        if not _NAME_MIN_LEN <= len(name) <= _NAME_MAX_LEN:
            raise LaunchError("invalid_profile_name", "name must be 1 to 60 characters", status=400)
        if any(ord(ch) <= 0x1F or 0x7F <= ord(ch) <= 0x9F for ch in name):
            raise LaunchError(
                "invalid_profile_name", "name must not contain control characters", status=400
            )
        return name

    def _create(self, profiles: list[dict[str, Any]], parsed: dict[str, Any]) -> dict[str, Any]:
        """Build a new profile dict (id minted here); enforce name/count rules."""
        if len(profiles) >= MAX_PROFILES:
            raise LaunchError(
                "too_many_profiles", f"at most {MAX_PROFILES} profiles are allowed", status=400
            )
        name: str = parsed["name"]
        self._enforce_unique_name(profiles, name, exclude_id=None)
        profile: dict[str, Any] = {
            "id": self._mint_id(profiles),
            "name": name,
            "preset": parsed["preset"] or _DEFAULT_PRESET,
        }
        # A create never inherits from the shell (first-save rule): the fields
        # are only what the request provides.
        profile.update(parsed["provided"])
        return profile

    def _update(self, profiles: list[dict[str, Any]], parsed: dict[str, Any]) -> dict[str, Any]:
        """Update an existing profile in place; enforce the base-change rule."""
        profile = self._find(profiles, parsed["id"])
        if profile is None:
            raise LaunchError("profile_not_found", "no profile with that id", status=404)

        if parsed["name"] is not None:
            self._enforce_unique_name(profiles, parsed["name"], exclude_id=profile["id"])
            profile["name"] = parsed["name"]
        if parsed["preset"] is not None:
            profile["preset"] = parsed["preset"]

        provided = parsed["provided"]
        clear = parsed["clear"]
        self._enforce_base_change_rule(profile, provided, clear)

        for field, value in provided.items():
            profile[field] = value
        for field in clear:
            profile.pop(field, None)
        return profile

    @staticmethod
    def _enforce_base_change_rule(
        profile: Mapping[str, Any], provided: Mapping[str, str], clear: tuple[str, ...]
    ) -> None:
        """Per-profile BR-13: changing the base while a key is saved needs the key."""
        if profile.get("api_key") is None or "api_key" in clear:
            return
        saved_base = profile.get("api_base")
        base_set = "api_base" in provided and provided["api_base"] != saved_base
        base_cleared = "api_base" in clear and saved_base is not None
        if (base_set or base_cleared) and "api_key" not in provided:
            raise LaunchError(
                "key_required_for_base_change",
                "changing the API base requires re-entering the API key",
                status=400,
            )

    @staticmethod
    def _enforce_unique_name(
        profiles: list[dict[str, Any]], name: str, *, exclude_id: str | None
    ) -> None:
        wanted = name.lower()
        exclude = exclude_id.lower() if exclude_id else None
        for profile in profiles:
            if exclude is not None and str(profile.get("id", "")).lower() == exclude:
                continue
            if str(profile.get("name", "")).lower() == wanted:
                raise LaunchError(
                    "profile_name_taken", "a profile with that name already exists", status=400
                )

    @staticmethod
    def _mint_id(profiles: list[dict[str, Any]]) -> str:
        existing = {str(p.get("id", "")).lower() for p in profiles}
        while True:
            candidate = _ID_PREFIX + secrets.token_hex(_ID_HEX_CHARS // 2)
            if candidate.lower() not in existing:
                return candidate

    # --- activate (POST /api/settings/profiles/activate) --------------------

    def activate(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Set the active profile and re-mirror it; return the view."""
        profile_id = self._require_id(body)
        with self._settings._lock:
            data, block, file_status = self._settings._read_full()
            if file_status == "unreadable":
                raise LaunchError(
                    "config_unreadable",
                    "the config file is not valid JSON; it was left unchanged",
                    status=409,
                )
            profiles, _active = self._effective_profiles(block)
            profile = self._find(profiles, profile_id)
            if profile is None:
                raise LaunchError("profile_not_found", "no profile with that id", status=404)
            new_block = self._persist(block, profiles, profile["id"])
            self._write(data, new_block)
            self._audit("activate", profile)
        return self.view(editable=True)

    # --- delete (POST /api/settings/profiles/delete) ------------------------

    def delete(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Delete a profile; refuse the active one; return the view."""
        profile_id = self._require_id(body)
        with self._settings._lock:
            data, block, file_status = self._settings._read_full()
            if file_status == "unreadable":
                raise LaunchError(
                    "config_unreadable",
                    "the config file is not valid JSON; it was left unchanged",
                    status=409,
                )
            profiles, active_id = self._effective_profiles(block)
            profile = self._find(profiles, profile_id)
            if profile is None:
                raise LaunchError("profile_not_found", "no profile with that id", status=404)
            if active_id is not None and str(profile["id"]).lower() == active_id.lower():
                raise LaunchError(
                    "cannot_delete_active_profile",
                    "the active profile cannot be deleted",
                    status=409,
                )
            remaining = [p for p in profiles if str(p["id"]).lower() != str(profile["id"]).lower()]
            new_block = self._persist(block, remaining, active_id)
            self._write(data, new_block)
            self._audit("delete", profile)
        return self.view(editable=True)

    @staticmethod
    def _require_id(body: Mapping[str, Any]) -> str:
        if not isinstance(body, dict):
            raise LaunchError("invalid_json", "request body must be a JSON object", status=400)
        profile_id = body.get("id")
        if not isinstance(profile_id, str) or not profile_id:
            raise LaunchError("invalid_json", "id is required", status=400)
        return profile_id

    # --- connection test support --------------------------------------------

    def effective_test_values(self, body: Mapping[str, Any]) -> dict[str, str | None]:
        """Resolve the four test values, optionally against a stored ``profile_id``.

        Without ``profile_id`` this defers to the flat single-settings resolver
        (mirrored active values). With ``profile_id`` the profile's saved values
        are the base; typed fields override; the key/base rule applies against
        THAT profile's saved base.
        """
        if not isinstance(body, dict):
            raise LaunchError("invalid_json", "request body must be a JSON object", status=400)
        profile_id = body.get("profile_id")
        if profile_id is None:
            return self._settings.effective_test_values(body)
        if not isinstance(profile_id, str):
            raise LaunchError("invalid_json", "profile_id must be a string", status=400)

        rest = {k: v for k, v in body.items() if k != "profile_id"}
        typed_model = validate_model(rest["model"]) if "model" in rest else None
        typed_reasoning = (
            validate_reasoning_effort(rest["reasoning_effort"])
            if "reasoning_effort" in rest
            else None
        )
        typed_key = validate_api_key(rest["api_key"]) if "api_key" in rest else None
        typed_base = (
            validate_api_base(rest["api_base"], resolver=self._settings._resolver)
            if "api_base" in rest
            else None
        )

        with self._settings._lock:
            block, file_status = self._settings._read_env_block()
            if file_status == "unreadable":
                raise LaunchError(
                    "config_unreadable",
                    "the config file is not valid JSON; it was left unchanged",
                    status=409,
                )
            profiles, _active = self._effective_profiles(block)
            profile = self._find(profiles, profile_id)
            if profile is None:
                raise LaunchError("profile_not_found", "no profile with that id", status=404)
            saved_model = profile.get("model")
            saved_key = profile.get("api_key")
            saved_base = profile.get("api_base")
            saved_reasoning = profile.get("reasoning_effort")

        effective_base = typed_base if typed_base is not None else saved_base
        if typed_key is not None:
            effective_key: str | None = typed_key
        else:
            if effective_base != saved_base:
                raise LaunchError(
                    "key_required_for_base_change",
                    "testing a different API base requires entering the API key",
                    status=400,
                )
            effective_key = saved_key

        return {
            "model": typed_model if typed_model is not None else saved_model,
            "api_key": effective_key,
            "api_base": effective_base,
            "reasoning_effort": (
                typed_reasoning if typed_reasoning is not None else saved_reasoning
            ),
        }

    # --- persistence helpers (called under the lock) ------------------------

    def _persist(
        self, block: Mapping[str, Any], profiles: list[dict[str, Any]], active_id: str | None
    ) -> dict[str, Any]:
        """Build the new env block: the list entry, the active id, and the mirror.

        Every other entry and top-level key is preserved. The active profile is
        mirrored into the canonical names (aliases removed, marker set, absent
        fields removed) exactly as the single-settings store does; when there is
        no active profile the mirror is left as-is.
        """
        new_block = dict(block)
        new_block[PROFILES_VAR] = json.dumps(profiles)
        active = self._find(profiles, active_id) if active_id is not None else None
        if active is not None:
            new_block[ACTIVE_VAR] = active["id"]
            fields = {f: active[f] for f in _PROFILE_FIELDS if f in active}
            new_block = self._settings.mirror_block(new_block, fields)
        else:
            new_block.pop(ACTIVE_VAR, None)
        return new_block

    def _write(self, data: Mapping[str, Any], new_block: Mapping[str, Any]) -> None:
        new_data = dict(data)
        new_data["env"] = dict(new_block)
        write_secret_text(self._settings.path, json.dumps(new_data, indent=2))

    def _warnings_for(self, profile: Mapping[str, Any]) -> list[str]:
        model = profile.get("model")
        if not model:
            return []
        return model_warnings(str(model), profile.get("api_base"))

    @staticmethod
    def _audit(action: str, profile: Mapping[str, Any]) -> None:
        """Log the action, profile id and base host only — never the key."""
        base = profile.get("api_base")
        base_host = None
        if isinstance(base, str) and base:
            try:
                base_host = (urlsplit(base).hostname or "").lower() or None
            except ValueError:
                base_host = None
        logger.info(
            "viewer llm profile %s: id=%s base_host=%s",
            action,
            profile.get("id"),
            base_host or "(none)",
        )


__all__ = [
    "ACTIVE_VAR",
    "MAX_PROFILES",
    "PRESETS",
    "PROFILES_VAR",
    "PROFILE_ENTRY_NAMES",
    "ProfileStore",
    "provider_key_var",
]
