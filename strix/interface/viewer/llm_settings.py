"""Viewer LLM settings store (FR-5..FR-18, BR-1..BR-28, BR-35, DD-1..DD-3, DD-6, DD-9).

``SettingsStore`` owns the LLM portion of ``~/.strix/cli-config.json`` — it
reads and writes only the four canonical names, their five aliases, and the
managed marker (BR-18), preserving every other ``env`` entry and every other
top-level key. It computes the effective view the page renders, applies saves,
clears and resets under one process-wide lock, and builds the environment the
scan launcher hands each child so a UI-managed launch cannot leak a shell key
(section 6 of ``business-logic-model.md``).

This module imports nothing from the scan engine (and so never pulls LiteLLM
into the viewer process — ``memory/project.md`` c3; DD-4). ``provider_key_var``
is the one derived-provider-variable strip rule (DD-3): no LiteLLM lookup.

Lock order (BR-35): the launcher's launch lock is OUTER, this store's lock is
INNER. ``child_env`` is called while the launch lock is held and takes the
store lock briefly; the store NEVER takes any launcher lock, and ``view`` reads
``scan_active`` from the launcher before it takes the store lock. The DNS
validation in ``apply`` / ``effective_test_values`` / ``pre_launch_check``
happens OUTSIDE the store lock so the 3-second DNS bound never holds it.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import sys
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from strix.interface.viewer.launch_errors import LaunchError
from strix.interface.viewer.llm_settings_validation import (
    REASONING_EFFORT_VALUES,
    model_warnings,
    validate_api_base,
    validate_api_key,
    validate_model,
    validate_reasoning_effort,
)
from strix.interface.viewer.target_validation import (
    _METADATA_HOSTS,
    _RESOLVE_TIMEOUT,
    _default_resolver,
    _is_cloud_metadata,
    _is_private_host_name,
)
from strix.utils.secret_files import write_secret_text


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    Resolver = Callable[[str, float], list[str]]


logger = logging.getLogger(__name__)


# --- constants (public contract) --------------------------------------------

#: Marker written into the ``env`` block on the first save; its presence means
#: settings are UI-managed (FR-15). Always written upper-case; never passed to
#: the connection-test child (BR-18).
MARKER = "STRIX_VIEWER_LLM_MANAGED"

#: Canonical variable names the store writes, keyed by logical field (FR-10).
CANONICAL: dict[str, str] = {
    "model": "STRIX_LLM",
    "api_key": "LLM_API_KEY",
    "api_base": "LLM_API_BASE",
    "reasoning_effort": "STRIX_REASONING_EFFORT",
}

#: Alias names removed on a save of the owning field (FR-10). Each maps to the
#: logical field it is an alias of.
ALIASES: dict[str, str] = {
    "OPENAI_API_KEY": "api_key",
    "OPENAI_API_BASE": "api_base",
    "OPENAI_BASE_URL": "api_base",
    "LITELLM_BASE_URL": "api_base",
    "OLLAMA_API_BASE": "api_base",
}

#: Every name of every field, grouped by field (canonical first, then aliases).
#: Case-insensitive lookups use the upper-cased forms here.
FIELD_NAMES: dict[str, tuple[str, ...]] = {
    field: (CANONICAL[field], *(name for name, f in ALIASES.items() if f == field))
    for field in CANONICAL
}

#: The nine strip-set names: four canonical plus five aliases (BR-25). Upper-case.
STRIP_SET: frozenset[str] = frozenset(CANONICAL.values()) | frozenset(ALIASES)

#: The two env entries the *profiles* layer owns (``llm_profiles``). Named here
#: so ``reset`` can remove them without importing the profiles module (which
#: would be a circular import). Kept in sync with ``llm_profiles`` by
#: ``tests/test_llm_profiles_store.py``.
PROFILE_ENTRY_NAMES: frozenset[str] = frozenset(
    {"STRIX_VIEWER_LLM_PROFILES", "STRIX_VIEWER_LLM_ACTIVE"}
)

#: Static suggestion list shown by the page (DD-7); suggestions only.
MODEL_SUGGESTIONS: tuple[str, ...] = (
    "openai/gpt-5",
    "openai/gpt-5-mini",
    "anthropic/claude-sonnet-4-5",
    "anthropic/claude-opus-4-1",
    "anthropic/claude-haiku-4-5",
    "gemini/gemini-2.5-pro",
    "gemini/gemini-2.5-flash",
    "deepseek/deepseek-chat",
    "deepseek/deepseek-reasoner",
    "openrouter/auto",
    "xai/grok-4",
    "ollama/llama3.3",
)

#: The seven reasoning-effort choices (FR-22), in display order.
REASONING_CHOICES: tuple[str, ...] = REASONING_EFFORT_VALUES

#: Display form of the default config path shown by the page (FR-5).
_DISPLAY_PATH = "~/.strix/cli-config.json"

#: Default reasoning effort (matches ``settings.LlmSettings`` default).
_DEFAULT_REASONING = "high"

#: Providers whose key variable does not follow ``<PROVIDER>_API_KEY`` (DD-3).
_PROVIDER_KEY_EXCEPTIONS: dict[str, str] = {
    "together_ai": "TOGETHERAI_API_KEY",
    "perplexity": "PERPLEXITYAI_API_KEY",
    "mistralai": "MISTRAL_API_KEY",
}

#: Providers whose key is already in the strip set, so no extra variable (DD-3).
_PROVIDERS_WITHOUT_EXTRA_KEY: frozenset[str] = frozenset({"openai", "chatgpt", "ollama"})

#: Prefixes LiteLLM treats as routing wrappers, skipped before the provider.
_WRAPPER_PREFIXES: tuple[str, ...] = ("litellm/", "any-llm/")

_PROVIDER_KEY_PATTERN = re.compile(r"^[A-Z0-9_]+_API_KEY$")


def provider_key_var(model: str | None) -> str | None:
    """Return the shell provider key variable to strip for ``model`` (DD-3).

    For ``provider/name`` returns ``PROVIDER_API_KEY`` (provider upper-cased,
    ``-`` -> ``_``). ``litellm/`` and ``any-llm/`` routing prefixes are stripped
    first; bare names and the providers already in the strip set (openai /
    chatgpt / ollama) return ``None``. A small exception table covers providers
    whose variable breaks the pattern. The result always matches
    ``^[A-Z0-9_]+_API_KEY$`` or is ``None`` (defence in depth; FR-16).
    """
    if not model:
        return None
    route = model.strip()
    for prefix in _WRAPPER_PREFIXES:
        if route.startswith(prefix):
            route = route[len(prefix) :]
    if "/" not in route:
        return None
    provider = route.split("/", 1)[0].lower()
    if not provider or provider in _PROVIDERS_WITHOUT_EXTRA_KEY:
        return None
    if provider in _PROVIDER_KEY_EXCEPTIONS:
        return _PROVIDER_KEY_EXCEPTIONS[provider]
    name = provider.upper().replace("-", "_") + "_API_KEY"
    if not _PROVIDER_KEY_PATTERN.match(name):
        return None
    return name


class SettingsStore:
    """Reads and writes the LLM ``env`` entries of the shared config file.

    All file access is serialised by ``_lock`` (BR-21). ``environ`` defaults to
    ``os.environ`` read at call time, not at construction, so the view always
    reflects the current shell. ``resolver`` and ``platform`` are injectable for
    tests. ``scan_active_provider`` lets the server pass the launcher's
    running-scan flag without a hard dependency on the launcher type.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        resolver: Resolver | None = None,
        platform: str | None = None,
    ) -> None:
        self.path: Path = path if path is not None else Path.home() / ".strix" / "cli-config.json"
        self._environ_override = environ
        self._resolver: Resolver = resolver or _default_resolver
        self._platform = platform if platform is not None else sys.platform
        self._lock = threading.Lock()

    # --- environment helpers ------------------------------------------------

    def _environ(self) -> Mapping[str, str]:
        """Return the effective shell environment, read at call time (contract)."""
        return self._environ_override if self._environ_override is not None else os.environ

    @staticmethod
    def _upper(mapping: Mapping[str, str]) -> dict[str, str]:
        """Upper-case every key so lookups are case-insensitive (BR-18)."""
        return {str(k).upper(): v for k, v in mapping.items()}

    # --- file access (under the lock in public methods) ---------------------

    def _read_env_block(self) -> tuple[dict[str, Any], str]:
        """Return (upper-cased env block, file_status) without taking the lock.

        ``file_status`` is ``missing`` (no file), ``unreadable`` (not valid JSON
        or unreadable) or ``ok``. An unreadable file yields ``{}`` so a view can
        still render; callers that must not write detect ``unreadable`` first.
        """
        if not self.path.exists():
            return {}, "missing"
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return {}, "unreadable"
        if not isinstance(data, dict):
            return {}, "unreadable"
        env_block = data.get("env", {})
        if not isinstance(env_block, dict):
            return {}, "unreadable"
        return {str(k).upper(): v for k, v in env_block.items()}, "ok"

    def _read_full(self) -> tuple[dict[str, Any], dict[str, Any], str]:
        """Return (full JSON object, upper-cased env block, file_status).

        The full object is kept so a save preserves every other top-level key
        (BR-18). On a missing or unreadable file the object is ``{}``.
        """
        if not self.path.exists():
            return {}, {}, "missing"
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return {}, {}, "unreadable"
        if not isinstance(data, dict):
            return {}, {}, "unreadable"
        raw_env = data.get("env", {})
        env_block = (
            {str(k).upper(): v for k, v in raw_env.items()} if isinstance(raw_env, dict) else {}
        )
        return data, env_block, "ok"

    @staticmethod
    def _first_present(block: Mapping[str, Any], names: tuple[str, ...]) -> str | None:
        """Return the value of the first of ``names`` present in ``block``."""
        for name in names:
            if name in block and block[name] is not None:
                return str(block[name])
        return None

    @staticmethod
    def _any_present(block: Mapping[str, Any], names: tuple[str, ...]) -> bool:
        return any(name in block for name in names)

    # --- view (GET /api/settings) ------------------------------------------

    def view(self, *, editable: bool, scan_active: bool = False) -> dict[str, Any]:
        """Return the GET /api/settings JSON shape (section 3; FR-5, DD-6).

        The lock covers only the file read so a single view never mixes two file
        versions (BR-21). The API key is reported as ``{set, source}`` only —
        never a value, length or hash (BR-12).
        """
        environ = self._upper(self._environ())
        with self._lock:
            block, file_status = self._read_env_block()
        managed = block.get(MARKER) == "1"

        def value_source(field: str) -> tuple[str | None, str]:
            names = FIELD_NAMES[field]
            if managed:
                saved = self._first_present(block, names)
                return (saved, "ui") if saved is not None else (None, "default")
            if self._any_present(environ, names):
                return self._first_present(environ, names), "environment"
            if self._any_present(block, names):
                return self._first_present(block, names), "file"
            return None, "default"

        model_value, model_source = value_source("model")
        base_value, base_source = value_source("api_base")
        reasoning_value, reasoning_source = value_source("reasoning_effort")
        key_value, key_source = value_source("api_key")

        if reasoning_value is None:
            # The default is surfaced as a concrete value for the select control.
            reasoning_value = _DEFAULT_REASONING

        return {
            "fields": {
                "model": {"value": model_value, "source": model_source},
                "api_base": {"value": base_value, "source": base_source},
                "reasoning_effort": {"value": reasoning_value, "source": reasoning_source},
                "api_key": {"set": key_value is not None, "source": key_source},
            },
            "managed": managed,
            "editable": editable,
            "file_status": file_status,
            "config_path": self._display_path(),
            "platform": self._platform,
            "reasoning_choices": list(REASONING_CHOICES),
            "model_suggestions": list(MODEL_SUGGESTIONS),
            "scan_active": scan_active,
        }

    def _display_path(self) -> str:
        """Return the display form of the config path (``~`` for the home dir)."""
        default = Path.home() / ".strix" / "cli-config.json"
        if self.path == default:
            return _DISPLAY_PATH
        return str(self.path)

    # --- apply (PUT /api/settings) -----------------------------------------

    def apply(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and save ``body``; return the new view plus ``warnings``.

        Order (section 4): validate field syntax and the ``clear``/set conflict,
        DNS-validate the base OUTSIDE the lock, then under the lock re-read the
        file, apply the base-change (BR-13) and first-save (BR-14) rules and the
        first-save adoption (DD-2), write atomically and set the marker. An
        invalid-JSON file raises ``config_unreadable`` (409) without touching it.
        """
        provided, clear = self._parse_apply_body(body)

        # Syntax-validate every provided field (step 3). The base is also
        # DNS-validated here, outside the lock, so the 3 s bound never holds it.
        validated: dict[str, str] = {}
        if "model" in provided:
            validated["model"] = validate_model(provided["model"])
        if "reasoning_effort" in provided:
            validated["reasoning_effort"] = validate_reasoning_effort(provided["reasoning_effort"])
        if "api_key" in provided:
            validated["api_key"] = validate_api_key(provided["api_key"])
        if "api_base" in provided:
            validated["api_base"] = validate_api_base(provided["api_base"], resolver=self._resolver)

        with self._lock:
            data, block, file_status = self._read_full()
            if file_status == "unreadable":
                raise LaunchError(
                    "config_unreadable",
                    "the config file is not valid JSON; it was left unchanged",
                    status=409,
                )
            managed = block.get(MARKER) == "1"
            environ = self._upper(self._environ())

            saved_key = self._any_present(block, FIELD_NAMES["api_key"])
            saved_base = self._first_present(block, FIELD_NAMES["api_base"])
            key_only_from_env = (
                not managed and not saved_key and self._any_present(environ, FIELD_NAMES["api_key"])
            )

            self._enforce_base_change_rule(validated, clear, saved_key, saved_base)
            self._enforce_first_save_rule(validated, key_only_from_env)

            new_block = self._build_new_block(block, managed, environ, validated, clear)
            self._sync_active_profile(new_block)
            new_data = dict(data)
            new_data["env"] = new_block
            write_secret_text(self.path, json.dumps(new_data, indent=2))

            self._audit(validated, clear)

        # Compute warnings from the now-effective values (after the lock).
        warnings = self._compute_warnings(validated)
        result = self.view(editable=True)
        result["warnings"] = warnings
        return result

    def _parse_apply_body(self, body: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Split a PUT body into (provided fields, clear tuple), checking shape.

        Unknown keys -> ``unknown_field``; wrong types -> ``invalid_json``; a
        field both set and cleared -> ``conflicting_fields`` (BR-5, BR-10).
        """
        if not isinstance(body, dict):
            raise LaunchError("invalid_json", "request body must be a JSON object", status=400)
        allowed = set(CANONICAL) | {"clear"}
        for key in body:
            if key not in allowed:
                raise LaunchError("unknown_field", f"unknown field: {key}", status=400)

        provided = {k: body[k] for k in CANONICAL if k in body}

        raw_clear = body.get("clear", [])
        if not isinstance(raw_clear, list) or any(not isinstance(c, str) for c in raw_clear):
            raise LaunchError("invalid_json", "clear must be a list of field names", status=400)
        clear: list[str] = []
        for name in raw_clear:
            if name not in CANONICAL:
                raise LaunchError(
                    "unknown_field", f"cannot clear unknown field: {name}", status=400
                )
            clear.append(name)

        conflict = set(provided) & set(clear)
        if conflict:
            raise LaunchError(
                "conflicting_fields",
                f"field both set and cleared: {', '.join(sorted(conflict))}",
                status=400,
            )
        return provided, tuple(clear)

    @staticmethod
    def _enforce_base_change_rule(
        validated: Mapping[str, str],
        clear: tuple[str, ...],
        saved_key: bool,
        saved_base: str | None,
    ) -> None:
        """BR-13: changing the base while a key is saved requires ``api_key``.

        The rule guards the saved key from being sent to a different host. It
        therefore does not apply when the same request also clears the key
        (the key is being removed, not redirected) — this is what makes BR-13's
        "allowed ... after both the key and the base were cleared" reachable.
        """
        if not saved_key or "api_key" in clear:
            return
        base_set = "api_base" in validated and validated["api_base"] != saved_base
        base_cleared = "api_base" in clear and saved_base is not None
        if (base_set or base_cleared) and "api_key" not in validated:
            raise LaunchError(
                "key_required_for_base_change",
                "changing the API base requires re-entering the API key",
                status=400,
            )

    @staticmethod
    def _enforce_first_save_rule(validated: Mapping[str, str], key_only_from_env: bool) -> None:
        """BR-14/DD-2: first managed save with a shell-only key needs ``api_key``."""
        if key_only_from_env and "api_key" not in validated:
            raise LaunchError(
                "key_required_first_save",
                "the API key currently comes from the shell; re-enter it to save",
                status=400,
            )

    def _build_new_block(
        self,
        block: Mapping[str, Any],
        managed: bool,
        environ: Mapping[str, str],
        validated: Mapping[str, str],
        clear: tuple[str, ...],
    ) -> dict[str, Any]:
        """Produce the new ``env`` block (section 4 step e).

        Starts from the existing block (every entry kept). On first save adopts
        model/base/reasoning from the effective values when not provided, and
        the key ONLY from the file (never the shell; DD-2). Sets provided fields
        under canonical names, deletes the aliases of every touched or adopted
        field, clears named fields, and sets the marker.
        """
        new_block = dict(block)
        to_write: dict[str, str] = dict(validated)

        if not managed:
            # First managed save adoption (DD-2).
            for field in ("model", "api_base", "reasoning_effort"):
                if field in to_write or field in clear:
                    continue
                adopted = self._adopt_from_effective(block, environ, field)
                if adopted is not None:
                    to_write[field] = adopted
            if "api_key" not in to_write and "api_key" not in clear:
                # Adopt the key only from the file, never from the shell.
                file_key = self._first_present(block, FIELD_NAMES["api_key"])
                if file_key is not None:
                    to_write["api_key"] = file_key

        # Write provided/adopted fields under canonical names; drop their aliases.
        for field, value in to_write.items():
            for name in FIELD_NAMES[field]:
                new_block.pop(name, None)
            new_block[CANONICAL[field]] = value

        # Clear named fields: remove canonical and alias names.
        for field in clear:
            for name in FIELD_NAMES[field]:
                new_block.pop(name, None)

        new_block[MARKER] = "1"
        return new_block

    def _adopt_from_effective(
        self, block: Mapping[str, Any], environ: Mapping[str, str], field: str
    ) -> str | None:
        """Effective value of ``field`` for first-save adoption (env then file)."""
        names = FIELD_NAMES[field]
        env_value = self._first_present(environ, names)
        if env_value is not None:
            return env_value
        return self._first_present(block, names)

    def _sync_active_profile(self, new_block: dict[str, Any]) -> None:
        """Keep the active profile's stored fields in step with a flat PUT (spec).

        When a profiles list exists with an active id, a flat ``PUT /api/settings``
        must update that active profile's stored fields to match the just-written
        canonical names, so the stored list and the canonical mirror never
        disagree. Mutates ``new_block`` in place; a missing/empty/malformed list
        or no active profile is a no-op (the single-settings path is unchanged).
        """
        raw = new_block.get("STRIX_VIEWER_LLM_PROFILES")
        active_id = new_block.get("STRIX_VIEWER_LLM_ACTIVE")
        if not isinstance(raw, str) or active_id is None:
            return
        try:
            profiles = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(profiles, list):
            return
        wanted = str(active_id).lower()
        changed = False
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            if str(profile.get("id", "")).lower() != wanted:
                continue
            for field, canonical in CANONICAL.items():
                if canonical in new_block:
                    profile[field] = new_block[canonical]
                else:
                    profile.pop(field, None)
            changed = True
            break
        if changed:
            new_block["STRIX_VIEWER_LLM_PROFILES"] = json.dumps(profiles)

    def _compute_warnings(self, validated: Mapping[str, str]) -> list[str]:
        """Advisory warnings after a save (currently only ``model_unfamiliar``)."""
        if "model" not in validated:
            return []
        effective_base = validated.get("api_base") or self.saved_base()
        return model_warnings(validated["model"], effective_base)

    def _audit(self, validated: Mapping[str, str], clear: tuple[str, ...]) -> None:
        """Log changed field NAMES and the new base host only (BR-16, NFR-1).

        Never a key value, length or hash. Only the base host (not the full URL,
        which could embed a query) is logged, and only when the base changed.
        """
        changed = sorted(set(validated) | set(clear))
        base_host = None
        if "api_base" in validated:
            base_host = self._host_of(validated["api_base"])
        logger.info(
            "viewer llm settings saved: fields=%s base_host=%s",
            ",".join(changed) if changed else "(none)",
            base_host or "(unchanged)",
        )

    @staticmethod
    def _host_of(url: str) -> str | None:
        try:
            return (urlsplit(url).hostname or "").lower() or None
        except ValueError:
            return None

    # --- reset (POST /api/settings/reset) ----------------------------------

    def reset(self, *, confirm: bool) -> dict[str, Any]:
        """Remove the marker and the nine strip-set names only (BR-22, FR-14).

        Every other entry and top-level key is kept. Requires ``confirm`` True.
        """
        if confirm is not True:
            raise LaunchError("invalid_json", "reset requires confirm: true", status=400)
        with self._lock:
            data, block, file_status = self._read_full()
            if file_status == "unreadable":
                raise LaunchError(
                    "config_unreadable",
                    "the config file is not valid JSON; it was left unchanged",
                    status=409,
                )
            removed = {MARKER, *STRIP_SET, *PROFILE_ENTRY_NAMES}
            new_block = {k: v for k, v in block.items() if k not in removed}
            new_data = dict(data)
            new_data["env"] = new_block
            write_secret_text(self.path, json.dumps(new_data, indent=2))
            logger.info("viewer llm settings reset to environment")
        return self.view(editable=True)

    # --- connection test support -------------------------------------------

    def effective_test_values(self, body: Mapping[str, Any]) -> dict[str, str | None]:
        """Resolve the four values for a connection test (FR-27, BR-15).

        Typed fields override saved ones; validates typed fields exactly as a
        save (including the api_base DNS rules) but writes nothing. A blank key
        uses the saved key only if the typed base equals the saved base,
        otherwise raises ``key_required_for_base_change``.
        """
        provided, _clear = self._parse_apply_body({**body, "clear": []})

        typed_model = validate_model(provided["model"]) if "model" in provided else None
        typed_reasoning = (
            validate_reasoning_effort(provided["reasoning_effort"])
            if "reasoning_effort" in provided
            else None
        )
        typed_key = validate_api_key(provided["api_key"]) if "api_key" in provided else None
        typed_base = (
            validate_api_base(provided["api_base"], resolver=self._resolver)
            if "api_base" in provided
            else None
        )

        with self._lock:
            block, file_status = self._read_env_block()
            if file_status == "unreadable":
                raise LaunchError(
                    "config_unreadable",
                    "the config file is not valid JSON; it was left unchanged",
                    status=409,
                )
            saved_model = self._first_present(block, FIELD_NAMES["model"])
            saved_key = self._first_present(block, FIELD_NAMES["api_key"])
            saved_base = self._first_present(block, FIELD_NAMES["api_base"])
            saved_reasoning = self._first_present(block, FIELD_NAMES["reasoning_effort"])

        effective_base = typed_base if typed_base is not None else saved_base
        if typed_key is not None:
            effective_key: str | None = typed_key
        else:
            # A blank/omitted key may only reuse the saved key for the saved base.
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

    # --- redaction support ---------------------------------------------------

    def redaction_env(self) -> dict[str, str]:
        """The config file's ``env`` values, for masking secrets in streamed logs.

        A scan reads its LLM key from this file itself, so the key is in neither
        the viewer's ``os.environ`` nor the launched child's environment. The
        log stream builds its redactor from this plus ``os.environ`` so a saved
        key can never be echoed back through it. The result is used only to
        mask text; it is never returned to a client. An unreadable or missing
        file yields an empty mapping.
        """
        with self._lock:
            block, _status = self._read_env_block()
        return {name: value for name, value in block.items() if isinstance(value, str) and value}

    # --- launch environment (section 6) ------------------------------------

    def child_env(self, base: Mapping[str, str]) -> dict[str, str]:
        """Build the environment for a launched scan child (BR-25, BR-26, FR-16).

        Managed -> drop every strip-set name (case-insensitive) and, when a UI
        key is saved, the one provider key variable derived from the saved model
        (DD-3). Unmanaged (or unreadable file) -> ``dict(base)`` unchanged; an
        unreadable file is logged once. Never adds a variable.

        Lock order (BR-35): this runs INSIDE the launcher's launch lock and takes
        the store lock only for the file read; it never takes a launcher lock.
        """
        with self._lock:
            block, file_status = self._read_env_block()
        if file_status == "unreadable":
            logger.warning("viewer config file is unreadable; launching unmanaged")
            return dict(base)
        if block.get(MARKER) != "1":
            return dict(base)

        env = {k: v for k, v in base.items() if k.upper() not in STRIP_SET}
        if "LLM_API_KEY" in block:
            name = provider_key_var(block.get("STRIX_LLM"))
            if name:
                env = {k: v for k, v in env.items() if k.upper() != name}
        return env

    # --- pre-launch base check (section 6a, DD-9) --------------------------

    def pre_launch_check(self) -> None:
        """Refuse a managed launch whose saved base now points at metadata (BR-36).

        When managed and a base is saved, resolve the host (3 s bound) and raise
        ``llm_base_blocked`` (400) if any resolved address is cloud-metadata or
        the host is a metadata name. Runs BEFORE the launch lock (section 6a), so
        the DNS bound never holds a lock. Otherwise returns.
        """
        with self._lock:
            block, file_status = self._read_env_block()
        if file_status == "unreadable" or block.get(MARKER) != "1":
            return
        base = self._first_present(block, FIELD_NAMES["api_base"])
        if not base:
            return
        host = (urlsplit(base).hostname or "").lower().rstrip(".")
        if not host:
            return
        if host in _METADATA_HOSTS:
            raise LaunchError(
                "llm_base_blocked",
                "the saved LLM base URL points at a blocked address",
                status=400,
            )
        addresses = self._resolve_host(host)
        if any(_is_cloud_metadata(addr) for addr in addresses):
            raise LaunchError(
                "llm_base_blocked",
                "the saved LLM base URL points at a blocked address",
                status=400,
            )

    def _resolve_host(self, host: str) -> list[str]:
        """Resolve ``host`` for the pre-launch check; failures resolve to none.

        A literal address resolves to itself; a metadata name is caught by the
        caller before this runs. A resolution failure is not an error here — a
        host that cannot be resolved is not a metadata address, and the scan's
        own later resolution remains the residual-risk backstop (DD-9).
        """
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            pass
        if _is_private_host_name(host):
            return []
        try:
            return list(self._resolver(host, _RESOLVE_TIMEOUT))
        except (OSError, ValueError, FutureTimeoutError):
            # Unresolvable is not metadata; the scan's own later resolution is
            # the residual-risk backstop (DD-9).
            return []

    # --- helpers (public contract) -----------------------------------------

    def saved_key_present(self) -> bool:
        """True when any alias of the key field is in the file (BR-13)."""
        with self._lock:
            block, _status = self._read_env_block()
        return self._any_present(block, FIELD_NAMES["api_key"])

    def saved_base(self) -> str | None:
        """The saved API base URL, or ``None`` (BR-13, test support)."""
        with self._lock:
            block, _status = self._read_env_block()
        return self._first_present(block, FIELD_NAMES["api_base"])

    # --- helpers for the profiles layer (llm_profiles) ---------------------

    @classmethod
    def canonical_fields(cls, block: Mapping[str, Any]) -> dict[str, str | None]:
        """Return the logical field -> value map read from an env ``block``.

        Reads each field from its canonical name then its aliases (the same
        precedence ``view`` uses). Used by the profiles layer to synthesise the
        virtual ``default`` migration profile from managed flat values. The
        block must already be upper-cased.
        """
        return {field: cls._first_present(block, FIELD_NAMES[field]) for field in CANONICAL}

    @staticmethod
    def mirror_block(block: Mapping[str, Any], fields: Mapping[str, str]) -> dict[str, Any]:
        """Return ``block`` with ``fields`` mirrored to the canonical names (BR-18).

        For each logical field present in ``fields`` the canonical name is set
        and every alias of that field is removed; fields ABSENT from ``fields``
        have both their canonical and alias names removed (so the mirror never
        carries a stale value the active profile lacks). The marker is set. This
        is the exact mirror rule the single-settings ``_build_new_block`` uses,
        factored out so the profiles layer reuses it verbatim.
        """
        new_block = dict(block)
        for field, canonical in CANONICAL.items():
            for name in FIELD_NAMES[field]:
                new_block.pop(name, None)
            if field in fields:
                new_block[canonical] = fields[field]
        new_block[MARKER] = "1"
        return new_block
