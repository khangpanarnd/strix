import type { Vulnerability } from "@/types/issues";
import {
  parseRunJson,
  parseVulnerabilitiesJson,
  type ParsedRunSummary,
} from "@/lib/local-run-parser";

/**
 * Data seam for the local viewer: fetches against the local Python server's
 * JSON endpoints (same origin, relative URLs), producing the in-memory
 * `LoadedRun` shape the UI renders plus a `finished` flag driving live polling.
 *
 * The server serves a live in-progress run and a finished one identically; the
 * only signal is `run.finished`.
 */

/** A transcript agent as emitted by GET /api/transcript (already parsed). */
export interface TranscriptAgent {
  id: string;
  name: string;
  parent_id: string | null;
  status: string;
  created_at: string;
  updated_at: string;
}

/** Chat/tool event data as emitted by GET /api/transcript. */
export interface TranscriptEvent {
  id: string;
  type: "chat" | "tool";
  agent_id: string;
  timestamp: string;
  version: number;
  data: Record<string, unknown>;
}

export interface Transcript {
  agents: TranscriptAgent[];
  events: TranscriptEvent[];
}

/**
 * One MCP connection's non-secret status, as persisted to run.json by the
 * engine under `mcp_connection_status` and surfaced verbatim by GET /api/run.
 * Only name / provider / tool_count / dead ride here; never config, url, or
 * token. `dead` means the connection's live session gave up reconnecting.
 */
export interface McpConnectionStatus {
  name: string;
  provider: string | null;
  toolCount: number;
  dead: boolean;
}

/**
 * Read the MCP connection roster out of a raw run record. Tolerates the field
 * being absent (older runs, or a run with no MCP) and any malformed entry,
 * yielding an empty list rather than throwing.
 */
export function parseMcpConnectionStatus(raw: Record<string, unknown>): McpConnectionStatus[] {
  const list = raw?.mcp_connection_status;
  if (!Array.isArray(list)) return [];
  return list.flatMap((entry) => {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) return [];
    const record = entry as Record<string, unknown>;
    const name = typeof record.name === "string" ? record.name.trim() : "";
    if (!name) return [];
    const provider = typeof record.provider === "string" && record.provider.trim() ? record.provider.trim() : null;
    const toolCount = typeof record.tool_count === "number" ? record.tool_count : 0;
    const dead = record.dead === true;
    return [{ name, provider, toolCount, dead }];
  });
}

export interface LoadedRun {
  summary: ParsedRunSummary;
  /** Whole raw run record (for llm_usage, targets_info details, etc.). */
  raw: Record<string, unknown>;
  finished: boolean;
  vulnerabilities: Vulnerability[];
  reportMarkdown: string | null;
  transcript: Transcript;
}

async function getJson(path: string): Promise<unknown> {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} responded ${res.status}`);
  return res.json();
}

/** Build a ``?run=<name>`` suffix for run-scoped data endpoints. */
function runQuery(runName?: string | null): string {
  return runName ? `?run=${encodeURIComponent(runName)}` : "";
}

export async function fetchRunSummary(runName?: string | null): Promise<{
  summary: ParsedRunSummary;
  raw: Record<string, unknown>;
  finished: boolean;
}> {
  const raw = (await getJson("/api/run" + runQuery(runName))) as Record<string, unknown>;
  // parseRunJson tolerates extra keys and takes raw TEXT.
  const summary = parseRunJson(JSON.stringify(raw));
  const finished = raw.finished === true;
  return { summary, raw, finished };
}

export async function fetchVulnerabilities(
  runId: string | null,
  runName?: string | null
): Promise<Vulnerability[]> {
  const arr = await getJson("/api/vulnerabilities" + runQuery(runName));
  return parseVulnerabilitiesJson(JSON.stringify(arr), runId);
}

export async function fetchReportMarkdown(runName?: string | null): Promise<string | null> {
  const obj = (await getJson("/api/report" + runQuery(runName))) as { markdown?: string };
  return obj?.markdown ?? null;
}

export async function fetchTranscript(runName?: string | null): Promise<Transcript> {
  const obj = (await getJson("/api/transcript" + runQuery(runName))) as Partial<Transcript>;
  return {
    agents: Array.isArray(obj?.agents) ? obj.agents : [],
    events: Array.isArray(obj?.events) ? obj.events : [],
  };
}

/** One-shot fetch of every endpoint (used on mount and on final settle). */
export async function fetchAll(runName?: string | null): Promise<LoadedRun> {
  const { summary, raw, finished } = await fetchRunSummary(runName);
  const [vulnerabilities, reportMarkdown, transcript] = await Promise.all([
    fetchVulnerabilities(summary.runId, runName).catch(() => [] as Vulnerability[]),
    fetchReportMarkdown(runName).catch(() => null),
    fetchTranscript(runName).catch(() => ({ agents: [], events: [] }) as Transcript),
  ]);
  return { summary, raw, finished, vulnerabilities, reportMarkdown, transcript };
}

// ---------------------------------------------------------------------------
// Run history + email auth + report send
//
// These endpoints back the "Your runs" sidebar section. Auth and report-send
// responses carry a meaningful JSON body on non-2xx statuses (an ``error``
// code), so they read the body regardless of status rather than throwing.
// ---------------------------------------------------------------------------

export interface RunSeverityCounts {
  critical: number;
  high: number;
  medium: number;
  low: number;
}

export interface RunListEntry {
  name: string;
  target: string | null;
  scan_mode: string | null;
  status: string | null;
  start_time: string | null;
  end_time: string | null;
  finished: boolean;
  severity_counts: RunSeverityCounts;
}

export interface RunsPayload {
  locked: boolean;
  count: number;
  runs: RunListEntry[];
}

export interface AuthStatus {
  verified: boolean;
  email: string | null;
}

export type OtpStartResult = { ok: true } | { ok: false; error: string };
export type OtpVerifyResult =
  | { verified: true; email: string }
  | { verified: false; error: string };
export type SendReportResult =
  | { ok: true; password: string; filename: string }
  | { ok: false; error: string };

async function postJson(
  path: string,
  body: Record<string, unknown>
): Promise<{ ok: boolean; status: number; data: Record<string, unknown> }> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  let data: Record<string, unknown> = {};
  try {
    const parsed = await res.json();
    if (parsed && typeof parsed === "object") data = parsed as Record<string, unknown>;
  } catch {
    /* empty or non-JSON body */
  }
  return { ok: res.ok, status: res.status, data };
}

export async function fetchRuns(): Promise<RunsPayload> {
  const obj = (await getJson("/api/runs")) as Partial<RunsPayload>;
  return {
    locked: obj?.locked ?? true,
    count: typeof obj?.count === "number" ? obj.count : 0,
    runs: Array.isArray(obj?.runs) ? (obj.runs as RunListEntry[]) : [],
  };
}

/** The scan modes the launcher offers, narrowest-to-deepest. */
export type ScanMode = "quick" | "standard" | "deep";

/**
 * Server-advertised launch limits (GET /api/capabilities → scan_limits), so the
 * form never hard-codes a budget ceiling or an instruction cap: the server is
 * the single source of truth and the UI mirrors it (frontend-components §1).
 */
export interface ScanLimits {
  max_budget_usd: number;
  default_budget_usd: number;
  modes: ScanMode[];
  instruction_max: number;
}

export interface Capabilities {
  can_steer: boolean;
  /** Launch endpoints exist only when the viewer was started in launch mode (BR-5). */
  scan_launch_available: boolean;
  /**
   * False only while the workspace has no run at all. When false the UI must
   * not start the run-polling loop (it would 404 every 500 ms) and opens the
   * launch view instead (frontend-components §1, §4.1).
   */
  initial_run: boolean;
  /** Present only in launch mode; the form reads its bounds from here. */
  scan_limits: ScanLimits | null;
}

/** The form values POSTed to /api/scans (domain-entities §3). */
export interface LaunchValues {
  target: string;
  scan_mode: ScanMode;
  max_budget_usd: number;
  instruction: string;
  authorized: boolean;
  allow_private: boolean;
}

/** Lifecycle of a viewer-launched (or observed) scan (domain-entities ScanState). */
export type ScanState =
  | "starting"
  | "running"
  | "stopping"
  | "finished"
  | "failed"
  | "stopped"
  | "unknown";

/** Terminal states — polling stops once a scan reaches one of these. */
export const TERMINAL_SCAN_STATES: ReadonlySet<ScanState> = new Set<ScanState>([
  "finished",
  "failed",
  "stopped",
]);

/** GET /api/scans/status response shape (domain-entities §3). */
export interface ScanStatus {
  state: ScanState;
  exit_code: number | null;
  stoppable: boolean;
  error_tail: string | null;
}

/**
 * A structured launch failure. Thrown by `launchScan` on any non-202 response;
 * `error` is a code from the business-rules error table and the UI maps it to
 * the field it belongs to. `run` is present only for `scan_active` (409).
 */
export class LaunchError extends Error {
  readonly code: string;
  readonly status: number;
  readonly run?: string;

  constructor(code: string, message: string, status: number, run?: string) {
    super(message);
    this.name = "LaunchError";
    this.code = code;
    this.status = status;
    this.run = run;
  }
}

/** The four allow-listed downloadable artifacts (BR-32); the keys are fixed. */
export type ArtifactKey =
  | "report.html"
  | "penetration_test_report.md"
  | "findings.sarif"
  | "vulnerabilities.json";

export const ARTIFACT_KEYS: readonly ArtifactKey[] = [
  "report.html",
  "penetration_test_report.md",
  "findings.sarif",
  "vulnerabilities.json",
] as const;

export type SteerResult = { ok: true } | { ok: false; error: string };

const SCAN_MODES: readonly ScanMode[] = ["quick", "standard", "deep"] as const;

function isScanMode(value: unknown): value is ScanMode {
  return typeof value === "string" && (SCAN_MODES as readonly string[]).includes(value);
}

function parseScanLimits(raw: unknown): ScanLimits | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  const maxBudget = typeof r.max_budget_usd === "number" && Number.isFinite(r.max_budget_usd) ? r.max_budget_usd : 50;
  const defaultBudget =
    typeof r.default_budget_usd === "number" && Number.isFinite(r.default_budget_usd) ? r.default_budget_usd : 5;
  const modes = Array.isArray(r.modes) ? r.modes.filter(isScanMode) : [];
  const instructionMax = typeof r.instruction_max === "number" && r.instruction_max > 0 ? r.instruction_max : 4000;
  return {
    max_budget_usd: maxBudget,
    default_budget_usd: defaultBudget,
    modes: modes.length > 0 ? modes : [...SCAN_MODES],
    instruction_max: instructionMax,
  };
}

/**
 * GET /api/capabilities. `can_steer` is true only inside a live in-TUI scan;
 * `scan_launch_available` / `initial_run` / `scan_limits` drive the launch UI.
 * Capabilities are read once per session (they do not change).
 */
export async function fetchCapabilities(): Promise<Capabilities> {
  const obj = (await getJson("/api/capabilities")) as Partial<Capabilities> & Record<string, unknown>;
  return {
    can_steer: obj?.can_steer === true,
    scan_launch_available: obj?.scan_launch_available === true,
    // initial_run defaults to true so a server that predates this field keeps
    // the existing polling behaviour rather than silently suppressing it.
    initial_run: obj?.initial_run !== false,
    scan_limits: parseScanLimits((obj as Record<string, unknown>)?.scan_limits),
  };
}

/**
 * POST /api/scans. Resolves to the new run name on 202, otherwise throws a
 * `LaunchError` carrying the server's error code, message, HTTP status and
 * (for scan_active) the conflicting run name. Same-origin with the session
 * cookie; the UI never sends `authorized: true` unless the box is ticked.
 */
export async function launchScan(values: LaunchValues): Promise<{ run_name: string }> {
  const res = await fetch("/api/scans", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    cache: "no-store",
    body: JSON.stringify(values),
  });
  let data: Record<string, unknown> = {};
  try {
    const parsed = await res.json();
    if (parsed && typeof parsed === "object") data = parsed as Record<string, unknown>;
  } catch {
    /* empty or non-JSON body */
  }
  if (res.status === 202 && typeof data.run_name === "string") {
    return { run_name: data.run_name };
  }
  const code = typeof data.error === "string" ? data.error : "unavailable";
  const message =
    typeof data.message === "string" && data.message
      ? data.message
      : "The scan could not be started. Please try again.";
  const run = typeof data.run === "string" ? data.run : undefined;
  throw new LaunchError(code, message, res.status, run);
}

/** POST /api/scans/stop. Best-effort: resolves true on 202, false otherwise. */
export async function stopScan(run: string): Promise<boolean> {
  const res = await fetch("/api/scans/stop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    cache: "no-store",
    body: JSON.stringify({ run }),
  });
  return res.status === 202;
}

/**
 * GET /api/scans/status?run=. Returns a normalised `ScanStatus`; an unknown or
 * unreachable run resolves to the `unknown`, non-stoppable shape so callers can
 * treat "no live child" uniformly (used for CLI-started runs, FR-27).
 */
export async function fetchScanStatus(run: string): Promise<ScanStatus> {
  const res = await fetch("/api/scans/status?run=" + encodeURIComponent(run), {
    cache: "no-store",
    credentials: "same-origin",
  });
  if (!res.ok) {
    return { state: "unknown", exit_code: null, stoppable: false, error_tail: null };
  }
  const obj = (await res.json()) as Partial<ScanStatus>;
  const state: ScanState = isScanState(obj?.state) ? obj.state : "unknown";
  return {
    state,
    exit_code: typeof obj?.exit_code === "number" ? obj.exit_code : null,
    stoppable: obj?.stoppable === true,
    error_tail: typeof obj?.error_tail === "string" ? obj.error_tail : null,
  };
}

const SCAN_STATES: readonly ScanState[] = [
  "starting",
  "running",
  "stopping",
  "finished",
  "failed",
  "stopped",
  "unknown",
];

function isScanState(value: unknown): value is ScanState {
  return typeof value === "string" && (SCAN_STATES as readonly string[]).includes(value);
}

/**
 * GET /api/run/artifacts?run=. Returns the subset of the four allow-listed
 * files that exist for the run (BR-32); tolerates a 404 (no run yet) or any
 * malformed payload by returning an empty list.
 */
export async function fetchArtifacts(run: string): Promise<ArtifactKey[]> {
  try {
    const res = await fetch("/api/run/artifacts?run=" + encodeURIComponent(run), {
      cache: "no-store",
      credentials: "same-origin",
    });
    if (!res.ok) return [];
    const obj = (await res.json()) as { available?: unknown };
    if (!Array.isArray(obj?.available)) return [];
    const allowed = new Set<string>(ARTIFACT_KEYS);
    return obj.available.filter((k): k is ArtifactKey => typeof k === "string" && allowed.has(k));
  } catch {
    return [];
  }
}

/**
 * Build the download URL for one allow-listed artifact. The browser fetches it
 * directly (plain `<a download>`), so the session cookie rides automatically.
 */
export function artifactUrl(run: string, name: ArtifactKey): string {
  return `/api/run/artifact?run=${encodeURIComponent(run)}&name=${encodeURIComponent(name)}`;
}

/** POST /api/agents/steer. Sends a steering instruction to a running agent. */
export async function steerAgent(agentId: string, message: string): Promise<SteerResult> {
  const { ok, data } = await postJson("/api/agents/steer", {
    agent_id: agentId,
    message,
  });
  if (ok && data.ok === true) return { ok: true };
  return { ok: false, error: String(data.error ?? "unavailable") };
}

export type SubmitFeedbackResult = { ok: true } | { ok: false; error: string };

/**
 * POST /api/feedback. Sends a feedback message plus a work email (no
 * verification) to the local server, which relays it to Strix.
 */
export async function submitFeedback(
  message: string,
  email: string
): Promise<SubmitFeedbackResult> {
  const { ok, data } = await postJson("/api/feedback", { message, email });
  if (ok && data.ok === true) return { ok: true };
  return { ok: false, error: String(data.error ?? "unavailable") };
}

export async function fetchAuthStatus(): Promise<AuthStatus> {
  const obj = (await getJson("/api/auth/status")) as Partial<AuthStatus>;
  return { verified: obj?.verified === true, email: obj?.email ?? null };
}

export async function otpStart(email: string): Promise<OtpStartResult> {
  const { ok, data } = await postJson("/api/auth/otp/start", { email });
  if (ok && data.ok === true) return { ok: true };
  return { ok: false, error: String(data.error ?? "unavailable") };
}

export async function otpVerify(email: string, code: string): Promise<OtpVerifyResult> {
  const { ok, data } = await postJson("/api/auth/otp/verify", { email, code });
  if (ok && data.verified === true) {
    return { verified: true, email: String(data.email ?? email) };
  }
  return { verified: false, error: String(data.error ?? "invalid_code") };
}

export async function forgetAuth(): Promise<void> {
  await postJson("/api/auth/forget", {});
}

export async function sendReport(runName?: string | null): Promise<SendReportResult> {
  const { ok, data } = await postJson("/api/report/send", runName ? { run: runName } : {});
  if (ok && data.ok === true) {
    return {
      ok: true,
      password: String(data.password ?? ""),
      filename: String(data.filename ?? "strix-report.pdf"),
    };
  }
  return { ok: false, error: String(data.error ?? "unavailable") };
}
