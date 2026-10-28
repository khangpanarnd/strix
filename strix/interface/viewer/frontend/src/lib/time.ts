/**
 * UTC time helpers for the viewer. Every time the UI shows is UTC, so a run
 * looks the same to everyone who opens it regardless of their browser's time
 * zone, and matches the timestamps in `run.json` and the reports.
 */

/**
 * Parse a run-record timestamp into a Date, or null when it is missing or not a
 * date. Accepts ISO 8601 (`2026-10-26T06:20:13+00:00`, `...Z`) and the CLI's
 * `2026-10-26 06:20:13 UTC`. A value with no zone designator is read as UTC —
 * `new Date("2026-10-26T06:20:13")` would otherwise be read in the browser's
 * local zone and silently shift the time.
 */
export function parseTimestamp(raw: string | null | undefined): Date | null {
  if (typeof raw !== "string" || !raw.trim()) return null;
  let text = raw.trim().replace(/\s*UTC$/i, "Z").replace(" ", "T");
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/i.test(text);
  if (!hasZone && /T\d{2}:\d{2}/.test(text)) text += "Z";
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** `2026-10-26 06:20:13 UTC` — full precision, for the scan timeline. */
export function formatUtc(date: Date): string {
  return `${date.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

/** `Oct 26, 06:20 UTC` — compact, for lists. Fixed locale so output is stable. */
export function formatUtcShort(date: Date): string {
  const text = date.toLocaleString("en-US", {
    timeZone: "UTC",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  return `${text} UTC`;
}

/** `1h 2m 3s`, `4m 5s`, `6s`. Negative or non-finite input reads as `0s`. */
export function formatElapsed(totalSeconds: number): string {
  const seconds = Number.isFinite(totalSeconds) && totalSeconds > 0 ? Math.floor(totalSeconds) : 0;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}h ${m}m ${s}s`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}
