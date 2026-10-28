import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, Check, Copy, Radio, ScrollText, Search, WifiOff } from "lucide-react";
import { cn } from "@/lib/utils";
import { formatElapsed, formatUtc, parseTimestamp } from "@/lib/time";
import { useRunLog, type LogRow } from "@/components/live/useRunLog";

/**
 * "Server log": the scan's own log, streamed live, so it is clear what is
 * running on the server side. It reads <run>/strix.log through GET /api/log, so
 * it works for scans started from this viewer and from the command line alike.
 *
 * Quiet is not the same as stuck — a local model can think for minutes between
 * lines — so the header says how long since the log last changed instead of
 * guessing, and warns only after a long silence.
 */

interface ServerLogPanelProps {
  runName: string | null;
  finished: boolean;
  /** Agent id -> display name, from the transcript, so lines show names not ids. */
  agentNames: ReadonlyMap<string, string>;
}

type LevelFilter = "debug" | "info" | "warning" | "error";

const LEVEL_RANK: Record<string, number> = {
  DEBUG: 10,
  INFO: 20,
  WARNING: 30,
  ERROR: 40,
  CRITICAL: 50,
};
const FILTER_MIN: Record<LevelFilter, number> = { debug: 0, info: 20, warning: 30, error: 40 };
const LEVEL_OPTIONS: Array<{ value: LevelFilter; label: string }> = [
  { value: "debug", label: "Everything (debug)" },
  { value: "info", label: "Info and above" },
  { value: "warning", label: "Warnings and errors" },
  { value: "error", label: "Errors only" },
];

/** A scan with no output for this long gets a hint (not a verdict). */
const QUIET_WARN_SECONDS = 90;
/** Within this many pixels of the bottom counts as "following". */
const FOLLOW_SLACK_PX = 24;
const ALL_AGENTS = "__all__";
const NO_AGENT = "__system__";

const LEVEL_STYLE: Record<string, string> = {
  DEBUG: "text-[#666]",
  INFO: "text-sky-400",
  WARNING: "text-amber-400",
  ERROR: "text-red-400",
  CRITICAL: "text-red-300",
};

function levelRank(level: string): number {
  return LEVEL_RANK[level.toUpperCase()] ?? 20;
}

/** `13:20:13` in UTC, with the full timestamp on hover. */
function clock(ts: string | null): { short: string; full: string } {
  const date = parseTimestamp(ts);
  return date
    ? { short: date.toISOString().slice(11, 19), full: formatUtc(date) }
    : { short: "--:--:--", full: "" };
}

function agentKey(row: LogRow): string {
  return row.entry.agent ?? NO_AGENT;
}

function shortId(id: string): string {
  return id.length > 14 ? `${id.slice(0, 6)}…${id.slice(-4)}` : id;
}

function rowText(row: LogRow): string {
  const { short } = clock(row.entry.ts);
  return [`${short} ${row.entry.level} ${row.entry.logger}: ${row.entry.message}`, ...row.extra].join("\n");
}

export function ServerLogPanel({ runName, finished, agentNames }: ServerLogPanelProps) {
  const { rows, loaded, exists, olderHidden, connected, quietSeconds } = useRunLog(runName, finished);

  const [level, setLevel] = useState<LevelFilter>("info");
  const [agent, setAgent] = useState<string>(ALL_AGENTS);
  const [search, setSearch] = useState("");
  const [follow, setFollow] = useState(true);
  const [unseen, setUnseen] = useState(0);
  const [copied, setCopied] = useState(false);

  const scroller = useRef<HTMLDivElement>(null);
  const seenCount = useRef(0);

  const agentOptions = useMemo(() => {
    const keys = new Set<string>();
    for (const row of rows) keys.add(agentKey(row));
    return [...keys].sort((a, b) => label(a).localeCompare(label(b)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, agentNames]);

  function label(key: string): string {
    if (key === NO_AGENT) return "System";
    return agentNames.get(key) ?? shortId(key);
  }

  const visible = useMemo(() => {
    const min = FILTER_MIN[level];
    const needle = search.trim().toLowerCase();
    return rows.filter((row) => {
      if (levelRank(row.entry.level) < min) return false;
      if (agent !== ALL_AGENTS && agentKey(row) !== agent) return false;
      if (!needle) return true;
      return (
        row.entry.message.toLowerCase().includes(needle) ||
        row.entry.logger.toLowerCase().includes(needle) ||
        row.extra.some((line) => line.toLowerCase().includes(needle))
      );
    });
  }, [rows, level, agent, search]);

  // Stay on the newest line while following; otherwise count what arrived.
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    if (follow) {
      el.scrollTop = el.scrollHeight;
      seenCount.current = visible.length;
      setUnseen(0);
    } else {
      setUnseen(Math.max(0, visible.length - seenCount.current));
    }
  }, [visible, follow]);

  // Leaving the bottom pauses following; returning to it resumes.
  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= FOLLOW_SLACK_PX;
    setFollow((prev) => (prev === atBottom ? prev : atBottom));
  };

  const jumpToLatest = () => {
    setFollow(true);
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  };

  useEffect(() => {
    if (!copied) return;
    const id = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(id);
  }, [copied]);

  const copyVisible = async () => {
    try {
      await navigator.clipboard.writeText(visible.map(rowText).join("\n"));
      setCopied(true);
    } catch {
      /* clipboard can be unavailable on a non-secure origin; nothing to do */
    }
  };

  const quiet = !finished && quietSeconds !== null && quietSeconds >= QUIET_WARN_SECONDS;
  const filtered = rows.length - visible.length;

  return (
    <section
      data-testid="server-log"
      aria-label="Server log"
      className="rounded-xl border border-[#222] bg-[rgba(255,255,255,0.02)]"
    >
      <header className="space-y-3 border-b border-[#1c1c1c] p-4">
        <div className="flex flex-wrap items-center gap-2">
          <ScrollText className="h-4 w-4 text-[#888]" aria-hidden="true" />
          <h2 className="text-sm font-semibold text-white">Server log</h2>
          <StatusChip finished={finished} exists={exists} quietSeconds={quietSeconds} />
          <button
            type="button"
            onClick={() => void copyVisible()}
            disabled={visible.length === 0}
            className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-[#2a2a2a] px-2 py-1 text-xs text-[#bbb] transition-colors hover:border-[#444] hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
          >
            {copied ? <Check className="h-3.5 w-3.5" aria-hidden="true" /> : <Copy className="h-3.5 w-3.5" aria-hidden="true" />}
            {copied ? "Copied" : "Copy"}
          </button>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <select
            aria-label="Log level"
            data-testid="server-log-level"
            value={level}
            onChange={(e) => setLevel(e.target.value as LevelFilter)}
            className="rounded-md border border-[#2a2a2a] bg-black px-2 py-1 text-xs text-white focus:outline-none focus:ring-2 focus:ring-white/20"
          >
            {LEVEL_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          <select
            aria-label="Agent"
            data-testid="server-log-agent"
            value={agent}
            onChange={(e) => setAgent(e.target.value)}
            className="max-w-[14rem] rounded-md border border-[#2a2a2a] bg-black px-2 py-1 text-xs text-white focus:outline-none focus:ring-2 focus:ring-white/20"
          >
            <option value={ALL_AGENTS}>All agents</option>
            {agentOptions.map((key) => (
              <option key={key} value={key}>
                {label(key)}
              </option>
            ))}
          </select>
          <label className="relative min-w-[10rem] flex-1">
            <span className="sr-only">Search the log</span>
            <Search
              className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[#666]"
              aria-hidden="true"
            />
            <input
              type="search"
              data-testid="server-log-search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search"
              className="w-full rounded-md border border-[#2a2a2a] bg-black py-1 pl-7 pr-2 text-xs text-white placeholder:text-[#555] focus:outline-none focus:ring-2 focus:ring-white/20"
            />
          </label>
        </div>

        {quiet && (
          <p
            role="status"
            data-testid="server-log-quiet"
            className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-200"
          >
            No output for {formatElapsed(quietSeconds ?? 0)}. The scan may be waiting on the LLM,
            which can take a while with a local model. If this keeps growing, check that the model
            server is running.
          </p>
        )}
        {!connected && (
          <p
            role="status"
            data-testid="server-log-offline"
            className="flex items-center gap-2 rounded-md border border-red-500/30 bg-red-500/5 px-3 py-2 text-xs text-red-300"
          >
            <WifiOff className="h-3.5 w-3.5 flex-shrink-0" aria-hidden="true" />
            Lost contact with the viewer. Retrying…
          </p>
        )}
      </header>

      <div className="relative">
        <div
          ref={scroller}
          onScroll={onScroll}
          role="log"
          aria-label="Server log lines"
          aria-live="off"
          tabIndex={0}
          data-testid="server-log-lines"
          className="h-[420px] overflow-y-auto overflow-x-hidden bg-black/40 px-3 py-2 font-mono text-[12px] leading-5 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-white/20"
        >
          {olderHidden && (
            <p className="pb-2 text-center text-[11px] text-[#555]">
              Showing the most recent lines only.
            </p>
          )}
          {visible.length === 0 ? (
            <EmptyState
              loaded={loaded}
              exists={exists}
              finished={finished}
              total={rows.length}
              hidden={filtered}
            />
          ) : (
            visible.map((row) => <LogLine key={row.id} row={row} agentLabel={label(agentKey(row))} />)
          )}
        </div>

        {!follow && (
          <button
            type="button"
            onClick={jumpToLatest}
            data-testid="server-log-jump"
            className="absolute bottom-3 right-4 inline-flex items-center gap-1.5 rounded-full border border-[#3a3a3a] bg-[#111] px-3 py-1.5 text-xs font-medium text-white shadow-lg transition-colors hover:border-[#666]"
          >
            <ArrowDown className="h-3.5 w-3.5" aria-hidden="true" />
            {unseen > 0 ? `${unseen} new line${unseen === 1 ? "" : "s"}` : "Jump to latest"}
          </button>
        )}
      </div>

      <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-[#1c1c1c] px-4 py-2 text-[11px] text-[#666]">
        <span>
          {visible.length} line{visible.length === 1 ? "" : "s"}
          {filtered > 0 ? ` shown, ${filtered} hidden by filters` : ""}
        </span>
        <span>Times are UTC.</span>
      </footer>
    </section>
  );
}

function StatusChip({
  finished,
  exists,
  quietSeconds,
}: {
  finished: boolean;
  exists: boolean;
  quietSeconds: number | null;
}) {
  if (finished) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-[#888]">
        <span className="h-1.5 w-1.5 rounded-full bg-[#555]" aria-hidden="true" />
        Scan ended
      </span>
    );
  }
  if (!exists) {
    return <span className="text-xs text-[#888]">Waiting for the scan to start logging…</span>;
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-emerald-400" data-testid="server-log-live">
      <Radio className="h-3.5 w-3.5" aria-hidden="true" />
      Live
      {quietSeconds !== null && (
        <span className="text-[#888]">· last output {formatElapsed(quietSeconds)} ago</span>
      )}
    </span>
  );
}

function EmptyState({
  loaded,
  exists,
  finished,
  total,
  hidden,
}: {
  loaded: boolean;
  exists: boolean;
  finished: boolean;
  total: number;
  hidden: number;
}) {
  let text: string;
  if (!loaded) text = "Loading the log…";
  else if (total > 0 && hidden > 0) text = "No lines match the current filters.";
  else if (!exists) {
    text = finished
      ? "This run has no log file."
      : "The scan has not written its log yet. It appears as soon as the scan starts up.";
  } else text = "The log is empty so far.";
  return (
    <p role="status" className="py-10 text-center font-sans text-sm text-[#666]">
      {text}
    </p>
  );
}

const LogLine = memo(function LogLine({ row, agentLabel }: { row: LogRow; agentLabel: string }) {
  const { entry, extra } = row;
  const time = clock(entry.ts);
  const levelClass = LEVEL_STYLE[entry.level.toUpperCase()] ?? "text-[#888]";
  const loud = levelRank(entry.level) >= 40;
  return (
    <div className={cn("py-px", loud && "bg-red-500/[0.06]")}>
      <div className="flex gap-2">
        <time className="flex-none tabular-nums text-[#555]" title={time.full} dateTime={entry.ts ?? undefined}>
          {time.short}
        </time>
        <span className={cn("w-[3.6rem] flex-none font-semibold", levelClass)}>
          {entry.level.slice(0, 5).toUpperCase()}
        </span>
        {entry.agent && (
          <span className="max-w-[9rem] flex-none truncate text-violet-300" title={entry.agent}>
            {agentLabel}
          </span>
        )}
        <span className="min-w-0 flex-1 whitespace-pre-wrap break-words text-[#d4d4d4]">
          <span className="text-[#666]">{entry.logger.replace(/^strix\./, "")}: </span>
          {entry.message}
        </span>
      </div>
      {extra.length > 0 && (
        <pre className="ml-[4.5rem] whitespace-pre-wrap break-words text-[#9a9a9a]">{extra.join("\n")}</pre>
      )}
    </div>
  );
});

export default ServerLogPanel;
