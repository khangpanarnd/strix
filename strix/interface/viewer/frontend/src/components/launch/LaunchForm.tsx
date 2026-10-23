import { useId, useMemo, useState } from "react";
import { ShieldAlert } from "lucide-react";
import { cn } from "@/lib/utils";
import type { LaunchValues, ScanLimits, ScanMode } from "@/data/serverSource";

/**
 * Controlled launch form. Client-side checks are convenience only — the server
 * re-validates everything (FR-8, BR-6..BR-17) and is the single source of
 * truth. Server error codes are mapped to the field they belong to via
 * `fieldErrors`; anything unmapped is rendered by the parent as a banner.
 *
 * Accessibility: every control has a real `<label htmlFor>`, the mode radios
 * live in a `<fieldset>`/`<legend>`, field errors are wired with
 * `aria-describedby` + `role="alert"`, and colour is never the only error
 * signal (an icon + text accompany it).
 */

/** Field keys a server error code can target. */
export type LaunchFieldKey = "target" | "budget" | "instruction" | "authorization";

/** Per-field server error messages, keyed by the field the code maps to. */
export type FieldErrors = Partial<Record<LaunchFieldKey, string>>;

/**
 * Map a server error `code` to the form field it belongs to (frontend-components
 * §2). Codes that do not target a specific field return null and are surfaced as
 * a banner by the caller.
 */
export function fieldForErrorCode(code: string): LaunchFieldKey | null {
  if (code === "invalid_target" || code.startsWith("target_")) return "target";
  if (code === "invalid_budget" || code === "budget_exceeds_max") return "budget";
  if (code === "invalid_instruction") return "instruction";
  if (code === "authorization_required") return "authorization";
  return null;
}

interface ModeOption {
  value: ScanMode;
  label: string;
  description: string;
}

const MODE_OPTIONS: ModeOption[] = [
  { value: "quick", label: "Quick", description: "Fastest, lowest cost — a focused first pass." },
  { value: "standard", label: "Standard", description: "Balanced depth and cost — the usual choice." },
  { value: "deep", label: "Deep", description: "Most thorough — longest run, highest cost." },
];

export interface LaunchFormProps {
  limits: ScanLimits;
  disabled: boolean;
  fieldErrors: FieldErrors;
  /** Called with validated-shape values; the server decides what is acceptable. */
  onSubmit: (values: LaunchValues) => void;
  /** Initial values, used to preserve inputs across an "Edit and retry". */
  initialValues?: Partial<LaunchValues>;
}

/** True when the viewer is not reachable over loopback (NFR-6, UI side). */
function isNonLoopback(): boolean {
  if (typeof window === "undefined") return false;
  const host = window.location.hostname;
  return host !== "localhost" && host !== "127.0.0.1" && host !== "::1" && host !== "[::1]";
}

export function LaunchForm({
  limits,
  disabled,
  fieldErrors,
  onSubmit,
  initialValues,
}: LaunchFormProps) {
  const uid = useId();
  const ids = {
    target: `${uid}-target`,
    targetErr: `${uid}-target-err`,
    modeLegend: `${uid}-mode-legend`,
    budget: `${uid}-budget`,
    budgetErr: `${uid}-budget-err`,
    instruction: `${uid}-instruction`,
    instructionErr: `${uid}-instruction-err`,
    instructionCount: `${uid}-instruction-count`,
    allowPrivate: `${uid}-allow-private`,
    authorized: `${uid}-authorized`,
    authorizedErr: `${uid}-authorized-err`,
  };

  const modeFromInitial =
    initialValues?.scan_mode && limits.modes.includes(initialValues.scan_mode)
      ? initialValues.scan_mode
      : limits.modes[0] ?? "quick";

  const [target, setTarget] = useState(initialValues?.target ?? "");
  const [scanMode, setScanMode] = useState<ScanMode>(modeFromInitial);
  const [budget, setBudget] = useState<string>(
    String(initialValues?.max_budget_usd ?? limits.default_budget_usd)
  );
  const [instruction, setInstruction] = useState(initialValues?.instruction ?? "");
  const [allowPrivate, setAllowPrivate] = useState(initialValues?.allow_private ?? false);
  const [authorized, setAuthorized] = useState(initialValues?.authorized ?? false);

  const nonLoopback = useMemo(isNonLoopback, []);

  const trimmedTarget = target.trim();
  const budgetNumber = Number(budget);
  const budgetInRange =
    budget.trim() !== "" &&
    Number.isFinite(budgetNumber) &&
    budgetNumber > 0 &&
    budgetNumber <= limits.max_budget_usd;
  const instructionTooLong = instruction.length > limits.instruction_max;

  // The Start button honours only the two non-negotiable gates (FR-8): a
  // non-empty target and ticked authorization. Everything else is validated
  // server-side; the button stays usable so the server can own the verdict.
  const canSubmit = trimmedTarget.length > 0 && authorized && !disabled;

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    onSubmit({
      target: trimmedTarget,
      scan_mode: scanMode,
      max_budget_usd: budgetInRange ? budgetNumber : limits.default_budget_usd,
      instruction: instruction.trim(),
      authorized,
      allow_private: allowPrivate,
    });
  };

  return (
    <form
      onSubmit={handleSubmit}
      noValidate
      data-testid="launch-form"
      className="space-y-6"
    >
      {nonLoopback && (
        <div
          role="note"
          data-testid="launch-nonloopback-notice"
          className="flex items-start gap-3 rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-3"
        >
          <ShieldAlert className="mt-0.5 h-5 w-5 flex-shrink-0 text-amber-400" aria-hidden="true" />
          <p className="text-sm text-amber-200">
            This viewer is reachable over the network. Anyone with this link can start scans that
            spend your LLM budget.
          </p>
        </div>
      )}

      {/* Target */}
      <div className="space-y-1.5">
        <label htmlFor={ids.target} className="block text-sm font-medium text-white">
          Target
        </label>
        <input
          id={ids.target}
          data-testid="launch-target-input"
          type="text"
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          disabled={disabled}
          autoComplete="off"
          placeholder="https://staging.example.com"
          aria-invalid={fieldErrors.target ? true : undefined}
          aria-describedby={fieldErrors.target ? ids.targetErr : undefined}
          className={cn(
            "block w-full rounded-lg border bg-[rgba(255,255,255,0.03)] px-3 py-2 text-sm text-white placeholder:text-[#555] transition-colors focus:outline-none focus:ring-2 focus:ring-white/20 disabled:opacity-60",
            fieldErrors.target ? "border-red-500/60" : "border-[#2a2a2a] focus:border-[#555]"
          )}
        />
        <FieldError id={ids.targetErr} message={fieldErrors.target} testId="launch-target-error" />
      </div>

      {/* Scan mode */}
      <fieldset className="space-y-2" disabled={disabled} aria-describedby={ids.modeLegend}>
        <legend id={ids.modeLegend} className="text-sm font-medium text-white">
          Scan mode
        </legend>
        <div className="space-y-2" role="radiogroup" aria-label="Scan mode" data-testid="launch-mode-group">
          {MODE_OPTIONS.filter((o) => limits.modes.includes(o.value)).map((option) => {
            const checked = scanMode === option.value;
            return (
              <label
                key={option.value}
                htmlFor={`${ids.modeLegend}-${option.value}`}
                className={cn(
                  "flex cursor-pointer items-start gap-3 rounded-lg border px-3 py-2.5 transition-colors",
                  checked
                    ? "border-white/30 bg-[rgba(255,255,255,0.05)]"
                    : "border-[#2a2a2a] hover:border-[#444]"
                )}
              >
                <input
                  id={`${ids.modeLegend}-${option.value}`}
                  data-testid={`launch-mode-${option.value}`}
                  type="radio"
                  name={`${uid}-scan-mode`}
                  value={option.value}
                  checked={checked}
                  onChange={() => setScanMode(option.value)}
                  className="mt-0.5 h-4 w-4 flex-shrink-0 accent-emerald-400"
                />
                <span className="min-w-0">
                  <span className="block text-sm font-medium text-white">{option.label}</span>
                  <span className="block text-xs text-[#888]">{option.description}</span>
                </span>
              </label>
            );
          })}
        </div>
      </fieldset>

      {/* Budget */}
      <div className="space-y-1.5">
        <label htmlFor={ids.budget} className="block text-sm font-medium text-white">
          Budget (USD)
        </label>
        <input
          id={ids.budget}
          data-testid="launch-budget-input"
          type="number"
          inputMode="decimal"
          step={0.5}
          min={0.5}
          max={limits.max_budget_usd}
          value={budget}
          onChange={(e) => setBudget(e.target.value)}
          disabled={disabled}
          aria-invalid={fieldErrors.budget ? true : undefined}
          aria-describedby={fieldErrors.budget ? ids.budgetErr : undefined}
          className={cn(
            "block w-40 rounded-lg border bg-[rgba(255,255,255,0.03)] px-3 py-2 text-sm text-white transition-colors focus:outline-none focus:ring-2 focus:ring-white/20 disabled:opacity-60",
            fieldErrors.budget ? "border-red-500/60" : "border-[#2a2a2a] focus:border-[#555]"
          )}
        />
        <p className="text-xs text-[#666]">
          Maximum {limits.max_budget_usd.toFixed(2)} USD. The server rejects values out of range.
        </p>
        <FieldError id={ids.budgetErr} message={fieldErrors.budget} testId="launch-budget-error" />
      </div>

      {/* Instructions */}
      <div className="space-y-1.5">
        <label htmlFor={ids.instruction} className="block text-sm font-medium text-white">
          Instructions <span className="font-normal text-[#666]">(optional)</span>
        </label>
        <textarea
          id={ids.instruction}
          data-testid="launch-instruction-input"
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          disabled={disabled}
          rows={4}
          maxLength={limits.instruction_max}
          placeholder="e.g. focus on the login flow"
          aria-invalid={fieldErrors.instruction || instructionTooLong ? true : undefined}
          aria-describedby={cn(
            ids.instructionCount,
            fieldErrors.instruction ? ids.instructionErr : ""
          )}
          className={cn(
            "block w-full resize-y rounded-lg border bg-[rgba(255,255,255,0.03)] px-3 py-2 text-sm text-white placeholder:text-[#555] transition-colors focus:outline-none focus:ring-2 focus:ring-white/20 disabled:opacity-60",
            fieldErrors.instruction ? "border-red-500/60" : "border-[#2a2a2a] focus:border-[#555]"
          )}
        />
        <div className="flex items-center justify-between">
          <FieldError id={ids.instructionErr} message={fieldErrors.instruction} testId="launch-instruction-error" />
          <span
            id={ids.instructionCount}
            data-testid="launch-instruction-count"
            className={cn("ml-auto text-xs tabular-nums", instructionTooLong ? "text-red-400" : "text-[#666]")}
          >
            {instruction.length} / {limits.instruction_max}
          </span>
        </div>
      </div>

      {/* Allow private */}
      <label
        htmlFor={ids.allowPrivate}
        className="flex cursor-pointer items-start gap-3 text-sm text-[#ddd]"
      >
        <input
          id={ids.allowPrivate}
          data-testid="launch-allow-private-checkbox"
          type="checkbox"
          checked={allowPrivate}
          onChange={(e) => setAllowPrivate(e.target.checked)}
          disabled={disabled}
          className="mt-0.5 h-4 w-4 flex-shrink-0 accent-emerald-400"
        />
        <span>Allow private/loopback targets (cloud metadata is always blocked)</span>
      </label>

      {/* Authorization */}
      <div className="space-y-1.5">
        <label
          htmlFor={ids.authorized}
          className="flex cursor-pointer items-start gap-3 text-sm text-[#ddd]"
        >
          <input
            id={ids.authorized}
            data-testid="launch-authorized-checkbox"
            type="checkbox"
            checked={authorized}
            onChange={(e) => setAuthorized(e.target.checked)}
            disabled={disabled}
            aria-invalid={fieldErrors.authorization ? true : undefined}
            aria-describedby={fieldErrors.authorization ? ids.authorizedErr : undefined}
            className="mt-0.5 h-4 w-4 flex-shrink-0 accent-emerald-400"
          />
          <span>I am authorized to test this target</span>
        </label>
        <FieldError id={ids.authorizedErr} message={fieldErrors.authorization} testId="launch-authorized-error" />
      </div>

      <button
        type="submit"
        data-testid="launch-submit-button"
        disabled={!canSubmit}
        className={cn(
          "inline-flex items-center justify-center rounded-lg px-5 py-2.5 text-sm font-semibold transition-colors",
          canSubmit ? "bg-white text-black hover:bg-neutral-200" : "cursor-not-allowed bg-white/[0.08] text-[#666]"
        )}
      >
        {disabled ? "Starting…" : "Start scan"}
      </button>
    </form>
  );
}

function FieldError({
  id,
  message,
  testId,
}: {
  id: string;
  message?: string;
  testId: string;
}) {
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

export default LaunchForm;
