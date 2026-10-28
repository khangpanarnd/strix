/**
 * Client for the viewer's sign-in endpoints (the server enforces everything;
 * this only talks to it).
 *
 *   GET  /api/login/status   what to show: is login on, signed in, change due
 *   POST /api/login          username + password -> a login cookie (HttpOnly)
 *   POST /api/password       change the signed-in account's password
 *   POST /api/logout         end the login session
 *
 * The password is sent once over the request body and is never stored, logged,
 * or kept in state after the request returns.
 */

export interface LoginStatus {
  /** The server wants a username and password. */
  loginRequired: boolean;
  /** This browser is signed in AND owes no forced password change. */
  authenticated: boolean;
  /** Signed in, but must replace the shipped default password before continuing. */
  mustChangePassword: boolean;
  /** The signed-in username, present once signed in (even if a change is due). */
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
      mustChangePassword: r.must_change_password === true,
      username: typeof r.username === "string" && r.username ? r.username : null,
    };
  } catch {
    return null;
  }
}

/** How a sign-in attempt ended. */
export type SignInOutcome =
  | { kind: "ok"; username: string; mustChangePassword: boolean }
  | { kind: "invalid"; message: string }
  | { kind: "locked"; retryAfterSeconds: number }
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
    return {
      kind: "ok",
      username: typeof data.username === "string" ? data.username : username,
      mustChangePassword: data.must_change_password === true,
    };
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
  return { kind: "error", message: GENERIC_FAILURE };
}

/** How a password change ended. */
export type ChangePasswordOutcome =
  | { kind: "ok"; username: string }
  | { kind: "wrong-current"; message: string }
  | { kind: "policy"; message: string }
  | { kind: "locked"; retryAfterSeconds: number }
  | { kind: "error"; message: string };

/**
 * POST /api/password. Replaces the signed-in account's password. Never throws:
 * every failure is a typed outcome.
 */
export async function changePassword(
  currentPassword: string,
  newPassword: string
): Promise<ChangePasswordOutcome> {
  let res: Response;
  try {
    res = await fetch("/api/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
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
    return { kind: "ok", username: typeof data.username === "string" ? data.username : "" };
  }
  if (res.status === 401) {
    return {
      kind: "wrong-current",
      message:
        typeof data.message === "string" && data.message
          ? data.message
          : "Your current password is incorrect.",
    };
  }
  if (res.status === 400) {
    return {
      kind: "policy",
      message:
        typeof data.message === "string" && data.message
          ? data.message
          : "That password cannot be used.",
    };
  }
  if (res.status === 429) {
    const header = Number(res.headers.get("Retry-After"));
    const body = typeof data.retry_after === "number" ? data.retry_after : NaN;
    const seconds = [body, header].find((n) => Number.isFinite(n) && n > 0) ?? 60;
    return { kind: "locked", retryAfterSeconds: Math.ceil(seconds) };
  }
  return { kind: "error", message: "Could not change the password. Please try again." };
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
