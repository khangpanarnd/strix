import { useId } from "react";
import { ShieldAlert } from "lucide-react";
import { cn } from "@/lib/utils";
import type { FieldSource, SettingsView, TestResult } from "@/data/serverSource";
import { ConnectionResult } from "@/components/settings/ConnectionResult";
import { BaseChangeConfirm } from "@/components/settings/BaseChangeConfirm";
import { ResetConfirm } from "@/components/settings/ResetConfirm";

/**
 * The settings form (frontend-components §SettingsForm). Fully controlled by
 * `SettingsView`, which owns the server state, the typed values and every
 * async action. This component is presentation plus light client-side checks
 * for convenience — the server re-validates everything and is the single
 * source of truth (business-rules §2).
 *
 * Accessibility: each field has a real `<label htmlFor>`, hints and errors are
 * wired with `aria-describedby`, errors carry `role="alert"`, source badges
 * carry text (colour is never the only signal), and every control is
 * keyboard-reachable (NFR-8).
 */

/** The fields a server error code can target; `banner` is the catch-all. */
export type SettingsFieldKey =
  | "name"
  | "model"
  | "api_key"
  | "api_base"
  | "reasoning_effort"
  | "banner";

/** Per-field server error messages keyed by the field the code maps to. */
export type SettingsFieldErrors = Partial<Record<SettingsFieldKey, string>>;

/**
 * Map a server error `code` to the field it belongs to (frontend-components
 * §SettingsForm): profile-name codes → name; `invalid_model` → model; key
 * codes → key; base codes → base; `invalid_reasoning_effort` → select;
 * everything else → the banner.
 */
export function fieldForSettingsCode(code: string): SettingsFieldKey {
  if (code === "invalid_profile_name" || code === "profile_name_taken") return "name";
  if (code === "invalid_model") return "model";
  if (code === "invalid_api_key" || code.startsWith("key_required")) return "api_key";
  if (code === "invalid_api_base" || code.startsWith("api_base_")) return "api_base";
  if (code === "invalid_reasoning_effort") return "reasoning_effort";
  return "banner";
}

/** A human label for each source badge (text, never colour alone). */
const SOURCE_LABEL: Record<FieldSource, string> = {
  ui: "UI",
  file: "Command line file",
  environment: "Environment",
  default: "Default",
};

export interface SettingsFormValues {
  model: string;
  /** Write-only: the field is empty unless the user typed a new key. */
  api_key: string;
  api_base: string;
  reasoning_effort: string;
}

export interface SettingsFormProps {
  view: SettingsView;
  values: SettingsFormValues;
  onChange: (patch: Partial<SettingsFormValues>) => void;
  /** True when the form differs from the saved view (enables Save). */
  dirty: boolean;
  saving: boolean;
  testing: boolean;
  testResult: TestResult | null;
  fieldErrors: SettingsFieldErrors;
  /** Which confirm overlay (if any) is open. */
  confirm: "none" | "base-change" | "reset";
  /** The host the key would be sent to, when the base-change dialog is open. */
  baseChangeHost: string;
  onTest: () => void;
  onCancelTest: () => void;
  onSave: () => void;
  onConfirmBaseChange: () => void;
  onCancelBaseChange: () => void;
  onClearField: (field: "model" | "api_key" | "api_base" | "reasoning_effort") => void;
  onReset: () => void;
  onConfirmReset: () => void;
  onCancelReset: () => void;
}

export function SettingsForm(props: SettingsFormProps) {
  const {
    view,
    values,
    onChange,
    dirty,
    saving,
    testing,
    testResult,
    fieldErrors,
    confirm,
    baseChangeHost,
    onTest,
    onCancelTest,
    onSave,
    onConfirmBaseChange,
    onCancelBaseChange,
    onClearField,
    onReset,
    onConfirmReset,
    onCancelReset,
  } = props;

  const uid = useId();
  const ids = {
    model: `${uid}-model`,
    modelHint: `${uid}-model-hint`,
    modelErr: `${uid}-model-err`,
    modelList: `${uid}-model-list`,
    key: `${uid}-key`,
    keyHint: `${uid}-key-hint`,
    keyErr: `${uid}-key-err`,
    keyWin: `${uid}-key-win`,
    base: `${uid}-base`,
    baseHint: `${uid}-base-hint`,
    baseErr: `${uid}-base-err`,
    reasoning: `${uid}-reasoning`,
    reasoningErr: `${uid}-reasoning-err`,
  };

  const readOnly = !view.editable;
  const fileUnreadable = view.file_status === "unreadable";
  const disabled = readOnly || saving;
  const isWin32 = view.platform === "win32";

  const { model, api_base, reasoning_effort, api_key } = view.fields;

  // Save is blocked while nothing changed, while an unreadable file would be
  // overwritten, or while read-only. Test follows the same read-only rule.
  const canSave = dirty && !fileUnreadable && !disabled;
  const busy = saving;

  return (
    <div className="space-y-6" data-testid="settings-form">
      {/* Model */}
      <div className="space-y-1.5">
        <div className="flex items-center gap-2">
          <label htmlFor={ids.model} className="block text-sm font-medium text-white">
            Model
          </label>
          <SourceBadge source={model.source} />
          {model.source === "ui" && !readOnly && (
            <ClearLink
              field="model"
              testId="settings-model-clear"
              onClear={onClearField}
              disabled={disabled}
            />
          )}
        </div>
        <input
          id={ids.model}
          data-testid="settings-model"
          type="text"
          list={ids.modelList}
          value={values.model}
          onChange={(e) => onChange({ model: e.target.value })}
          disabled={disabled}
          autoComplete="off"
          spellCheck={false}
          placeholder="provider/model, for example anthropic/claude-sonnet-4-5"
          aria-invalid={fieldErrors.model ? true : undefined}
          aria-describedby={cn(ids.modelHint, fieldErrors.model ? ids.modelErr : "")}
          className={inputClass(!!fieldErrors.model)}
        />
        <datalist id={ids.modelList}>
          {view.model_suggestions.map((s) => (
            <option key={s} value={s} />
          ))}
        </datalist>
        <EnvHint id={ids.modelHint} source={model.source} />
        <FieldError id={ids.modelErr} message={fieldErrors.model} testId="settings-model-error" />
      </div>

      {/* API key */}
      <div className="space-y-1.5">
        <div className="flex items-center gap-2">
          <label htmlFor={ids.key} className="block text-sm font-medium text-white">
            API key
          </label>
          <KeyStatusBadge set={api_key.set} source={api_key.source} />
          {api_key.set && api_key.source === "ui" && !readOnly && (
            <ClearLink
              field="api_key"
              testId="settings-api-key-clear"
              onClear={onClearField}
              disabled={disabled}
            />
          )}
        </div>
        <input
          id={ids.key}
          data-testid="settings-api-key"
          type="password"
          autoComplete="new-password"
          value={values.api_key}
          onChange={(e) => onChange({ api_key: e.target.value })}
          disabled={disabled}
          placeholder={api_key.set ? "Enter a new key to replace the saved one" : "Enter an API key"}
          aria-invalid={fieldErrors.api_key ? true : undefined}
          aria-describedby={cn(ids.keyHint, isWin32 ? ids.keyWin : "", fieldErrors.api_key ? ids.keyErr : "")}
          className={inputClass(!!fieldErrors.api_key)}
        />
        <p id={ids.keyHint} className="text-xs text-[#777]">
          The key is sent to the viewer on this machine and stored in the config file. It is visible
          in your browser&rsquo;s developer tools while you save.
        </p>
        {isWin32 && (
          <p id={ids.keyWin} data-testid="settings-key-windows-note" className="text-xs text-amber-300/90">
            On Windows the key file is stored in your user profile and protected only by your
            account&rsquo;s permissions.
          </p>
        )}
        <FieldError id={ids.keyErr} message={fieldErrors.api_key} testId="settings-api-key-error" />
      </div>

      {/* API base URL */}
      <div className="space-y-1.5">
        <div className="flex items-center gap-2">
          <label htmlFor={ids.base} className="block text-sm font-medium text-white">
            API base URL
          </label>
          <SourceBadge source={api_base.source} />
          {api_base.source === "ui" && !readOnly && (
            <ClearLink
              field="api_base"
              testId="settings-api-base-clear"
              onClear={onClearField}
              disabled={disabled}
            />
          )}
        </div>
        <input
          id={ids.base}
          data-testid="settings-api-base"
          type="text"
          value={values.api_base}
          onChange={(e) => onChange({ api_base: e.target.value })}
          disabled={disabled}
          autoComplete="off"
          spellCheck={false}
          placeholder="Leave empty for the provider default"
          aria-invalid={fieldErrors.api_base ? true : undefined}
          aria-describedby={cn(ids.baseHint, fieldErrors.api_base ? ids.baseErr : "")}
          className={inputClass(!!fieldErrors.api_base)}
        />
        <p id={ids.baseHint} className="text-xs text-[#777]">
          Use <span className="font-mono">http://</span> only for local and LAN servers; every other
          host needs <span className="font-mono">https://</span>.
        </p>
        <EnvHint source={api_base.source} />
        <FieldError id={ids.baseErr} message={fieldErrors.api_base} testId="settings-api-base-error" />
      </div>

      {/* Reasoning effort */}
      <div className="space-y-1.5">
        <div className="flex items-center gap-2">
          <label htmlFor={ids.reasoning} className="block text-sm font-medium text-white">
            Reasoning effort
          </label>
          <SourceBadge source={reasoning_effort.source} />
        </div>
        <select
          id={ids.reasoning}
          data-testid="settings-reasoning"
          value={values.reasoning_effort}
          onChange={(e) => onChange({ reasoning_effort: e.target.value })}
          disabled={disabled}
          aria-invalid={fieldErrors.reasoning_effort ? true : undefined}
          aria-describedby={fieldErrors.reasoning_effort ? ids.reasoningErr : undefined}
          className={cn(
            "block w-56 rounded-lg border bg-[rgba(255,255,255,0.03)] px-3 py-2 text-sm text-white transition-colors focus:outline-none focus:ring-2 focus:ring-white/20 disabled:opacity-60",
            fieldErrors.reasoning_effort ? "border-red-500/60" : "border-[#2a2a2a] focus:border-[#555]"
          )}
        >
          {view.reasoning_choices.map((choice) => (
            <option key={choice} value={choice}>
              {choice}
            </option>
          ))}
        </select>
        <FieldError
          id={ids.reasoningErr}
          message={fieldErrors.reasoning_effort}
          testId="settings-reasoning-error"
        />
      </div>

      {/* Connection test result */}
      <ConnectionResult testing={testing} result={testResult} onCancel={onCancelTest} />

      {/* Banner for unmapped server errors */}
      {fieldErrors.banner && (
        <div
          role="alert"
          data-testid="settings-error-banner"
          className="flex items-start gap-3 rounded-lg border border-red-500/30 bg-red-500/5 px-4 py-3"
        >
          <ShieldAlert className="mt-0.5 h-5 w-5 flex-shrink-0 text-red-400" aria-hidden="true" />
          <p className="text-sm text-red-300">{fieldErrors.banner}</p>
        </div>
      )}

      {/* Inline confirmations replace the action row when open. */}
      {confirm === "base-change" ? (
        <BaseChangeConfirm
          newHost={baseChangeHost}
          busy={busy}
          onContinue={onConfirmBaseChange}
          onCancel={onCancelBaseChange}
        />
      ) : confirm === "reset" ? (
        <ResetConfirm busy={busy} onConfirm={onConfirmReset} onCancel={onCancelReset} />
      ) : (
        !readOnly && (
          <div className="flex flex-wrap items-center gap-3 pt-1">
            <button
              type="button"
              data-testid="settings-test"
              onClick={onTest}
              disabled={testing || saving}
              className="inline-flex items-center justify-center rounded-lg border border-[#333] px-4 py-2 text-sm font-medium text-[#ddd] transition-colors hover:border-[#555] hover:text-white disabled:cursor-not-allowed disabled:opacity-60"
            >
              Test connection
            </button>
            <button
              type="button"
              data-testid="settings-save"
              onClick={onSave}
              disabled={!canSave}
              className={cn(
                "inline-flex items-center justify-center rounded-lg px-5 py-2.5 text-sm font-semibold transition-colors",
                canSave
                  ? "bg-white text-black hover:bg-neutral-200"
                  : "cursor-not-allowed bg-white/[0.08] text-[#666]"
              )}
            >
              {saving ? "Saving…" : "Save"}
            </button>
            {view.managed && (
              <button
                type="button"
                data-testid="settings-reset"
                onClick={onReset}
                disabled={saving}
                className="ml-auto inline-flex items-center justify-center rounded-lg px-3 py-2 text-sm text-[#888] transition-colors hover:text-white disabled:opacity-60"
              >
                Reset to environment
              </button>
            )}
          </div>
        )
      )}
    </div>
  );
}

export function inputClass(hasError: boolean): string {
  return cn(
    "block w-full rounded-lg border bg-[rgba(255,255,255,0.03)] px-3 py-2 text-sm text-white placeholder:text-[#555] transition-colors focus:outline-none focus:ring-2 focus:ring-white/20 disabled:opacity-60",
    hasError ? "border-red-500/60" : "border-[#2a2a2a] focus:border-[#555]"
  );
}

/** A field-level error: an icon plus text, tied to its field by id (NFR-8). */
export function FieldError({ id, message, testId }: { id: string; message?: string; testId: string }) {
  if (!message) return null;
  return (
    <p
      id={id}
      role="alert"
      data-testid={testId}
      className="flex items-center gap-1.5 text-xs text-red-400"
    >
      <ShieldAlert className="h-3.5 w-3.5 flex-shrink-0" aria-hidden="true" />
      <span>{message}</span>
    </p>
  );
}

/** A source badge with text (colour is never the only signal, NFR-8). */
function SourceBadge({ source }: { source: FieldSource }) {
  return (
    <span
      data-testid="settings-source-badge"
      data-source={source}
      className="inline-flex items-center rounded-full border border-white/10 bg-[rgba(255,255,255,0.05)] px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-[#999]"
    >
      {SOURCE_LABEL[source]}
    </span>
  );
}

/** The key's status badge: "A key is set (source)" or "No key set". */
function KeyStatusBadge({ set, source }: { set: boolean; source: FieldSource }) {
  return (
    <span
      data-testid="settings-key-status"
      className="inline-flex items-center rounded-full border border-white/10 bg-[rgba(255,255,255,0.05)] px-2 py-0.5 text-[10px] font-medium text-[#999]"
    >
      {set ? `A key is set (${SOURCE_LABEL[source]})` : "No key set"}
    </span>
  );
}

/** A per-field Clear link that submits a `{clear:[field]}` save. */
function ClearLink({
  field,
  testId,
  onClear,
  disabled,
}: {
  field: "model" | "api_key" | "api_base" | "reasoning_effort";
  testId: string;
  onClear: (field: "model" | "api_key" | "api_base" | "reasoning_effort") => void;
  disabled: boolean;
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      onClick={() => onClear(field)}
      disabled={disabled}
      className="text-xs text-[#888] underline-offset-2 transition-colors hover:text-white hover:underline disabled:opacity-60"
    >
      Clear
    </button>
  );
}

/**
 * The "value comes from the shell" line shown for environment-sourced fields
 * (FR-7), and the "uses the Strix default" note beside a cleared managed field
 * is covered by the badge text; this hint only speaks to the environment case.
 */
function EnvHint({ id, source }: { id?: string; source: FieldSource }) {
  if (source !== "environment") return null;
  return (
    <p id={id} className="text-xs text-[#888]">
      Set in the shell that started the viewer. Saving here replaces it for scans started from the
      viewer.
    </p>
  );
}

export default SettingsForm;
