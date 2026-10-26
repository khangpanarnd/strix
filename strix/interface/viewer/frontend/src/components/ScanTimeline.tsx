import { useEffect, useState } from "react";
import { CalendarClock, CheckCircle2, Hourglass, OctagonX, TriangleAlert } from "lucide-react";
import { formatElapsed, formatUtc, parseTimestamp } from "@/lib/time";
import { cn } from "@/lib/utils";

/**
 * When a scan started, when it ended, and how long it took — all in UTC.
 *
 * While the scan is running the end shows "In progress" and the duration counts
 * up every second from the recorded start. Once it ends, the end label says how
 * it ended (Completed / Stopped / Failed / Interrupted) so a stopped or failed
 * scan is never presented as complete.
 */

interface ScanTimelineProps {
  startTime: string | null;
  endTime: string | null;
  /** The raw run status from run.json (running, completed, stopped, failed, interrupted). */
  status: string | null;
  /** True once the run is in a terminal state with an end time. */
  finished: boolean;
}

interface EndStyle {
  label: string;
  icon: React.ReactNode;
  tone: string;
}

function endStyle(status: string | null): EndStyle {
  switch ((status ?? "").toLowerCase()) {
    case "stopped":
      return {
        label: "Stopped",
        icon: <OctagonX className="h-4 w-4" aria-hidden="true" />,
        tone: "text-amber-400",
      };
    case "failed":
      return {
        label: "Failed",
        icon: <TriangleAlert className="h-4 w-4" aria-hidden="true" />,
        tone: "text-red-400",
      };
    case "interrupted":
      return {
        label: "Interrupted",
        icon: <TriangleAlert className="h-4 w-4" aria-hidden="true" />,
        tone: "text-amber-400",
      };
    default:
      return {
        label: "Completed",
        icon: <CheckCircle2 className="h-4 w-4" aria-hidden="true" />,
        tone: "text-emerald-400",
      };
  }
}

export function ScanTimeline({ startTime, endTime, status, finished }: ScanTimelineProps) {
  const start = parseTimestamp(startTime);
  const end = finished ? parseTimestamp(endTime) : null;
  const running = !finished;

  // Tick once a second, but only while the scan is running: a finished run is static.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [running]);

  const durationSeconds =
    start === null ? null : ((end?.getTime() ?? (running ? now : start.getTime())) - start.getTime()) / 1000;
  const ended = endStyle(status);

  return (
    <section
      aria-label="Scan timeline"
      data-testid="scan-timeline"
      className="animate-card-in grid grid-cols-1 gap-4 rounded-xl border border-[#222] bg-[rgba(255,255,255,0.02)] px-5 py-4 sm:grid-cols-3"
    >
      <TimelineItem
        label="Started"
        icon={<CalendarClock className="h-4 w-4" aria-hidden="true" />}
        tone="text-[#ddd]"
        testId="scan-timeline-started"
      >
        {start ? (
          <time dateTime={start.toISOString()}>{formatUtc(start)}</time>
        ) : (
          <span className="text-[#666]">Not recorded</span>
        )}
      </TimelineItem>

      <TimelineItem
        label={running ? "Completed" : ended.label}
        icon={running ? <Hourglass className="h-4 w-4" aria-hidden="true" /> : ended.icon}
        tone={running ? "text-emerald-400" : ended.tone}
        testId="scan-timeline-ended"
      >
        {running ? (
          <span className="text-[#aaa]">In progress…</span>
        ) : end ? (
          <time dateTime={end.toISOString()}>{formatUtc(end)}</time>
        ) : (
          <span className="text-[#666]">Not recorded</span>
        )}
      </TimelineItem>

      <TimelineItem
        label="Duration"
        icon={<Hourglass className="h-4 w-4" aria-hidden="true" />}
        tone="text-[#ddd]"
        testId="scan-timeline-duration"
      >
        {durationSeconds === null ? (
          <span className="text-[#666]">n/a</span>
        ) : (
          <span className="tabular-nums">{formatElapsed(durationSeconds)}</span>
        )}
      </TimelineItem>
    </section>
  );
}

function TimelineItem({
  label,
  icon,
  tone,
  testId,
  children,
}: {
  label: string;
  icon: React.ReactNode;
  tone: string;
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <dl className="min-w-0" data-testid={testId}>
      <dt className={cn("flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide", tone)}>
        {icon}
        {label}
      </dt>
      <dd className="mt-1 break-words font-mono text-sm text-white">{children}</dd>
    </dl>
  );
}

export default ScanTimeline;
