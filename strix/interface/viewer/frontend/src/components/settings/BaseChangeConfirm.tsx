import { ShieldAlert } from "lucide-react";

/**
 * Base-change confirmation dialog (frontend-components §BaseChangeConfirm,
 * FR-26). When the typed base URL differs from the saved one and a key is
 * available, Save first opens this inline dialog: nothing is sent to the server
 * until the user clicks Continue. Cancel leaves the settings unchanged.
 *
 * The server requires a key for a base change (BR-13), so this dialog is only
 * ever shown after the key field has a value; when the key is empty the form
 * shows the "Re-enter your API key to use a different base URL." error instead
 * and never opens this dialog.
 */

export interface BaseChangeConfirmProps {
  /** The host the key would be sent to, for the warning text. */
  newHost: string;
  busy: boolean;
  onContinue: () => void;
  onCancel: () => void;
}

export function BaseChangeConfirm({ newHost, busy, onContinue, onCancel }: BaseChangeConfirmProps) {
  return (
    <div
      role="dialog"
      aria-modal="false"
      aria-label="Confirm API base URL change"
      data-testid="settings-base-change-confirm"
      className="rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-3"
    >
      <div className="flex items-start gap-3">
        <ShieldAlert className="mt-0.5 h-5 w-5 flex-shrink-0 text-amber-400" aria-hidden="true" />
        <p className="flex-1 text-sm text-amber-100">
          Your API key will be sent to <span className="font-mono text-amber-200">{newHost}</span>.
          Continue?
        </p>
      </div>
      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          data-testid="settings-base-change-continue"
          onClick={onContinue}
          disabled={busy}
          className="rounded-lg bg-white px-3 py-1.5 text-xs font-semibold text-black transition-colors hover:bg-neutral-200 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {busy ? "Saving…" : "Continue"}
        </button>
        <button
          type="button"
          data-testid="settings-base-change-cancel"
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

export default BaseChangeConfirm;
