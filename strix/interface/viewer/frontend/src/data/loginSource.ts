/**
 * Client for the viewer's sign-in endpoints (the server enforces everything;
 * this only talks to it).
 *
 *   GET  /api/login/status   what to show: is login on, is this browser signed in
 *   POST /api/login          username + password -> a login cookie (HttpOnly)
 *   POST /api/logout         end the login session
 *
 * The password is sent once over the request body and is never stored, logged,
 * or kept in state after the request returns.
 */

export interface LoginStatus {
  /** The server wants a username and password. */
  loginRequired: boolean;
  /** This browser may use the viewer right now. */
  authenticated: boolean;
  /** This browser opened the viewer's secret link (the first factor). */
  authorizedBrowser: boolean;
  /** The signed-in username, only present once signed in. */
  username: string | null;
}

/**
 * GET /api/login/status. Resolves to `null` when the status cannot be read (an
 * older viewer without the endpoint, or the network is down), so a caller can
 * tell "unknown" from "signed out" and avoid signing the user out by mistake.
 */
export async function fetchLoginStatus(signal?: AbortSignal): Promise<LoginStatus | null> {
  try {
    const res = await fetch("/api/login/status", {
      cache: "no-store",
      credentials: "same-origin",
      signal,
    });
    if (!res.ok) return null;
    const r = (await res.json()) as Record<string, unknown>;
    return {
      loginRequired: r.login_required === true,
      authenticated: r.authenticated === true,
      authorizedBrowser: r.authorized_browser === true,
      username: typeof r.username === "string" && r.username ? r.username : null,
    };
  } catch {
    return null;
  }
}

/** How a sign-in attempt ended. */
export type SignInOutcome =
  | { kind: "ok"; username: string }
  | { kind: "invalid"; message: string }
  | { kind: "locked"; retryAfterSeconds: number }
  | { kind: "unauthorized-browser" }
  | { kind: "error"; message: string };

const GENERIC_FAILURE = "Could not sign in. Please try again.";

/** POST /api/login. Never throws: every failure is a typed outcome. */
export async function signIn(username: string, password: string): Promise<SignInOutcome> {
  let res: Response;
  try {
    res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify({ username, password }),
    });
  } catch {
    return { kind: "error", message: "Could not reach the viewer. Is it still running?" };
  }

  let data: Record<string, unknown> = {};
  try {
    const parsed = await res.json();
    if (parsed && typeof parsed === "object") data = parsed as Record<string, unknown>;
  } catch {
    /* empty or non-JSON body */
  }

  if (res.ok && data.ok === true) {
    return { kind: "ok", username: typeof data.username === "string" ? data.username : username };
  }
  if (res.status === 401) {
    return {
      kind: "invalid",
      message:
        typeof data.message === "string" && data.message
          ? data.message
          : "Incorrect username or password.",
    };
  }
  if (res.status === 429) {
    const header = Number(res.headers.get("Retry-After"));
    const body = typeof data.retry_after === "number" ? data.retry_after : NaN;
    const seconds = [body, header].find((n) => Number.isFinite(n) && n > 0) ?? 60;
    return { kind: "locked", retryAfterSeconds: Math.ceil(seconds) };
  }
  if (res.status === 403 && data.error === "forbidden") return { kind: "unauthorized-browser" };
  return { kind: "error", message: GENERIC_FAILURE };
}

/** POST /api/logout. Best effort: signing out locally must work even if this fails. */
export async function signOut(): Promise<void> {
  try {
    await fetch("/api/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      cache: "no-store",
      body: "{}",
    });
  } catch {
    /* the server forgets the session when it expires; the UI still leaves */
  }
}
