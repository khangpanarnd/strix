import { useEffect, useRef, useState } from "react";
import { AlertCircle, Copy, Loader2 } from "lucide-react";

/**
 * Shown while a launched scan is in the `starting` phase and when it fails at
 * startup (frontend-components §StartingPanel). Two faces:
 *
 *  - starting: spinner, the target + mode being started, elapsed seconds, and a
 *    Cancel link (the child is stoppable while STARTING).
 *  - failed: an error card with the failure title, the redacted `error_tail` in
 *    a `<pre>` with a Copy button, and an "Edit and retry" button that returns
 *    to the form with the values preserved.
 */

export interface StartingPanelProps {
  target: string;
  mode: string;
  /** Null while starting; a redacted tail string once the scan has FAILED. */
  errorTail: string | null;
  failed: boolean;
  /** Shown when the poll loop has exceeded its 120 s give-up budget. */
  tookTooLong: boolean;
  onCancel: () => void;
  onOpenRun: () => void;
  onEditAndRetry: () => void;
}

export function StartingPanel({
  target,
  mode,
  errorTail,
  failed,
  tookTooLong,
  onCancel,
  onOpenRun,
  onEditAndRetry,
}: StartingPanelProps) {
  const [elapsed, setElapsed] = useState(0);
  const startRef = useRef(Date.now());
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (failed) return;
    const id = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startRef.current) / 1000));
    }, 1000);
    return () => clearInterval(id);
  }, [failed]);

  const handleCopy = () => {
    if (!errorTail) return;
    void navigator.clipboard
      .writeText(errorTail)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      })
      .catch(() => {
        /* clipboard may be unavailable; copy is best-effort */
      });
  };

  if (failed) {
    return (
      <div
        data-testid="starting-panel-failed"
        role="alert"
        className="rounded-xl border border-red-500/30 bg-red-500/5 p-6"
      >
        <div className="flex items-start gap-3">
          <AlertCircle className="mt-0.5 h-5 w-5 flex-shrink-0 text-red-400" aria-hidden="true" />
          <div className="min-w-0 flex-1">
            <h2 className="text-base font-semibold text-white">The scan failed to start</h2>
            <p className="mt-0.5 text-sm text-[#aaa]">
              Starting <span className="font-mono text-[#ddd]">{target}</span> ({mode}) did not succeed.
            </p>
          </div>
        </div>

        {errorTail && (
          <div className="mt-4">
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-xs font-medium uppercase tracking-wide text-[#777]">Output</span>
              <button
                type="button"
                data-testid="starting-panel-copy-button"
                onClick={handleCopy}
                className="inline-flex items-center gap-1.5 rounded-md border border-[#333] px-2 py-1 text-xs text-[#aaa] transition-colors hover:border-[#555] hover:text-white"
              >
                <Copy className="h-3 w-3" aria-hidden="true" />
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
            <pre
              data-testid="starting-panel-error-tail"
              className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-[#2a2a2a] bg-black/40 p-3 font-mono text-xs text-[#ccc]"
            >
              {errorTail}
            </pre>
          </div>
        )}

        <div className="mt-4">
          <button
            type="button"
            data-testid="starting-panel-retry-button"
            onClick={onEditAndRetry}
            className="inline-flex items-center justify-center rounded-lg bg-white px-4 py-2 text-sm font-semibold text-black transition-colors hover:bg-neutral-200"
          >
            Edit and retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div
      data-testid="starting-panel"
      className="rounded-xl border border-[#222] bg-[rgba(255,255,255,0.02)] p-6"
      aria-live="polite"
    >
      <div className="flex items-start gap-3">
        <Loader2 className="mt-0.5 h-5 w-5 flex-shrink-0 animate-spin text-emerald-400" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <h2 className="text-base font-semibold text-white">Starting the scan…</h2>
          <p className="mt-0.5 text-sm text-[#aaa]">
            <span className="font-mono text-[#ddd]">{target}</span> ({mode}) ·{" "}
            <span className="tabular-nums">{elapsed}s</span>
          </p>
        </div>
      </div>

      {tookTooLong && (
        <p
          data-testid="starting-panel-slow-notice"
          className="mt-4 text-sm text-[#aaa]"
        >
          This is taking longer than expected.{" "}
          <button
            type="button"
            data-testid="starting-panel-open-run-button"
            onClick={onOpenRun}
            className="font-medium text-emerald-400 underline-offset-2 hover:underline"
          >
            Open the run
          </button>{" "}
          to watch its progress.
        </p>
      )}

      <div className="mt-4">
        <button
          type="button"
          data-testid="starting-panel-cancel-button"
          onClick={onCancel}
          className="text-sm text-[#888] underline-offset-2 transition-colors hover:text-white hover:underline"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

export default StartingPanel;
