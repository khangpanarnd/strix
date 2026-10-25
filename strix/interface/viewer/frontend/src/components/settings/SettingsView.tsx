import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, AlertTriangle, Info, Plus, Settings as SettingsIcon } from "lucide-react";
import {
  SettingsError,
  activateProfile,
  deleteProfile,
  fetchProfiles,
  resetSettings,
  saveProfile,
  testConnection,
  type LlmProfile,
  type ProfileSaveBody,
  type ProfilesView as ProfilesViewData,
  type TestResult,
} from "@/data/serverSource";
import { fieldForSettingsCode, type SettingsFieldErrors } from "@/components/settings/SettingsForm";
import { LegacySettingsView } from "@/components/settings/LegacySettingsView";
import { ProfileList } from "@/components/settings/ProfileList";
import { ProfileEditor, type ProfileEditorValues } from "@/components/settings/ProfileEditor";
import { ResetConfirm } from "@/components/settings/ResetConfirm";

/**
 * Container and data owner for the LLM Settings page (llm-profiles-spec.md
 * §UI). Loads `GET /api/settings/profiles` on mount; on a 404/403 (an older
 * server that predates profiles) it falls back to the single-form page so
 * nothing breaks.
 *
 * It renders a profiles list at the top and a New/Edit editor panel below.
 * It owns the server state, the typed editor values and every async action
 * (test with an AbortController and the active profile's id, save with the
 * base-change confirmation, activate, delete with confirmation, reset).
 *
 * Security: the key field is never seeded from the server, never stored in
 * browser storage, and is cleared after a successful save.
 */

type EditorMode = { kind: "closed" } | { kind: "new" } | { kind: "edit"; id: string };

const EMPTY_EDITOR: ProfileEditorValues = {
  name: "",
  preset: "custom",
  model: "",
  api_key: "",
  api_base: "",
  reasoning_effort: "",
};

/** Default reasoning effort for a fresh editor (first server-ordered choice). */
function defaultReasoning(view: ProfilesViewData): string {
  return view.reasoning_choices[0] ?? "";
}

/** Build the editor baseline for a new profile (key always empty). */
function newEditorValues(view: ProfilesViewData): ProfileEditorValues {
  const preset = view.presets[0]?.id ?? "custom";
  const base = view.presets.find((p) => p.id === preset)?.default_base ?? "";
  const prefix = view.presets.find((p) => p.id === preset)?.model_prefix ?? "";
  return {
    ...EMPTY_EDITOR,
    preset,
    model: prefix,
    api_base: base,
    reasoning_effort: defaultReasoning(view),
  };
}

/** Build the editor baseline for an existing profile (key always empty). */
function editEditorValues(profile: LlmProfile, view: ProfilesViewData): ProfileEditorValues {
  return {
    name: profile.name,
    preset: profile.preset || "custom",
    model: profile.model ?? "",
    api_key: "", // write-only: never seeded from the server
    api_base: profile.api_base ?? "",
    reasoning_effort: profile.reasoning_effort ?? defaultReasoning(view),
  };
}

/** The host of a base URL string, or "" when it cannot be parsed. */
function hostOf(base: string): string {
  const trimmed = base.trim();
  if (!trimmed) return "";
  try {
    return new URL(trimmed).host;
  } catch {
    return trimmed;
  }
}

export function SettingsView() {
  const [view, setView] = useState<ProfilesViewData | null>(null);
  const [legacyFallback, setLegacyFallback] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [mode, setMode] = useState<EditorMode>({ kind: "closed" });
  const [values, setValues] = useState<ProfileEditorValues>(EMPTY_EDITOR);
  const [fieldErrors, setFieldErrors] = useState<SettingsFieldErrors>({});
  const [confirm, setConfirm] = useState<"none" | "base-change">("none");
  const [testResult, setTestResult] = useState<TestResult | null>(null);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  const [resetConfirm, setResetConfirm] = useState(false);
  const [activatedNote, setActivatedNote] = useState(false);

  const testAbortRef = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await fetchProfiles();
      setView(next);
      setLegacyFallback(false);
      setLoadError(null);
    } catch (err) {
      if (err instanceof SettingsError && (err.status === 404 || err.status === 403)) {
        // Older server without the profiles endpoint: fall back to the
        // single-form page so the Settings page keeps working.
        setLegacyFallback(true);
        return;
      }
      setLoadError(
        err instanceof SettingsError
          ? err.message
          : "The settings could not be loaded. Please try again."
      );
    }
  }, []);

  useEffect(() => {
    void load();
    return () => {
      testAbortRef.current?.abort();
    };
  }, [load]);

  const editingProfile =
    mode.kind === "edit" && view ? (view.profiles.find((p) => p.id === mode.id) ?? null) : null;

  const openNew = useCallback(() => {
    if (!view) return;
    setMode({ kind: "new" });
    setValues(newEditorValues(view));
    setFieldErrors({});
    setTestResult(null);
    setConfirm("none");
    setActivatedNote(false);
  }, [view]);

  const openEdit = useCallback(
    (id: string) => {
      if (!view) return;
      const profile = view.profiles.find((p) => p.id === id);
      if (!profile) return;
      setMode({ kind: "edit", id });
      setValues(editEditorValues(profile, view));
      setFieldErrors({});
      setTestResult(null);
      setConfirm("none");
      setActivatedNote(false);
    },
    [view]
  );

  const closeEditor = useCallback(() => {
    testAbortRef.current?.abort();
    setMode({ kind: "closed" });
    setValues(EMPTY_EDITOR);
    setFieldErrors({});
    setTestResult(null);
    setConfirm("none");
    setTesting(false);
  }, []);

  // Editing a field: apply the patch and, when the preset changes, pre-fill the
  // model prefix and the base hint (where the preset has a default_base). Every
  // field stays editable.
  const onChange = useCallback(
    (patch: Partial<ProfileEditorValues>) => {
      setTestResult(null);
      setFieldErrors((prev) => {
        const next = { ...prev };
        for (const key of Object.keys(patch)) {
          delete next[key as keyof SettingsFieldErrors];
        }
        delete next.banner;
        return next;
      });

      if (patch.preset !== undefined && view) {
        const preset = view.presets.find((p) => p.id === patch.preset);
        setValues((prev) => ({
          ...prev,
          ...patch,
          // Pre-fill the model prefix and base hint only; keep anything the
          // user already typed beyond the prefix by replacing just the prefix.
          model: preset ? preset.model_prefix : prev.model,
          api_base: preset && preset.default_base ? preset.default_base : prev.api_base,
        }));
        return;
      }
      setValues((prev) => ({ ...prev, ...patch }));
    },
    [view]
  );

  const handleServerError = useCallback((err: unknown) => {
    if (err instanceof SettingsError) {
      const field = fieldForSettingsCode(err.code);
      setFieldErrors({ [field]: err.message });
    } else {
      setFieldErrors({ banner: "The profile could not be saved. Please try again." });
    }
  }, []);

  // Which fields changed vs the edited profile (for a new profile everything a
  // non-empty value is sent). The key is "changed" only when the user typed.
  const buildSaveBody = useCallback((): ProfileSaveBody => {
    const body: ProfileSaveBody = { name: values.name.trim() };
    body.preset = values.preset;
    if (mode.kind === "edit" && editingProfile) {
      body.id = editingProfile.id;
      const clear: Array<"api_key" | "api_base" | "reasoning_effort"> = [];
      if (values.model.trim() !== (editingProfile.model ?? "")) body.model = values.model.trim();
      if (values.api_base.trim() !== (editingProfile.api_base ?? "")) {
        if (values.api_base.trim()) body.api_base = values.api_base.trim();
        else if (editingProfile.api_base) clear.push("api_base");
      }
      if (values.reasoning_effort !== (editingProfile.reasoning_effort ?? "")) {
        if (values.reasoning_effort) body.reasoning_effort = values.reasoning_effort;
        else if (editingProfile.reasoning_effort) clear.push("reasoning_effort");
      }
      if (values.api_key.length > 0) body.api_key = values.api_key;
      if (clear.length > 0) body.clear = clear;
    } else {
      if (values.model.trim()) body.model = values.model.trim();
      if (values.api_base.trim()) body.api_base = values.api_base.trim();
      if (values.reasoning_effort) body.reasoning_effort = values.reasoning_effort;
      if (values.api_key.length > 0) body.api_key = values.api_key;
    }
    return body;
  }, [values, mode, editingProfile]);

  const applyViewAfterSave = useCallback((next: ProfilesViewData) => {
    setView(next);
  }, []);

  const doSave = useCallback(
    async (body: ProfileSaveBody) => {
      setSaving(true);
      setBusy(true);
      setFieldErrors({});
      try {
        const next = await saveProfile(body);
        applyViewAfterSave(next);
        closeEditor();
      } catch (err) {
        handleServerError(err);
        setConfirm("none");
      } finally {
        setSaving(false);
        setBusy(false);
      }
    },
    [applyViewAfterSave, closeEditor, handleServerError]
  );

  // Save entry: name is required, and (on an edit) the base-change rule gates
  // the save behind the confirmation dialog, requiring a typed key.
  const onSave = useCallback(() => {
    if (!view) return;
    if (!values.name.trim()) {
      setFieldErrors({ name: "Enter a name for this profile." });
      return;
    }
    if (mode.kind === "edit" && editingProfile) {
      const baseChanged = values.api_base.trim() !== (editingProfile.api_base ?? "").trim();
      const keySaved = editingProfile.api_key_set;
      const typedKey = values.api_key.length > 0;
      if (baseChanged && keySaved) {
        if (!typedKey) {
          setFieldErrors({ api_key: "Re-enter your API key to use a different base URL." });
          return;
        }
        setConfirm("base-change");
        return;
      }
    }
    void doSave(buildSaveBody());
  }, [view, values, mode, editingProfile, doSave, buildSaveBody]);

  const onConfirmBaseChange = useCallback(() => {
    void doSave(buildSaveBody());
  }, [doSave, buildSaveBody]);

  const onCancelBaseChange = useCallback(() => setConfirm("none"), []);

  // Build the test body (the same value fields as a save, no name/preset).
  const onTest = useCallback(async () => {
    if (!view) return;
    const profileId = mode.kind === "edit" && editingProfile ? editingProfile.id : undefined;
    if (mode.kind === "edit" && editingProfile) {
      const baseChanged = values.api_base.trim() !== (editingProfile.api_base ?? "").trim();
      const keySaved = editingProfile.api_key_set;
      const typedKey = values.api_key.length > 0;
      if (baseChanged && keySaved && !typedKey) {
        setFieldErrors({ api_key: "Re-enter your API key to use a different base URL." });
        return;
      }
    }

    const update: {
      model?: string;
      api_key?: string;
      api_base?: string;
      reasoning_effort?: string;
    } = {};
    if (values.model.trim()) update.model = values.model.trim();
    if (values.api_base.trim()) update.api_base = values.api_base.trim();
    if (values.reasoning_effort) update.reasoning_effort = values.reasoning_effort;
    if (values.api_key.length > 0) update.api_key = values.api_key;

    const controller = new AbortController();
    testAbortRef.current = controller;
    setTesting(true);
    setTestResult(null);
    setFieldErrors({});
    try {
      const result = await testConnection(update, controller.signal, profileId);
      setTestResult(result);
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      handleServerError(err);
    } finally {
      setTesting(false);
      testAbortRef.current = null;
    }
  }, [view, values, mode, editingProfile, handleServerError]);

  const onCancelTest = useCallback(() => {
    testAbortRef.current?.abort();
    setTesting(false);
  }, []);

  const onActivate = useCallback(async (id: string) => {
    setBusy(true);
    try {
      const next = await activateProfile(id);
      setView(next);
      setActivatedNote(true);
    } catch (err) {
      setLoadError(
        err instanceof SettingsError ? err.message : "The profile could not be activated."
      );
    } finally {
      setBusy(false);
    }
  }, []);

  const onConfirmDelete = useCallback(async (id: string) => {
    setBusy(true);
    try {
      const next = await deleteProfile(id);
      setView(next);
      setPendingDeleteId(null);
    } catch (err) {
      setLoadError(
        err instanceof SettingsError ? err.message : "The profile could not be deleted."
      );
      setPendingDeleteId(null);
    } finally {
      setBusy(false);
    }
  }, []);

  const onConfirmReset = useCallback(async () => {
    setBusy(true);
    try {
      await resetSettings();
      setResetConfirm(false);
      setActivatedNote(false);
      await load();
    } catch (err) {
      setLoadError(err instanceof SettingsError ? err.message : "The settings could not be reset.");
    } finally {
      setBusy(false);
    }
  }, [load]);

  // Save is enabled when a name is present; for an edit it also needs a change.
  const canSave = (() => {
    if (!values.name.trim()) return false;
    if (mode.kind === "new") return true;
    if (mode.kind === "edit" && editingProfile) {
      return (
        values.name.trim() !== editingProfile.name ||
        values.preset !== (editingProfile.preset || "custom") ||
        values.model.trim() !== (editingProfile.model ?? "") ||
        values.api_base.trim() !== (editingProfile.api_base ?? "") ||
        values.reasoning_effort !== (editingProfile.reasoning_effort ?? "") ||
        values.api_key.length > 0
      );
    }
    return false;
  })();

  return (
    <div className="mx-auto max-w-2xl space-y-6" data-testid="settings-view">
      <div className="flex items-center gap-2">
        <SettingsIcon className="h-5 w-5 text-[#888]" aria-hidden="true" />
        <h1 className="text-2xl font-semibold text-white">Settings</h1>
      </div>

      {legacyFallback ? (
        <LegacySettingsView />
      ) : loadError && !view ? (
        <div
          role="alert"
          className="flex items-start gap-3 rounded-lg border border-red-500/30 bg-red-500/5 px-4 py-3"
        >
          <AlertCircle className="mt-0.5 h-5 w-5 flex-shrink-0 text-red-400" aria-hidden="true" />
          <p className="text-sm text-red-300">{loadError}</p>
        </div>
      ) : !view ? (
        <div className="rounded-xl border border-[#222] bg-[rgba(255,255,255,0.02)] p-10 text-center">
          <div className="mx-auto mb-3 h-6 w-6 animate-spin rounded-full border-2 border-[#333] border-t-white" />
          <p className="text-sm text-[#888]">Loading settings…</p>
        </div>
      ) : (
        <>
          <Notices view={view} activatedNote={activatedNote} />
          {loadError && (
            <div
              role="alert"
              data-testid="settings-action-error"
              className="flex items-start gap-3 rounded-lg border border-red-500/30 bg-red-500/5 px-4 py-3"
            >
              <AlertCircle className="mt-0.5 h-5 w-5 flex-shrink-0 text-red-400" aria-hidden="true" />
              <p className="text-sm text-red-300">{loadError}</p>
            </div>
          )}

          {view.config_path && (
            <p className="text-xs text-[#666]">
              Stored in <span className="font-mono text-[#888]">{view.config_path}</span>
            </p>
          )}

          {view.profiles.length === 0 ? (
            <EmptyState editable={view.editable} busy={busy} onNew={openNew} />
          ) : (
            <ProfileList
              profiles={view.profiles}
              presets={view.presets}
              editable={view.editable}
              pendingDeleteId={pendingDeleteId}
              busy={busy}
              onNew={openNew}
              onActivate={(id) => void onActivate(id)}
              onEdit={openEdit}
              onRequestDelete={setPendingDeleteId}
              onConfirmDelete={(id) => void onConfirmDelete(id)}
              onCancelDelete={() => setPendingDeleteId(null)}
            />
          )}

          {view.editable && mode.kind !== "closed" && (
            <div className="rounded-xl border border-[#222] bg-[rgba(255,255,255,0.02)] p-5">
              <ProfileEditor
                view={view}
                editing={editingProfile}
                values={values}
                onChange={onChange}
                canSave={canSave}
                saving={saving}
                testing={testing}
                testResult={testResult}
                fieldErrors={fieldErrors}
                confirm={confirm}
                baseChangeHost={hostOf(values.api_base)}
                onTest={() => void onTest()}
                onCancelTest={onCancelTest}
                onSave={onSave}
                onCancel={closeEditor}
                onConfirmBaseChange={onConfirmBaseChange}
                onCancelBaseChange={onCancelBaseChange}
              />
            </div>
          )}

          {/* Reset to environment lives under the list, like the single form. */}
          {view.editable && view.managed && mode.kind === "closed" && (
            <div className="pt-1">
              {resetConfirm ? (
                <ResetConfirm
                  busy={busy}
                  onConfirm={() => void onConfirmReset()}
                  onCancel={() => setResetConfirm(false)}
                />
              ) : (
                <button
                  type="button"
                  data-testid="settings-reset"
                  onClick={() => setResetConfirm(true)}
                  disabled={busy}
                  className="inline-flex items-center justify-center rounded-lg px-3 py-2 text-sm text-[#888] transition-colors hover:text-white disabled:opacity-60"
                >
                  Reset to environment
                </button>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

/** The empty state for a fresh install with no profiles yet. */
function EmptyState({
  editable,
  busy,
  onNew,
}: {
  editable: boolean;
  busy: boolean;
  onNew: () => void;
}) {
  return (
    <div
      data-testid="settings-empty-state"
      className="rounded-xl border border-[#222] bg-[rgba(255,255,255,0.02)] p-10 text-center"
    >
      <h2 className="text-base font-semibold text-white">No profiles yet</h2>
      <p className="mx-auto mt-1 max-w-md text-sm text-[#888]">
        Create a profile to connect a model for scans you start from the viewer. Pick a preset to
        pre-fill the provider, or configure a custom endpoint.
      </p>
      {editable ? (
        <button
          type="button"
          data-testid="profile-new"
          onClick={onNew}
          disabled={busy}
          className="mt-4 inline-flex items-center gap-1.5 rounded-lg bg-white px-4 py-2 text-sm font-semibold text-black transition-colors hover:bg-neutral-200 disabled:cursor-not-allowed disabled:opacity-60"
        >
          <Plus className="h-4 w-4" aria-hidden="true" />
          New profile
        </button>
      ) : (
        <p className="mt-4 text-xs text-[#666]">
          Profiles can be created only from a viewer started on this machine.
        </p>
      )}
    </div>
  );
}

/**
 * State notices above the list: read-only, file-unreadable, scan-active, the
 * managed-missing-values warning for the active profile, and the just-activated
 * confirmation ("applies to scans you start next").
 */
function Notices({ view, activatedNote }: { view: ProfilesViewData; activatedNote: boolean }) {
  const readOnly = !view.editable;
  const fileUnreadable = view.file_status === "unreadable";
  const active = view.profiles.find((p) => p.id === view.active_id) ?? null;
  const activeMissing =
    view.managed && active !== null && (!active.model || !active.api_key_set);

  return (
    <div className="space-y-3">
      {readOnly && (
        <NoticeCard tone="info" testId="settings-readonly-notice">
          Settings can be changed only from a viewer started on this machine (bound to a loopback
          address). This viewer is reachable over the network, so the profiles are read-only. You
          can see them here, but not the API keys.
        </NoticeCard>
      )}

      {fileUnreadable && (
        <NoticeCard tone="error" testId="settings-unreadable-notice">
          The config file is not valid JSON. Nothing can be saved until it is fixed.
        </NoticeCard>
      )}

      {activeMissing && (
        <NoticeCard tone="warning" testId="settings-missing-values-notice">
          Scans started from the viewer will fail until the active profile has a model and an API
          key. Edit it, or reset to use your shell environment.
        </NoticeCard>
      )}

      {activatedNote && (
        <NoticeCard tone="info" testId="settings-activated-note">
          Applies to scans you start next. A running scan is not changed.
        </NoticeCard>
      )}

      {view.scan_active ? (
        <NoticeCard tone="info" testId="settings-scan-active-notice">
          A scan is running. Changes apply to scans you start after saving.
        </NoticeCard>
      ) : (
        <p data-testid="settings-apply-note" className="text-xs text-[#666]">
          Activating a profile applies to scans you start next, never to a scan already running.
        </p>
      )}
    </div>
  );
}

type NoticeTone = "info" | "warning" | "error";

function NoticeCard({
  tone,
  testId,
  children,
}: {
  tone: NoticeTone;
  testId: string;
  children: React.ReactNode;
}) {
  const palette: Record<NoticeTone, string> = {
    info: "border-[#2a2a2a] bg-[rgba(255,255,255,0.03)] text-[#bbb]",
    warning: "border-amber-500/30 bg-amber-500/5 text-amber-100",
    error: "border-red-500/30 bg-red-500/5 text-red-200",
  };
  const Icon = tone === "error" ? AlertCircle : tone === "warning" ? AlertTriangle : Info;
  const iconColor =
    tone === "error" ? "text-red-400" : tone === "warning" ? "text-amber-400" : "text-[#888]";
  return (
    <div
      role={tone === "info" ? "note" : "alert"}
      data-testid={testId}
      className={`flex items-start gap-3 rounded-lg border px-4 py-3 ${palette[tone]}`}
    >
      <Icon className={`mt-0.5 h-5 w-5 flex-shrink-0 ${iconColor}`} aria-hidden="true" />
      <p className="text-sm">{children}</p>
    </div>
  );
}

export default SettingsView;
