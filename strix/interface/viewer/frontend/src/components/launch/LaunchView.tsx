import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, Radar } from "lucide-react";
import {
  LaunchError,
  TERMINAL_SCAN_STATES,
  fetchScanStatus,
  launchScan,
  stopScan,
  type LaunchValues,
  type ScanLimits,
} from "@/data/serverSource";
import { LaunchForm, fieldForErrorCode, type FieldErrors } from "@/components/launch/LaunchForm";
import { StartingPanel } from "@/components/launch/StartingPanel";

/**
 * Page-level container for starting a scan (frontend-components §LaunchView).
 *
 * State machine:
 *   idle → submitting → starting → (navigate to run | failed)
 *   submitting → idle (on 4xx/409/429, with field errors or a banner)
 *
 * The parent owns run selection; on a successful launch that resolves to a
 * running scan, `onLaunched(runName)` is called so App can `selectRun` + switch
 * to the overview view. `starting` is polled once per second via
 * `fetchScanStatus`, stopping on unmount or any terminal state, and giving up
 * with a "taking longer than expected" notice after 120 s.
 */

type Phase = "idle" | "submitting" | "starting" | "failed";

const STATUS_POLL_MS = 1000;
const GIVE_UP_MS = 120_000;

export interface LaunchViewProps {
  limits: ScanLimits;
  /** Called once the launched scan reaches a watchable state. */
  onLaunched: (runName: string) => void;
  /** Open an already-running scan (the scan_active 409 path). */
  onOpenRun: (runName: string) => void;
}

export function LaunchView({ limits, onLaunched, onOpenRun }: LaunchViewProps) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [banner, setBanner] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [errorTail, setErrorTail] = useState<string | null>(null);
  const [tookTooLong, setTookTooLong] = useState(false);

  // The run name being started (only set in `starting`/`failed`).
  const [runName, setRunName] = useState<string | null>(null);
  // Preserved across an "Edit and retry" so the user does not re-type.
  const [lastValues, setLastValues] = useState<LaunchValues | null>(null);

  const handleSubmit = useCallback(
    async (values: LaunchValues) => {
      setPhase("submitting");
      setFieldErrors({});
      setBanner(null);
      setConflict(null);
      setErrorTail(null);
      setLastValues(values);
      try {
        const { run_name } = await launchScan(values);
        setRunName(run_name);
        setTookTooLong(false);
        setPhase("starting");
      } catch (err) {
        if (err instanceof LaunchError) {
          if (err.status === 409 && err.run) {
            setConflict(err.run);
            setPhase("idle");
            return;
          }
          const field = fieldForErrorCode(err.code);
          if (field) {
            setFieldErrors({ [field]: err.message });
          } else {
            setBanner(err.message);
          }
          setPhase("idle");
          return;
        }
        setBanner("The scan could not be started. Please try again.");
        setPhase("idle");
      }
    },
    []
  );

  // Poll the status endpoint while starting. Navigates to the run once it is
  // running/stoppable, flips to `failed` on a FAILED status, and after 120 s
  // surfaces the slow notice (without abandoning the poll).
  const launchedRef = useRef(false);
  useEffect(() => {
    if (phase !== "starting" || !runName) return;
    launchedRef.current = false;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const startedAt = Date.now();

    const tick = async () => {
      if (cancelled) return;
      const status = await fetchScanStatus(runName);
      if (cancelled) return;

      if (status.state === "failed") {
        setErrorTail(status.error_tail);
        setPhase("failed");
        return;
      }
      if (status.state === "running" || status.state === "stopping") {
        if (!launchedRef.current) {
          launchedRef.current = true;
          onLaunched(runName);
        }
        return;
      }
      if (TERMINAL_SCAN_STATES.has(status.state)) {
        // finished/stopped before we saw it run: still open the run.
        if (!launchedRef.current) {
          launchedRef.current = true;
          onLaunched(runName);
        }
        return;
      }
      // still starting / unknown: keep polling, note the give-up budget.
      if (Date.now() - startedAt >= GIVE_UP_MS) setTookTooLong(true);
      timer = setTimeout(tick, STATUS_POLL_MS);
    };

    timer = setTimeout(tick, STATUS_POLL_MS);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [phase, runName, onLaunched]);

  const handleCancel = useCallback(() => {
    if (runName) void stopScan(runName);
    setPhase("idle");
    setRunName(null);
  }, [runName]);

  const handleEditAndRetry = useCallback(() => {
    setPhase("idle");
    setErrorTail(null);
    setRunName(null);
  }, []);

  if (phase === "starting" || phase === "failed") {
    return (
      <div className="mx-auto max-w-2xl space-y-6" data-testid="launch-view">
        <LaunchHeader />
        <StartingPanel
          target={lastValues?.target ?? ""}
          mode={lastValues?.scan_mode ?? "quick"}
          errorTail={errorTail}
          failed={phase === "failed"}
          tookTooLong={tookTooLong}
          onCancel={handleCancel}
          onOpenRun={() => runName && onOpenRun(runName)}
          onEditAndRetry={handleEditAndRetry}
        />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6" data-testid="launch-view">
      <LaunchHeader />

      {conflict && (
        <div
          role="alert"
          data-testid="launch-conflict-banner"
          className="flex flex-wrap items-center gap-3 rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-3"
        >
          <AlertCircle className="h-5 w-5 flex-shrink-0 text-amber-400" aria-hidden="true" />
          <p className="flex-1 text-sm text-amber-200">A scan is already running.</p>
          <button
            type="button"
            data-testid="launch-open-running-button"
            onClick={() => onOpenRun(conflict)}
            className="rounded-lg bg-white px-3 py-1.5 text-xs font-semibold text-black transition-colors hover:bg-neutral-200"
          >
            Open running scan
          </button>
        </div>
      )}

      {banner && (
        <div
          role="alert"
          data-testid="launch-error-banner"
          className="flex items-start gap-3 rounded-lg border border-red-500/30 bg-red-500/5 px-4 py-3"
        >
          <AlertCircle className="mt-0.5 h-5 w-5 flex-shrink-0 text-red-400" aria-hidden="true" />
          <p className="text-sm text-red-300">{banner}</p>
        </div>
      )}

      <LaunchForm
        limits={limits}
        disabled={phase === "submitting"}
        fieldErrors={fieldErrors}
        onSubmit={handleSubmit}
        initialValues={lastValues ?? undefined}
      />
    </div>
  );
}

function LaunchHeader() {
  return (
    <div className="flex items-center gap-2">
      <Radar className="h-5 w-5 text-[#888]" aria-hidden="true" />
      <h1 className="text-2xl font-semibold text-white">New scan</h1>
    </div>
  );
}

export default LaunchView;
