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
  /**
   * Whether the LLM Settings page is offered and, if so, whether it is
   * editable (loopback bind) or read-only (FR-3). Defaults to unavailable so a
   * server that predates this feature simply hides the Settings entry.
   */
  llm_settings: LlmSettingsCapability;
  /**
   * Whether the Notifications page is offered and editable (loopback bind).
   * Defaults to unavailable so a server that predates it simply hides the entry.
   */
  notifications: LlmSettingsCapability;
}

/** The `llm_settings` capability block (GET /api/capabilities, FR-3). */
export interface LlmSettingsCapability {
  available: boolean;
  editable: boolean;
}

/** The form values POSTed to /api/scans (domain-entities §3). */
export interface LaunchValues {
  target: string;
  scan_mode: ScanMode;
  /**
   * The budget ceiling in USD, or `null` for no limit. `null` is sent as JSON
   * `null` and the server runs the scan uncapped (the common case for local
   * LLMs, which have no per-token cost).
   */
  max_budget_usd: number | null;
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
    llm_settings: parseLlmSettings((obj as Record<string, unknown>)?.llm_settings),
    notifications: parseLlmSettings((obj as Record<string, unknown>)?.notifications),
  };
}

/**
 * Parse the `llm_settings` capability. Defaults to `{available:false,
 * editable:false}` when the field is absent (an older server), so the Settings
 * entry is simply hidden rather than erroring.
 */
function parseLlmSettings(raw: unknown): LlmSettingsCapability {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { available: false, editable: false };
  }
  const r = raw as Record<string, unknown>;
  return { available: r.available === true, editable: r.editable === true };
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

// ---------------------------------------------------------------------------
// LLM settings (unit: llm-settings)
//
// GET /api/settings, PUT /api/settings, POST /api/settings/reset and
// POST /api/settings/test back the Settings page. The server is the single
// source of truth: the key is write-only (never returned, never pre-filled),
// every value carries its source, and error responses carry a machine-readable
// code mapped to a field by the form (domain-entities §3, business-rules §8).
// ---------------------------------------------------------------------------

/**
 * Where an effective value comes from (domain-entities SettingsView):
 *   - `ui`          saved through this page (managed);
 *   - `file`        saved by the command line into the shared config file (DD-6);
 *   - `environment` set in the shell that started the viewer;
 *   - `default`     the Strix built-in default.
 */
export type FieldSource = "ui" | "file" | "environment" | "default";

/** A value-bearing field (model, base, reasoning) with its source. */
export interface SettingsValueField {
  value: string | null;
  source: FieldSource;
}

/** The API key field: never a value, only whether one is set and its source. */
export interface SettingsKeyField {
  set: boolean;
  source: FieldSource;
}

/** GET /api/settings (and the body returned after a save / clear / reset). */
export interface SettingsView {
  fields: {
    model: SettingsValueField;
    api_base: SettingsValueField;
    reasoning_effort: SettingsValueField;
    api_key: SettingsKeyField;
  };
  /** The managed marker is present: this page owns the connection (FR-15). */
  managed: boolean;
  /** Editable only on a loopback bind; otherwise read-only (FR-2). */
  editable: boolean;
  /** `unreadable` means the config file is not valid JSON; Save is blocked. */
  file_status: "ok" | "missing" | "unreadable";
  /** Display form of the config path, e.g. `~/.strix/cli-config.json`. */
  config_path: string;
  /** `win32` triggers the always-visible key-file note (NFR-3). */
  platform: string;
  /** The seven allowed reasoning-effort values, server-ordered. */
  reasoning_choices: string[];
  /** Static model-id suggestions for the datalist (DD-7). */
  model_suggestions: string[];
  /** A viewer-launched scan is running: saves apply to later scans only. */
  scan_active: boolean;
  /** Advisory save warnings (e.g. `model_unfamiliar`); present on a save. */
  warnings: string[];
}

/**
 * The PUT body. Any subset of the four fields plus a `clear` list naming
 * fields to remove. The key is included only when the user typed one; an
 * empty string is never sent (clearing is only via `clear`, BR-11).
 */
export interface SettingsUpdate {
  model?: string;
  api_key?: string;
  api_base?: string;
  reasoning_effort?: string;
  clear?: SettingsFieldName[];
}

/** The four field names that can appear in a `clear` list (BR-10). */
export type SettingsFieldName = "model" | "api_key" | "api_base" | "reasoning_effort";

/** POST /api/settings/test result (domain-entities TestResult). */
export interface TestResult {
  ok: boolean;
  latency_ms: number | null;
  category: string | null;
  status: number | null;
  message: string;
}

/**
 * A structured settings failure, mirroring `LaunchError`. `error` is a code
 * from the business-rules error table (§8) and the form maps it to the field
 * it belongs to; anything unmapped renders as a banner above the buttons.
 */
export class SettingsError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "SettingsError";
    this.code = code;
    this.status = status;
  }
}

function asFieldSource(value: unknown): FieldSource {
  return value === "ui" || value === "file" || value === "environment" || value === "default"
    ? value
    : "default";
}

function parseValueField(raw: unknown): SettingsValueField {
  if (!raw || typeof raw !== "object") return { value: null, source: "default" };
  const r = raw as Record<string, unknown>;
  const value = typeof r.value === "string" ? r.value : null;
  return { value, source: asFieldSource(r.source) };
}

function parseKeyField(raw: unknown): SettingsKeyField {
  if (!raw || typeof raw !== "object") return { set: false, source: "default" };
  const r = raw as Record<string, unknown>;
  return { set: r.set === true, source: asFieldSource(r.source) };
}

function parseFileStatus(value: unknown): SettingsView["file_status"] {
  return value === "ok" || value === "missing" || value === "unreadable" ? value : "ok";
}

function parseStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/** Normalise a raw settings payload into a fully-typed `SettingsView`. */
function parseSettingsView(raw: unknown): SettingsView {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const fields = (r.fields && typeof r.fields === "object" ? r.fields : {}) as Record<string, unknown>;
  return {
    fields: {
      model: parseValueField(fields.model),
      api_base: parseValueField(fields.api_base),
      reasoning_effort: parseValueField(fields.reasoning_effort),
      api_key: parseKeyField(fields.api_key),
    },
    managed: r.managed === true,
    editable: r.editable === true,
    file_status: parseFileStatus(r.file_status),
    config_path: typeof r.config_path === "string" ? r.config_path : "",
    platform: typeof r.platform === "string" ? r.platform : "",
    reasoning_choices: parseStringList(r.reasoning_choices),
    model_suggestions: parseStringList(r.model_suggestions),
    scan_active: r.scan_active === true,
    warnings: parseStringList(r.warnings),
  };
}

/** Build a `SettingsError` from a parsed error body and HTTP status. */
function settingsErrorFrom(data: Record<string, unknown>, status: number): SettingsError {
  const code = typeof data.error === "string" ? data.error : "unavailable";
  const message =
    typeof data.message === "string" && data.message
      ? data.message
      : "The settings could not be saved. Please try again.";
  return new SettingsError(code, message, status);
}

/** Shared same-origin JSON POST/PUT that reads the body on any status. */
async function sendSettingsJson(
  path: string,
  method: "PUT" | "POST",
  body: Record<string, unknown>,
  signal?: AbortSignal
): Promise<{ ok: boolean; status: number; data: Record<string, unknown> }> {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    cache: "no-store",
    body: JSON.stringify(body),
    signal,
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

/** GET /api/settings. Same-origin; tolerant parsing into a `SettingsView`. */
export async function fetchSettings(): Promise<SettingsView> {
  const res = await fetch("/api/settings", { cache: "no-store", credentials: "same-origin" });
  if (!res.ok) {
    let data: Record<string, unknown> = {};
    try {
      const parsed = await res.json();
      if (parsed && typeof parsed === "object") data = parsed as Record<string, unknown>;
    } catch {
      /* non-JSON error body */
    }
    throw settingsErrorFrom(data, res.status);
  }
  return parseSettingsView(await res.json());
}

/**
 * PUT /api/settings. Sends only the fields present in `update` (the caller
 * includes a field only when it changed, and the key only when typed).
 * Resolves to the refreshed view (with `warnings`) on 200, else throws a
 * `SettingsError` carrying the server's code, message and status.
 */
export async function saveSettings(update: SettingsUpdate): Promise<SettingsView> {
  const body: Record<string, unknown> = {};
  if (update.model !== undefined) body.model = update.model;
  if (update.api_key !== undefined) body.api_key = update.api_key;
  if (update.api_base !== undefined) body.api_base = update.api_base;
  if (update.reasoning_effort !== undefined) body.reasoning_effort = update.reasoning_effort;
  if (update.clear && update.clear.length > 0) body.clear = update.clear;
  const { ok, status, data } = await sendSettingsJson("/api/settings", "PUT", body);
  if (!ok) throw settingsErrorFrom(data, status);
  return parseSettingsView(data);
}

/** POST /api/settings/reset ("Reset to environment"). Throws on non-200. */
export async function resetSettings(): Promise<SettingsView> {
  const { ok, status, data } = await sendSettingsJson("/api/settings/reset", "POST", {
    confirm: true,
  });
  if (!ok) throw settingsErrorFrom(data, status);
  return parseSettingsView(data);
}

/**
 * POST /api/settings/test. Sends the same value fields as a save (no `clear`).
 * A 200 carries a `TestResult` even when `ok` is false (a provider rejection is
 * a successful test round-trip); a non-200 (e.g. 409 `test_in_progress`, 429,
 * 400 `key_required_for_base_change`) throws a `SettingsError`. The optional
 * `signal` lets the caller abort the request from the 45 s Cancel button.
 */
export async function testConnection(
  update: Omit<SettingsUpdate, "clear">,
  signal?: AbortSignal,
  profileId?: string
): Promise<TestResult> {
  const body: Record<string, unknown> = {};
  if (profileId !== undefined) body.profile_id = profileId;
  if (update.model !== undefined) body.model = update.model;
  if (update.api_key !== undefined) body.api_key = update.api_key;
  if (update.api_base !== undefined) body.api_base = update.api_base;
  if (update.reasoning_effort !== undefined) body.reasoning_effort = update.reasoning_effort;
  const { ok, status, data } = await sendSettingsJson("/api/settings/test", "POST", body, signal);
  if (!ok) throw settingsErrorFrom(data, status);
  return {
    ok: data.ok === true,
    latency_ms: typeof data.latency_ms === "number" ? data.latency_ms : null,
    category: typeof data.category === "string" ? data.category : null,
    status: typeof data.status === "number" ? data.status : null,
    message: typeof data.message === "string" ? data.message : "",
  };
}



// ---------------------------------------------------------------------------
// LLM profiles (llm-profiles-spec.md)
//
// Several named LLM profiles can be saved; exactly one is active. Scans
// launched from the viewer use the active profile. These endpoints extend the
// single-settings feature: the key stays write-only (never returned), changes
// need a loopback bind, and errors carry a machine-readable code surfaced via
// the same `SettingsError` class.
// ---------------------------------------------------------------------------

/** One saved LLM profile as returned by GET /api/settings/profiles. */
export interface LlmProfile {
  id: string;
  name: string;
  preset: string;
  model: string | null;
  api_base: string | null;
  reasoning_effort: string | null;
  /** Whether a key is saved for this profile. The key itself is never returned. */
  api_key_set: boolean;
  active: boolean;
}

/** A static preset the server advertises; it only pre-fills the editor form. */
export interface LlmPreset {
  id: string;
  label: string;
  model_prefix: string;
  example_models: string[];
  /** A base-URL hint (may be null); pre-filled into the base field when present. */
  default_base: string | null;
  needs_key: boolean;
  note: string;
}

/** GET /api/settings/profiles response (the whole profiles page state). */
export interface ProfilesView {
  profiles: LlmProfile[];
  active_id: string | null;
  presets: LlmPreset[];
  max_profiles: number;
  managed: boolean;
  editable: boolean;
  file_status: "ok" | "missing" | "unreadable";
  config_path: string;
  platform: string;
  scan_active: boolean;
  reasoning_choices: string[];
  model_suggestions: string[];
  /** Advisory save warnings (e.g. `model_unfamiliar`); present on a save. */
  warnings: string[];
}

/**
 * The POST /api/settings/profiles/save body. No `id` creates a profile; an
 * `id` updates one (omitted fields stay unchanged). The key is included only
 * when the user typed one; `clear` names fields to remove. Saving does NOT
 * activate the profile.
 */
export interface ProfileSaveBody {
  id?: string;
  name: string;
  preset?: string;
  model?: string;
  api_key?: string;
  api_base?: string;
  reasoning_effort?: string;
  clear?: Array<"api_key" | "api_base" | "reasoning_effort">;
}

function parseProfileEntry(raw: unknown): LlmProfile | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  const id = typeof r.id === "string" ? r.id : "";
  const name = typeof r.name === "string" ? r.name : "";
  if (!id || !name) return null;
  return {
    id,
    name,
    preset: typeof r.preset === "string" ? r.preset : "custom",
    model: typeof r.model === "string" ? r.model : null,
    api_base: typeof r.api_base === "string" ? r.api_base : null,
    reasoning_effort: typeof r.reasoning_effort === "string" ? r.reasoning_effort : null,
    api_key_set: r.api_key_set === true,
    active: r.active === true,
  };
}

function parsePresetEntry(raw: unknown): LlmPreset | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  const id = typeof r.id === "string" ? r.id : "";
  if (!id) return null;
  return {
    id,
    label: typeof r.label === "string" ? r.label : id,
    model_prefix: typeof r.model_prefix === "string" ? r.model_prefix : "",
    example_models: parseStringList(r.example_models),
    default_base: typeof r.default_base === "string" && r.default_base ? r.default_base : null,
    needs_key: r.needs_key === true,
    note: typeof r.note === "string" ? r.note : "",
  };
}

/** Normalise a raw profiles payload into a fully-typed `ProfilesView`. */
function parseProfilesView(raw: unknown): ProfilesView {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const profiles = Array.isArray(r.profiles)
    ? r.profiles.map(parseProfileEntry).filter((p): p is LlmProfile => p !== null)
    : [];
  const presets = Array.isArray(r.presets)
    ? r.presets.map(parsePresetEntry).filter((p): p is LlmPreset => p !== null)
    : [];
  return {
    profiles,
    active_id: typeof r.active_id === "string" ? r.active_id : null,
    presets,
    max_profiles: typeof r.max_profiles === "number" ? r.max_profiles : 20,
    managed: r.managed === true,
    editable: r.editable === true,
    file_status: parseFileStatus(r.file_status),
    config_path: typeof r.config_path === "string" ? r.config_path : "",
    platform: typeof r.platform === "string" ? r.platform : "",
    scan_active: r.scan_active === true,
    reasoning_choices: parseStringList(r.reasoning_choices),
    model_suggestions: parseStringList(r.model_suggestions),
    warnings: parseStringList(r.warnings),
  };
}

/**
 * GET /api/settings/profiles. Same-origin; throws a `SettingsError` on any
 * non-200 (the caller inspects `status` to fall back to the single-form page
 * on 404/403 from an older server).
 */
export async function fetchProfiles(): Promise<ProfilesView> {
  const res = await fetch("/api/settings/profiles", {
    cache: "no-store",
    credentials: "same-origin",
  });
  if (!res.ok) {
    let data: Record<string, unknown> = {};
    try {
      const parsed = await res.json();
      if (parsed && typeof parsed === "object") data = parsed as Record<string, unknown>;
    } catch {
      /* non-JSON error body */
    }
    throw settingsErrorFrom(data, res.status);
  }
  return parseProfilesView(await res.json());
}

/**
 * POST /api/settings/profiles/save. Sends only the fields present in `body`
 * (the caller includes a field only when it changed, and the key only when
 * typed). Resolves to the refreshed view (with `warnings`) on 200, else throws
 * a `SettingsError`.
 */
export async function saveProfile(body: ProfileSaveBody): Promise<ProfilesView> {
  const payload: Record<string, unknown> = { name: body.name };
  if (body.id !== undefined) payload.id = body.id;
  if (body.preset !== undefined) payload.preset = body.preset;
  if (body.model !== undefined) payload.model = body.model;
  if (body.api_key !== undefined) payload.api_key = body.api_key;
  if (body.api_base !== undefined) payload.api_base = body.api_base;
  if (body.reasoning_effort !== undefined) payload.reasoning_effort = body.reasoning_effort;
  if (body.clear && body.clear.length > 0) payload.clear = body.clear;
  const { ok, status, data } = await sendSettingsJson(
    "/api/settings/profiles/save",
    "POST",
    payload
  );
  if (!ok) throw settingsErrorFrom(data, status);
  return parseProfilesView(data);
}

/**
 * POST /api/settings/profiles/activate. Mirrors the profile into the canonical
 * names and sets it active. Resolves to the refreshed view on 200, else throws.
 */
export async function activateProfile(id: string): Promise<ProfilesView> {
  const { ok, status, data } = await sendSettingsJson(
    "/api/settings/profiles/activate",
    "POST",
    { id }
  );
  if (!ok) throw settingsErrorFrom(data, status);
  return parseProfilesView(data);
}

/**
 * POST /api/settings/profiles/delete. 409 `cannot_delete_active_profile` for
 * the active one. Resolves to the refreshed view on 200, else throws.
 */
export async function deleteProfile(id: string): Promise<ProfilesView> {
  const { ok, status, data } = await sendSettingsJson(
    "/api/settings/profiles/delete",
    "POST",
    { id }
  );
  if (!ok) throw settingsErrorFrom(data, status);
  return parseProfilesView(data);
}


// ---------------------------------------------------------------------------
// Scan notifications (Slack / Microsoft Teams / Telegram)
//
// GET/PUT /api/notifications and POST /api/notifications/test back the
// Notifications page. Webhook URLs and bot tokens are write-only: they are
// sent on save/test but never returned; the view only says whether one is set
// (plus a webhook's host). Errors reuse `SettingsError` with the server's code.
// ---------------------------------------------------------------------------

export type NotificationChannelKey = "slack" | "teams" | "telegram";
export type NotificationEventKey = "started" | "progress" | "finished" | "stopped" | "failed";

export const NOTIFICATION_CHANNELS: readonly NotificationChannelKey[] = [
  "slack",
  "teams",
  "telegram",
] as const;
export const NOTIFICATION_EVENTS: readonly NotificationEventKey[] = [
  "started",
  "progress",
  "finished",
  "stopped",
  "failed",
] as const;

/** One destination as the server reports it. Never carries a secret. */
export interface NotificationChannelView {
  enabled: boolean;
  /** Everything needed to send is saved. */
  configured: boolean;
  /** Slack / Teams: the saved webhook's host (e.g. `hooks.slack.com`), else null. */
  host: string | null;
  /** Telegram: whether a bot token is saved. */
  token_set: boolean;
  /** Telegram: the saved chat id (not a secret), else an empty string. */
  chat_id: string;
}

/** GET /api/notifications (and the body returned after a save). */
export interface NotificationsView {
  enabled: boolean;
  events: Record<NotificationEventKey, boolean>;
  progress_interval_minutes: number;
  interval_min: number;
  interval_max: number;
  channels: Record<NotificationChannelKey, NotificationChannelView>;
  /** Editable only on a loopback bind; otherwise read-only. */
  editable: boolean;
  /** `unreadable` means the file is not valid JSON; Save is blocked. */
  file_status: "ok" | "missing" | "unreadable";
  config_path: string;
}

/** The PUT body: any subset; secrets are included only when the user typed one. */
export interface NotificationsUpdate {
  enabled?: boolean;
  events?: Partial<Record<NotificationEventKey, boolean>>;
  progress_interval_minutes?: number;
  channels?: {
    slack?: { enabled?: boolean; webhook_url?: string };
    teams?: { enabled?: boolean; webhook_url?: string };
    telegram?: { enabled?: boolean; bot_token?: string; chat_id?: string };
  };
  /** Secrets to remove, as `<channel>.<field>`. */
  clear?: Array<"slack.webhook_url" | "teams.webhook_url" | "telegram.bot_token" | "telegram.chat_id">;
}

/** The test body: a channel plus optional typed values that override saved ones. */
export interface NotificationTestBody {
  channel: NotificationChannelKey;
  webhook_url?: string;
  bot_token?: string;
  chat_id?: string;
}

/** POST /api/notifications/test. A failed delivery is `ok: false`, not an error. */
export interface NotificationTestResult {
  ok: boolean;
  message: string;
  status: number | null;
}

function parseNotificationChannel(raw: unknown): NotificationChannelView {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  return {
    enabled: r.enabled === true,
    configured: r.configured === true,
    host: typeof r.host === "string" && r.host ? r.host : null,
    token_set: r.token_set === true,
    chat_id: typeof r.chat_id === "string" ? r.chat_id : "",
  };
}

/** Normalise a raw notifications payload into a fully-typed `NotificationsView`. */
function parseNotificationsView(raw: unknown): NotificationsView {
  const r = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const events = (r.events && typeof r.events === "object" ? r.events : {}) as Record<string, unknown>;
  const channels = (r.channels && typeof r.channels === "object" ? r.channels : {}) as Record<
    string,
    unknown
  >;
  const interval = typeof r.progress_interval_minutes === "number" ? r.progress_interval_minutes : 15;
  return {
    enabled: r.enabled === true,
    events: Object.fromEntries(
      NOTIFICATION_EVENTS.map((key) => [key, events[key] === true])
    ) as Record<NotificationEventKey, boolean>,
    progress_interval_minutes: interval,
    interval_min: typeof r.interval_min === "number" ? r.interval_min : 5,
    interval_max: typeof r.interval_max === "number" ? r.interval_max : 240,
    channels: Object.fromEntries(
      NOTIFICATION_CHANNELS.map((key) => [key, parseNotificationChannel(channels[key])])
    ) as Record<NotificationChannelKey, NotificationChannelView>,
    editable: r.editable === true,
    file_status: parseFileStatus(r.file_status),
    config_path: typeof r.config_path === "string" ? r.config_path : "",
  };
}

/** GET /api/notifications. Throws a `SettingsError` on any non-200. */
export async function fetchNotifications(): Promise<NotificationsView> {
  const res = await fetch("/api/notifications", { cache: "no-store", credentials: "same-origin" });
  if (!res.ok) {
    let data: Record<string, unknown> = {};
    try {
      const parsed = await res.json();
      if (parsed && typeof parsed === "object") data = parsed as Record<string, unknown>;
    } catch {
      /* non-JSON error body */
    }
    throw settingsErrorFrom(data, res.status);
  }
  return parseNotificationsView(await res.json());
}

/** PUT /api/notifications. Resolves to the refreshed view, else throws a `SettingsError`. */
export async function saveNotifications(update: NotificationsUpdate): Promise<NotificationsView> {
  const { ok, status, data } = await sendSettingsJson(
    "/api/notifications",
    "PUT",
    update as Record<string, unknown>
  );
  if (!ok) throw settingsErrorFrom(data, status);
  return parseNotificationsView(data);
}

/**
 * POST /api/notifications/test. Resolves to the delivery result (also when the
 * destination rejected the message); throws a `SettingsError` for a bad request
 * (400) or the 6-per-minute limit (429).
 */
export async function testNotification(
  body: NotificationTestBody,
  signal?: AbortSignal
): Promise<NotificationTestResult> {
  const { ok, status, data } = await sendSettingsJson(
    "/api/notifications/test",
    "POST",
    body as unknown as Record<string, unknown>,
    signal
  );
  if (!ok) throw settingsErrorFrom(data, status);
  return {
    ok: data.ok === true,
    message: typeof data.message === "string" ? data.message : "",
    status: typeof data.status === "number" ? data.status : null,
  };
}
