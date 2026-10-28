import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, AlertTriangle, Info } from "lucide-react";
import {
  SettingsError,
  fetchSettings,
  resetSettings,
  saveSettings,
  testConnection,
  type SettingsFieldName,
  type SettingsUpdate,
  type SettingsView as SettingsViewData,
  type TestResult,
} from "@/data/serverSource";
import {
  SettingsForm,
  fieldForSettingsCode,
  type SettingsFieldErrors,
  type SettingsFormValues,
} from "@/components/settings/SettingsForm";

/**
 * The single-form Settings page (frontend-components §SettingsView). This is
 * the pre-profiles layout, kept as the fallback when `GET
 * /api/settings/profiles` returns 404/403 (an older server) so the page keeps
 * working. The profiles-aware `SettingsView` renders this when the profiles
 * endpoint is unavailable.
 *
 * Loads `GET /api/settings` on mount and after every successful save, clear or
 * reset, holds the typed form values, and runs every async action (test with
 * an AbortController, save with the base-change confirmation, per-field clear,
 * reset with confirmation).
 *
 * Security: the key field is never seeded from the server, never stored in
 * browser storage, and is cleared after a successful save (§4).
 */

/** Build the empty, write-only form baseline from a loaded view. */
function formFromView(view: SettingsViewData): SettingsFormValues {
  return {
    model: view.fields.model.value ?? "",
    api_key: "", // write-only: never seeded from the server
    api_base: view.fields.api_base.value ?? "",
    reasoning_effort:
      view.fields.reasoning_effort.value ?? view.reasoning_choices[0] ?? "high",
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

export function LegacySettingsView() {
  const [view, setView] = useState<SettingsViewData | null>(null);
  const [values, setValues] = useState<SettingsFormValues>({
    model: "",
    api_key: "",
    api_base: "",
    reasoning_effort: "",
  });
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<TestResult | null>(null);
  const [fieldErrors, setFieldErrors] = useState<SettingsFieldErrors>({});
  const [confirm, setConfirm] = useState<"none" | "base-change" | "reset">("none");

  const testAbortRef = useRef<AbortController | null>(null);

  const applyView = useCallback((next: SettingsViewData) => {
    setView(next);
    setValues(formFromView(next));
  }, []);

  const load = useCallback(async () => {
    try {
      const next = await fetchSettings();
      applyView(next);
      setLoadError(null);
    } catch (err) {
      setLoadError(
        err instanceof SettingsError
          ? err.message
          : "The settings could not be loaded. Please try again."
      );
    }
  }, [applyView]);

  useEffect(() => {
    void load();
    return () => {
      // Abort any in-flight test on unmount.
      testAbortRef.current?.abort();
    };
  }, [load]);

  const onChange = useCallback((patch: Partial<SettingsFormValues>) => {
    setValues((prev) => ({ ...prev, ...patch }));
    // A fresh edit invalidates the last test result and clears stale errors
    // for the edited fields.
    setTestResult(null);
    setFieldErrors((prev) => {
      const next = { ...prev };
      for (const key of Object.keys(patch)) {
        delete next[key as keyof SettingsFieldErrors];
      }
      delete next.banner;
      return next;
    });
  }, []);

  // Compute the changed subset. The key is "changed" only when the user typed
  // something (the field is write-only and starts empty).
  const changed = view
    ? {
        model: values.model !== (view.fields.model.value ?? ""),
        api_base: values.api_base !== (view.fields.api_base.value ?? ""),
        reasoning_effort:
          values.reasoning_effort !== (view.fields.reasoning_effort.value ?? ""),
        api_key: values.api_key.length > 0,
      }
    : { model: false, api_base: false, reasoning_effort: false, api_key: false };

  const dirty = changed.model || changed.api_base || changed.reasoning_effort || changed.api_key;

  /** Build the PUT update from only the changed fields. */
  const buildUpdate = useCallback((): SettingsUpdate => {
    const update: SettingsUpdate = {};
    if (changed.model) update.model = values.model.trim();
    if (changed.api_base) update.api_base = values.api_base.trim();
    if (changed.reasoning_effort) update.reasoning_effort = values.reasoning_effort;
    if (changed.api_key) update.api_key = values.api_key;
    return update;
  }, [changed, values]);

  const handleServerError = useCallback((err: unknown) => {
    if (err instanceof SettingsError) {
      const field = fieldForSettingsCode(err.code);
      setFieldErrors({ [field]: err.message });
    } else {
      setFieldErrors({ banner: "The settings could not be saved. Please try again." });
    }
  }, []);

  const doSave = useCallback(
    async (update: SettingsUpdate) => {
      setSaving(true);
      setFieldErrors({});
      try {
        const next = await saveSettings(update);
        applyView(next);
        setConfirm("none");
        setTestResult(null);
      } catch (err) {
        handleServerError(err);
        setConfirm("none");
      } finally {
        setSaving(false);
      }
    },
    [applyView, handleServerError]
  );

  // Save entry point: enforces the base-change rule (FR-12 / BR-13) before the
  // confirmation dialog. A base change requires a key; when none is typed and
  // one is saved, we surface the field error rather than opening the dialog.
  const onSave = useCallback(() => {
    if (!view) return;
    const savedBase = view.fields.api_base.value ?? "";
    const baseChanged = values.api_base.trim() !== savedBase.trim();
    const keySaved = view.fields.api_key.set;
    const typedKey = values.api_key.length > 0;

    if (baseChanged && keySaved) {
      if (!typedKey) {
        setFieldErrors({ api_key: "Re-enter your API key to use a different base URL." });
        return;
      }
      setConfirm("base-change");
      return;
    }
    void doSave(buildUpdate());
  }, [view, values, doSave, buildUpdate]);

  const onConfirmBaseChange = useCallback(() => {
    void doSave(buildUpdate());
  }, [doSave, buildUpdate]);

  const onCancelBaseChange = useCallback(() => setConfirm("none"), []);

  const onClearField = useCallback(
    (field: SettingsFieldName) => {
      void doSave({ clear: [field] });
    },
    [doSave]
  );

  const onTest = useCallback(async () => {
    if (!view) return;
    // Mirror the client-side base-change guard so Test never silently sends
    // the saved key to a different host (the server enforces BR-15 too).
    const savedBase = view.fields.api_base.value ?? "";
    const baseChanged = values.api_base.trim() !== savedBase.trim();
    const keySaved = view.fields.api_key.set;
    const typedKey = values.api_key.length > 0;
    if (baseChanged && keySaved && !typedKey) {
      setFieldErrors({ api_key: "Re-enter your API key to use a different base URL." });
      return;
    }

    const controller = new AbortController();
    testAbortRef.current = controller;
    setTesting(true);
    setTestResult(null);
    setFieldErrors({});
    try {
      const result = await testConnection(buildUpdate(), controller.signal);
      setTestResult(result);
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        // User cancelled; leave the result empty.
        return;
      }
      handleServerError(err);
    } finally {
      setTesting(false);
      testAbortRef.current = null;
    }
  }, [view, values, buildUpdate, handleServerError]);

  const onCancelTest = useCallback(() => {
    testAbortRef.current?.abort();
    setTesting(false);
  }, []);

  const onReset = useCallback(() => setConfirm("reset"), []);
  const onCancelReset = useCallback(() => setConfirm("none"), []);
  const onConfirmReset = useCallback(async () => {
    setSaving(true);
    setFieldErrors({});
    try {
      const next = await resetSettings();
      applyView(next);
      setTestResult(null);
    } catch (err) {
      handleServerError(err);
    } finally {
      setSaving(false);
      setConfirm("none");
    }
  }, [applyView, handleServerError]);

  if (loadError && !view) {
    return (
      <div
        role="alert"
        className="flex items-start gap-3 rounded-lg border border-red-500/30 bg-red-500/5 px-4 py-3"
      >
        <AlertCircle className="mt-0.5 h-5 w-5 flex-shrink-0 text-red-400" aria-hidden="true" />
        <p className="text-sm text-red-300">{loadError}</p>
      </div>
    );
  }

  if (!view) {
    return (
      <div className="rounded-xl border border-[#222] bg-[rgba(255,255,255,0.02)] p-10 text-center">
        <div className="mx-auto mb-3 h-6 w-6 animate-spin rounded-full border-2 border-[#333] border-t-white" />
        <p className="text-sm text-[#888]">Loading settings…</p>
      </div>
    );
  }

  return (
    <div className="space-y-6" data-testid="settings-single-form">
      <Notices view={view} />
      {view.config_path && (
        <p className="text-xs text-[#666]">
          Stored in <span className="font-mono text-[#888]">{view.config_path}</span>
        </p>
      )}
      <SettingsForm
        view={view}
        values={values}
        onChange={onChange}
        dirty={dirty}
        saving={saving}
        testing={testing}
        testResult={testResult}
        fieldErrors={fieldErrors}
        confirm={confirm}
        baseChangeHost={hostOf(values.api_base)}
        onTest={() => void onTest()}
        onCancelTest={onCancelTest}
        onSave={onSave}
        onConfirmBaseChange={onConfirmBaseChange}
        onCancelBaseChange={onCancelBaseChange}
        onClearField={onClearField}
        onReset={onReset}
        onConfirmReset={() => void onConfirmReset()}
        onCancelReset={onCancelReset}
      />
    </div>
  );
}

/**
 * The stack of state notices above the form (frontend-components §SettingsView):
 * read-only, file-unreadable, scan-active, managed-missing-values, and the
 * command-line-replaced-the-connection note.
 */
function Notices({ view }: { view: SettingsViewData }) {
  const readOnly = !view.editable;
  const fileUnreadable = view.file_status === "unreadable";
  const modelMissing = !view.fields.model.value;
  const keyMissing = !view.fields.api_key.set;
  const managedMissing = view.managed && (modelMissing || keyMissing);
  const staleConnection = view.managed && modelMissing;

  return (
    <div className="space-y-3">
      {readOnly && (
        <NoticeCard tone="info" testId="settings-readonly-notice">
          Settings can be changed only from a viewer started on this machine (bound to a loopback
          address). This viewer is reachable over the network, so the fields are read-only. You can
          see the settings here, but not the API key.
        </NoticeCard>
      )}

      {fileUnreadable && (
        <NoticeCard tone="error" testId="settings-unreadable-notice">
          The config file is not valid JSON. Nothing can be saved until it is fixed; Save is
          disabled.
        </NoticeCard>
      )}

      {managedMissing && (
        <NoticeCard tone="warning" testId="settings-missing-values-notice">
          Scans started from the viewer will fail until a model and an API key are set. Enter them,
          or reset to use your shell environment.
        </NoticeCard>
      )}

      {staleConnection && (
        <NoticeCard tone="warning" testId="settings-stale-connection-notice">
          The command line replaced the saved connection. Enter your settings again.
        </NoticeCard>
      )}

      {view.scan_active ? (
        <NoticeCard tone="info" testId="settings-scan-active-notice">
          A scan is running. Changes apply to scans you start after saving.
        </NoticeCard>
      ) : (
        <p data-testid="settings-apply-note" className="text-xs text-[#666]">
          Changes apply to scans you start after saving, never to a scan already running.
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

export default LegacySettingsView;
