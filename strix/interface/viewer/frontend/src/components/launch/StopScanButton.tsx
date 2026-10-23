import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Square } from "lucide-react";
import { cn } from "@/lib/utils";
import { fetchScanStatus, stopScan, type ScanStatus } from "@/data/serverSource";

/**
 * Stop control for the active run's header (frontend-components §StopScanButton).
 *
 * - Polls `fetchScanStatus(runName)` every 2 s while the run is not finished,
 *   and once after it finishes.
 * - Renders only when `status.stoppable` is true. CLI-started runs report
 *   `state: "unknown"` (not stoppable), so the button stays hidden for them
 *   (FR-27).
 * - Click opens an inline confirmation; confirming calls `stopScan`, shows
 *   "Stopping…" and disables itself. It does not re-enable — the parent's run
 *   poll shows the run finished once the process exits.
 * - After 30 s in `stopping` it adds a "being force-stopped" hint.
 */

const STATUS_POLL_MS = 2000;
const FORCE_STOP_HINT_MS = 30_000;

export interface StopScanButtonProps {
  runName: string;
  finished: boolean;
}

export function StopScanButton({ runName, finished }: StopScanButtonProps) {
  const [status, setStatus] = useState<ScanStatus | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [forceHint, setForceHint] = useState(false);
  const stoppingSinceRef = useRef<number | null>(null);

  // Poll status every 2 s until finished; a finished run gets one final read.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async () => {
      if (cancelled) return;
      const next = await fetchScanStatus(runName);
      if (cancelled) return;
      setStatus(next);

      if (next.state === "stopping") {
        if (stoppingSinceRef.current == null) stoppingSinceRef.current = Date.now();
        if (Date.now() - stoppingSinceRef.current >= FORCE_STOP_HINT_MS) setForceHint(true);
      } else {
        stoppingSinceRef.current = null;
      }

      if (finished) return; // one-shot read after the run ends
      timer = setTimeout(tick, STATUS_POLL_MS);
    };

    void tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [runName, finished]);

  const handleConfirmStop = useCallback(async () => {
    setConfirming(false);
    setStopping(true);
    await stopScan(runName);
  }, [runName]);

  // Hidden for CLI-started runs (state unknown) and any non-stoppable scan.
  if (!status?.stoppable) return null;

  if (stopping || status.state === "stopping") {
    return (
      <div className="inline-flex flex-col gap-1" data-testid="stop-scan-stopping">
        <span className="inline-flex items-center gap-2 rounded-lg border border-[#333] px-3 py-1.5 text-sm text-[#aaa]">
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          Stopping…
        </span>
        {forceHint && (
          <span data-testid="stop-scan-force-hint" className="text-xs text-[#777]">
            The scan is being force-stopped.
          </span>
        )}
      </div>
    );
  }

  if (confirming) {
    return (
      <div
        className="inline-flex items-center gap-2 rounded-lg border border-[#333] bg-[rgba(255,255,255,0.03)] px-3 py-1.5"
        role="group"
        aria-label="Confirm stopping the scan"
        data-testid="stop-scan-confirm"
      >
        <span className="text-sm text-[#ddd]">Stop this scan? Partial results are kept.</span>
        <button
          type="button"
          data-testid="stop-scan-confirm-button"
          onClick={() => void handleConfirmStop()}
          className="rounded-md bg-red-500 px-2.5 py-1 text-xs font-semibold text-white transition-colors hover:bg-red-400"
        >
          Stop
        </button>
        <button
          type="button"
          data-testid="stop-scan-keep-button"
          onClick={() => setConfirming(false)}
          className="rounded-md border border-[#444] px-2.5 py-1 text-xs font-medium text-[#aaa] transition-colors hover:text-white"
        >
          Keep running
        </button>
      </div>
    );
  }

  return (
    <button
      type="button"
      data-testid="stop-scan-button"
      onClick={() => setConfirming(true)}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-lg border border-red-500/40 bg-red-500/5 px-3 py-1.5 text-sm font-medium text-red-300 transition-colors hover:border-red-500/60 hover:bg-red-500/10"
      )}
    >
      <Square className="h-3.5 w-3.5" aria-hidden="true" />
      Stop scan
    </button>
  );
}

export default StopScanButton;
