import { useId } from "react";
import { ShieldAlert } from "lucide-react";
import { cn } from "@/lib/utils";
import type { LlmProfile, ProfilesView, TestResult } from "@/data/serverSource";
import {
  FieldError,
  inputClass,
  type SettingsFieldErrors,
} from "@/components/settings/SettingsForm";
import { ConnectionResult } from "@/components/settings/ConnectionResult";
import { BaseChangeConfirm } from "@/components/settings/BaseChangeConfirm";

/**
 * The New / Edit profile editor (llm-profiles-spec.md §UI). Reuses the existing
 * settings fields — model with datalist suggestions, a write-only password key
 * input that is never pre-filled and is cleared after save, API base URL,
 * reasoning effort, Test connection, Save, and the base-change confirmation —
 * and adds a Name field and a Preset select at the top.
 *
 * Fully controlled by the parent `SettingsView`, which owns the server state,
 * the typed values and every async action. This component is presentation plus
 * light client-side wiring; the server re-validates everything.
 *
 * Accessibility: each control has a real `<label htmlFor>`, hints and errors
 * are wired with `aria-describedby`, errors carry `role="alert"`, and colour is
 * never the only signal.
 */

export interface ProfileEditorValues {
  name: string;
  preset: string;
  model: string;
  /** Write-only: empty unless the user typed a new key. */
  api_key: string;
  api_base: string;
  reasoning_effort: string;
}

export interface ProfileEditorProps {
  view: ProfilesView;
  /** The profile being edited, or null for a new profile. */
  editing: LlmProfile | null;
  values: ProfileEditorValues;
  onChange: (patch: Partial<ProfileEditorValues>) => void;
  /** True when the form can be saved (name present and, for edits, dirty). */
  canSave: boolean;
  saving: boolean;
  testing: boolean;
  testResult: TestResult | null;
  fieldErrors: SettingsFieldErrors;
  /** Which confirm overlay (if any) is open. */
  confirm: "none" | "base-change";
  /** The host the key would be sent to, when the base-change dialog is open. */
  baseChangeHost: string;
  onTest: () => void;
  onCancelTest: () => void;
  onSave: () => void;
  onCancel: () => void;
  onConfirmBaseChange: () => void;
  onCancelBaseChange: () => void;
}

export function ProfileEditor(props: ProfileEditorProps) {
  const {
    view,
    editing,
    values,
    onChange,
    canSave,
    saving,
    testing,
    testResult,
    fieldErrors,
    confirm,
    baseChangeHost,
    onTest,
    onCancelTest,
    onSave,
    onCancel,
    onConfirmBaseChange,
    onCancelBaseChange,
  } = props;

  const uid = useId();
  const ids = {
    name: `${uid}-name`,
    nameErr: `${uid}-name-err`,
    preset: `${uid}-preset`,
    presetHint: `${uid}-preset-hint`,
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

  const disabled = saving;
  const isWin32 = view.platform === "win32";
  const keySaved = editing?.api_key_set === true;

  const selectedPreset = view.presets.find((p) => p.id === values.preset) ?? null;
  // A preset's example models take precedence as datalist suggestions; the
  // server's general suggestions are the fallback.
  const suggestions =
    selectedPreset && selectedPreset.example_models.length > 0
      ? selectedPreset.example_models
      : view.model_suggestions;

  return (
    <div className="space-y-6" data-testid="profile-editor">
      <h2 className="text-lg font-semibold text-white">
        {editing ? `Edit profile` : "New profile"}
      </h2>

      {/* Name */}
      <div className="space-y-1.5">
        <label htmlFor={ids.name} className="block text-sm font-medium text-white">
          Name
        </label>
        <input
          id={ids.name}
          data-testid="profile-name"
          type="text"
          value={values.name}
          onChange={(e) => onChange({ name: e.target.value })}
          disabled={disabled}
          autoComplete="off"
          spellCheck={false}
          maxLength={60}
          placeholder="A name for this profile, e.g. Work OpenAI"
          aria-invalid={fieldErrors.name ? true : undefined}
          aria-describedby={fieldErrors.name ? ids.nameErr : undefined}
          className={inputClass(!!fieldErrors.name)}
        />
        <FieldError id={ids.nameErr} message={fieldErrors.name} testId="profile-name-error" />
      </div>

      {/* Preset */}
      <div className="space-y-1.5">
        <label htmlFor={ids.preset} className="block text-sm font-medium text-white">
          Preset
        </label>
        <select
          id={ids.preset}
          data-testid="profile-preset"
          value={values.preset}
          onChange={(e) => onChange({ preset: e.target.value })}
          disabled={disabled}
          aria-describedby={selectedPreset?.note ? ids.presetHint : undefined}
          className={cn(
            "block w-full rounded-lg border bg-[rgba(255,255,255,0.03)] px-3 py-2 text-sm text-white transition-colors focus:outline-none focus:ring-2 focus:ring-white/20 disabled:opacity-60",
            "border-[#2a2a2a] focus:border-[#555]"
          )}
        >
          {view.presets.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </select>
        {selectedPreset?.note && (
          <p id={ids.presetHint} data-testid="profile-preset-note" className="text-xs text-[#888]">
            {selectedPreset.note}
          </p>
        )}
      </div>

      {/* Model */}
      <div className="space-y-1.5">
        <label htmlFor={ids.model} className="block text-sm font-medium text-white">
          Model
        </label>
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
          {suggestions.map((s) => (
            <option key={s} value={s} />
          ))}
        </datalist>
        <p id={ids.modelHint} className="text-xs text-[#777]">
          Choosing a preset fills in the provider prefix and example models. The field stays
          editable.
        </p>
        <FieldError id={ids.modelErr} message={fieldErrors.model} testId="settings-model-error" />
      </div>

      {/* API key */}
      <div className="space-y-1.5">
        <div className="flex items-center gap-2">
          <label htmlFor={ids.key} className="block text-sm font-medium text-white">
            API key
          </label>
          <span
            data-testid="settings-key-status"
            className="inline-flex items-center rounded-full border border-white/10 bg-[rgba(255,255,255,0.05)] px-2 py-0.5 text-[10px] font-medium text-[#999]"
          >
            {keySaved ? "A key is set" : "No key set"}
          </span>
        </div>
        <input
          id={ids.key}
          data-testid="settings-api-key"
          type="password"
          autoComplete="new-password"
          value={values.api_key}
          onChange={(e) => onChange({ api_key: e.target.value })}
          disabled={disabled}
          placeholder={keySaved ? "Enter a new key to replace the saved one" : "Enter an API key"}
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
        <label htmlFor={ids.base} className="block text-sm font-medium text-white">
          API base URL
        </label>
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
        <FieldError id={ids.baseErr} message={fieldErrors.api_base} testId="settings-api-base-error" />
      </div>

      {/* Reasoning effort */}
      <div className="space-y-1.5">
        <label htmlFor={ids.reasoning} className="block text-sm font-medium text-white">
          Reasoning effort
        </label>
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

      {/* Inline base-change confirmation replaces the action row when open. */}
      {confirm === "base-change" ? (
        <BaseChangeConfirm
          newHost={baseChangeHost}
          busy={saving}
          onContinue={onConfirmBaseChange}
          onCancel={onCancelBaseChange}
        />
      ) : (
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
            disabled={!canSave || saving}
            className={cn(
              "inline-flex items-center justify-center rounded-lg px-5 py-2.5 text-sm font-semibold transition-colors",
              canSave && !saving
                ? "bg-white text-black hover:bg-neutral-200"
                : "cursor-not-allowed bg-white/[0.08] text-[#666]"
            )}
          >
            {saving ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            data-testid="profile-editor-cancel"
            onClick={onCancel}
            disabled={saving}
            className="ml-auto inline-flex items-center justify-center rounded-lg px-3 py-2 text-sm text-[#888] transition-colors hover:text-white disabled:opacity-60"
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

export default ProfileEditor;
