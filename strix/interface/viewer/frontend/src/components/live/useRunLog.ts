import { useEffect, useRef, useState } from "react";
import { fetchRunLog, type RunLogChunk, type RunLogEntry } from "@/data/serverSource";
import { parseTimestamp } from "@/lib/time";

/**
 * Streams a run's server log by polling GET /api/log with a byte-offset cursor.
 *
 * - The first request has no cursor and returns the most recent lines; every
 *   later request sends the previous `offset` and receives only new lines.
 * - While the scan is running it polls every second (every 5 s in a hidden tab),
 *   immediately again when the server says more is waiting, and backs off on
 *   errors. When the scan finishes it does one more pass to pick up the last
 *   lines, then stops.
 * - The cursor lives in refs so a re-render never loses or repeats a line.
 */

/** One displayed record: the log line plus any traceback lines that followed it. */
export interface LogRow {
  id: number;
  entry: RunLogEntry;
  extra: string[];
}

/** Rows kept in memory; older ones are dropped so a long scan cannot grow the page forever. */
export const MAX_LOG_ROWS = 1500;

const POLL_VISIBLE_MS = 1000;
const POLL_HIDDEN_MS = 5000;
const FINAL_PASS_MS = 1000;
const MAX_BACKOFF_MS = 10_000;

export interface RunLogState {
  rows: LogRow[];
  /** The first response has arrived. */
  loaded: boolean;
  /** The scan has created its log file. */
  exists: boolean;
  /** Earlier lines exist that this view does not show. */
  olderHidden: boolean;
  /** False after repeated failures to reach the server. */
  connected: boolean;
  /** Seconds since the log last changed, corrected for the client/server clock difference. */
  quietSeconds: number | null;
}

/** Add `entries` to `rows`, folding continuation lines into the record before them. */
export function appendRows(
  rows: LogRow[],
  entries: RunLogEntry[],
  nextId: () => number
): LogRow[] {
  const next = rows.slice();
  for (const entry of entries) {
    const last = next[next.length - 1];
    if (entry.continuation && last) {
      next[next.length - 1] = { ...last, extra: [...last.extra, entry.message] };
    } else {
      next.push({ id: nextId(), entry, extra: [] });
    }
  }
  return next;
}

export function useRunLog(runName: string | null, finished: boolean): RunLogState {
  const [rows, setRows] = useState<LogRow[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [exists, setExists] = useState(false);
  const [olderHidden, setOlderHidden] = useState(false);
  const [connected, setConnected] = useState(true);
  const [fresh, setFresh] = useState<{ ageAtReceive: number; receivedAt: number } | null>(null);
  const [tick, setTick] = useState(() => Date.now());

  const cursorRef = useRef<number | null>(null);
  const idRef = useRef(0);
  const rowsRef = useRef<LogRow[]>([]);
  const finishedRef = useRef(finished);
  finishedRef.current = finished;

  // A different run starts from scratch.
  useEffect(() => {
    cursorRef.current = null;
    idRef.current = 0;
    rowsRef.current = [];
    setRows([]);
    setLoaded(false);
    setExists(false);
    setOlderHidden(false);
    setConnected(true);
    setFresh(null);
  }, [runName]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    let finalPassDone = false;
    const controller = new AbortController();

    const schedule = (ms: number) => {
      timer = setTimeout(() => void poll(), ms);
    };

    const apply = (chunk: RunLogChunk) => {
      cursorRef.current = chunk.offset;
      setLoaded(true);
      setExists(chunk.exists);
      setConnected(true);

      const serverNow = parseTimestamp(chunk.now)?.getTime();
      const updated = parseTimestamp(chunk.updated_at)?.getTime();
      setFresh(
        serverNow !== undefined && updated !== undefined
          ? { ageAtReceive: Math.max(0, (serverNow - updated) / 1000), receivedAt: Date.now() }
          : null
      );

      if (chunk.reset) {
        idRef.current = 0;
        rowsRef.current = [];
        setOlderHidden(false);
      }
      if (chunk.truncated_head) setOlderHidden(true);
      if (chunk.entries.length === 0 && !chunk.reset) return;

      let merged = appendRows(rowsRef.current, chunk.entries, () => idRef.current++);
      if (merged.length > MAX_LOG_ROWS) {
        merged = merged.slice(merged.length - MAX_LOG_ROWS);
        setOlderHidden(true);
      }
      rowsRef.current = merged;
      setRows(merged);
    };

    const poll = async () => {
      if (cancelled) return;
      try {
        const chunk = await fetchRunLog(runName, cursorRef.current, controller.signal);
        if (cancelled) return;
        failures = 0;
        apply(chunk);

        if (chunk.more) {
          schedule(0);
        } else if (finishedRef.current) {
          // One extra pass after the scan ends picks up whatever it flushed last.
          if (!finalPassDone) {
            finalPassDone = true;
            schedule(FINAL_PASS_MS);
          }
        } else {
          finalPassDone = false;
          schedule(document.hidden ? POLL_HIDDEN_MS : POLL_VISIBLE_MS);
        }
      } catch {
        if (cancelled) return;
        failures += 1;
        if (failures >= 2) setConnected(false);
        schedule(Math.min(MAX_BACKOFF_MS, POLL_VISIBLE_MS * 2 ** failures));
      }
    };

    void poll();
    return () => {
      cancelled = true;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [runName, finished]);

  // Re-render each second while the scan runs so "last output Ns ago" stays current.
  useEffect(() => {
    if (finished) return;
    const id = setInterval(() => setTick(Date.now()), 1000);
    return () => clearInterval(id);
  }, [finished]);

  const quietSeconds = fresh ? fresh.ageAtReceive + Math.max(0, (tick - fresh.receivedAt) / 1000) : null;

  return { rows, loaded, exists, olderHidden, connected, quietSeconds };
}
