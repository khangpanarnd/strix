import { AlertCircle, CheckCircle2, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import type { TestResult } from "@/data/serverSource";

/**
 * Accessible result region for the connection test (frontend-components
 * §ConnectionResult). Three faces, all inside one `role="status"` live region:
 *
 *  - testing: a spinner and "Testing… (up to 90 seconds)" with a Cancel that
 *    aborts the request (the server finishes or kills the child on its own);
 *  - success: a tick and the latency;
 *  - failure: the category message and the HTTP status.
 *
 * Messages are fixed text chosen by the server (BR-32); the component renders
 * them verbatim as plain text and never interprets them.
 */

export interface ConnectionResultProps {
  /** A request is in flight; shows the spinner and Cancel. */
  testing: boolean;
  /** The last result, or null before the first test (or after a reset). */
  result: TestResult | null;
  /** Aborts the in-flight request via the caller's AbortController. */
  onCancel: () => void;
}

export function ConnectionResult({ testing, result, onCancel }: ConnectionResultProps) {
  if (!testing && !result) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="settings-test-result"
      className="rounded-lg border border-[#2a2a2a] bg-[rgba(255,255,255,0.02)] px-4 py-3"
    >
      {testing ? (
        <div className="flex items-center gap-3">
          <Loader2 className="h-4 w-4 flex-shrink-0 animate-spin text-emerald-400" aria-hidden="true" />
          <span className="flex-1 text-sm text-[#ccc]">Testing… (up to 90 seconds)</span>
          <button
            type="button"
            data-testid="settings-test-cancel"
            onClick={onCancel}
            className="rounded-md border border-[#333] px-2.5 py-1 text-xs text-[#aaa] transition-colors hover:border-[#555] hover:text-white"
          >
            Cancel
          </button>
        </div>
      ) : result ? (
        <ResultBody result={result} />
      ) : null}
    </div>
  );
}

function ResultBody({ result }: { result: TestResult }) {
  const okText =
    result.message ||
    (result.latency_ms != null
      ? `The connection succeeded (${result.latency_ms} ms).`
      : "The connection succeeded.");
  return (
    <div className="flex items-start gap-3">
      {result.ok ? (
        <CheckCircle2 className="mt-0.5 h-4 w-4 flex-shrink-0 text-emerald-400" aria-hidden="true" />
      ) : (
        <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0 text-red-400" aria-hidden="true" />
      )}
      <div className="min-w-0 flex-1 space-y-0.5">
        <p className={cn("text-sm", result.ok ? "text-emerald-300" : "text-red-300")}>{okText}</p>
        {result.ok && result.latency_ms != null && result.message && (
          <p className="text-xs tabular-nums text-[#888]">{result.latency_ms} ms</p>
        )}
      </div>
    </div>
  );
}

export default ConnectionResult;
