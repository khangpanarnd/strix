import { AlertTriangle } from "lucide-react";

/**
 * Inline confirmation for "Reset to environment" (frontend-components
 * §ResetConfirm). Removes the settings saved here and goes back to the shell
 * environment (FR-14). Rendered in place of the Reset button once the user
 * clicks it; Cancel returns to the form unchanged.
 */

export interface ResetConfirmProps {
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ResetConfirm({ busy, onConfirm, onCancel }: ResetConfirmProps) {
  return (
    <div
      role="dialog"
      aria-modal="false"
      aria-label="Reset settings to environment"
      data-testid="settings-reset-confirm"
      className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-3"
    >
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 h-5 w-5 flex-shrink-0 text-amber-400" aria-hidden="true" />
        <p className="flex-1 text-sm text-amber-100">
          Remove the settings saved here and go back to your shell environment? Scans started from
          the viewer will use the environment variables again.
        </p>
      </div>
      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          data-testid="settings-reset-confirm-button"
          onClick={onConfirm}
          disabled={busy}
          className="rounded-lg bg-white px-3 py-1.5 text-xs font-semibold text-black transition-colors hover:bg-neutral-200 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {busy ? "Resetting…" : "Reset"}
        </button>
        <button
          type="button"
          data-testid="settings-reset-cancel-button"
          onClick={onCancel}
          disabled={busy}
          className="rounded-lg border border-[#333] px-3 py-1.5 text-xs text-[#aaa] transition-colors hover:border-[#555] hover:text-white disabled:opacity-60"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

export default ResetConfirm;
