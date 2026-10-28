import { useEffect, useId, useRef, useState } from "react";
import { signIn } from "@/data/loginSource";
import "./landing.css";
import "./login.css";

/**
 * Sign-in page, shown between the landing page and the dashboard.
 *
 * The server does all the checking; this form only collects the username and
 * password and reports what happened. Nothing is pre-filled, the password is
 * cleared after any failed attempt, and a lock-out shows a live countdown.
 */

interface LoginPageProps {
  /** Signed in: continue to the app, or to the change-password step. */
  onSignedIn: (username: string, mustChangePassword: boolean) => void;
  /** Return to the landing page. */
  onBack: () => void;
  /** Shown above the form, e.g. "Your session expired." */
  notice?: string | null;
}

type Problem = { kind: "invalid" | "error"; message: string } | null;

export function LoginPage({ onSignedIn, onBack, notice }: LoginPageProps) {
  const ids = {
    username: useId(),
    password: useId(),
    problem: useId(),
  };
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [reveal, setReveal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<Problem>(null);
  const [lockedFor, setLockedFor] = useState(0);
  const userInput = useRef<HTMLInputElement>(null);
  const passwordInput = useRef<HTMLInputElement>(null);
  // Bumped to ask for focus on the password field after the inputs are re-enabled.
  const [focusPassword, setFocusPassword] = useState(0);

  useEffect(() => {
    if (focusPassword > 0 && !busy) passwordInput.current?.focus();
  }, [focusPassword, busy]);

  useEffect(() => {
    const previous = document.title;
    document.title = "Sign in · Strix";
    userInput.current?.focus();
    return () => {
      document.title = previous;
    };
  }, []);

  // Count a lock-out down once a second so the user knows when to try again.
  useEffect(() => {
    if (lockedFor <= 0) return;
    const id = setInterval(() => setLockedFor((n) => Math.max(0, n - 1)), 1000);
    return () => clearInterval(id);
  }, [lockedFor > 0]); // eslint-disable-line react-hooks/exhaustive-deps

  const locked = lockedFor > 0;
  const canSubmit = !busy && !locked && username.trim() !== "" && password !== "";

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setProblem(null);
    const outcome = await signIn(username.trim(), password);
    setBusy(false);

    if (outcome.kind === "ok") {
      setPassword("");
      onSignedIn(outcome.username, outcome.mustChangePassword);
      return;
    }
    // Never keep a rejected password around.
    setPassword("");
    if (outcome.kind === "invalid") {
      setProblem({ kind: "invalid", message: outcome.message });
      // Focus once the inputs are enabled again (a disabled input ignores focus()).
      setFocusPassword((n) => n + 1);
    } else if (outcome.kind === "locked") {
      setLockedFor(outcome.retryAfterSeconds);
    } else {
      setProblem({ kind: "error", message: outcome.message });
    }
  };

  const describedBy = problem || locked ? ids.problem : undefined;

  return (
    <div className="landing login-page" data-testid="login-page">
      <header className="top">
        <div className="wrap">
          <a
            className="brand"
            href="#top"
            onClick={(e) => {
              e.preventDefault();
              onBack();
            }}
          >
            Strix<small>Internal guide</small>
          </a>
          <nav aria-label="Navigation">
            <a
              href="#top"
              onClick={(e) => {
                e.preventDefault();
                onBack();
              }}
            >
              Back to the overview
            </a>
          </nav>
        </div>
      </header>

      <main id="top" className="login-main">
        <div className="login-card">
          <h1>Sign in to scan your project</h1>
          <p className="login-lede">
            Sign in to open the scanner. Your account is checked by the viewer running on this
            machine, and your password is never stored in the page.
          </p>

          {notice && (
            <p className="login-notice" role="status" data-testid="login-notice">
              {notice}
            </p>
          )}

          {(problem || locked) && (
            <p className="login-problem" role="alert" id={ids.problem} data-testid="login-error">
              {locked
                ? `Too many failed attempts. Try again in ${lockedFor} second${lockedFor === 1 ? "" : "s"}.`
                : problem?.message}
            </p>
          )}

          <form onSubmit={(e) => void submit(e)} noValidate data-testid="login-form">
            <div className="login-field">
              <label htmlFor={ids.username}>Username</label>
              <input
                ref={userInput}
                id={ids.username}
                data-testid="login-username"
                type="text"
                name="username"
                autoComplete="username"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                value={username}
                disabled={busy}
                aria-invalid={problem?.kind === "invalid" ? true : undefined}
                aria-describedby={describedBy}
                onChange={(e) => setUsername(e.target.value)}
              />
            </div>

            <div className="login-field">
              <label htmlFor={ids.password}>Password</label>
              <div className="login-password">
                <input
                  ref={passwordInput}
                  id={ids.password}
                  data-testid="login-password"
                  type={reveal ? "text" : "password"}
                  name="password"
                  autoComplete="current-password"
                  value={password}
                  disabled={busy}
                  aria-invalid={problem?.kind === "invalid" ? true : undefined}
                  aria-describedby={describedBy}
                  onChange={(e) => setPassword(e.target.value)}
                />
                <button
                  type="button"
                  className="login-reveal"
                  aria-pressed={reveal}
                  onClick={() => setReveal((v) => !v)}
                >
                  {reveal ? "Hide" : "Show"}
                  <span className="sr-only"> password</span>
                </button>
              </div>
            </div>

            <button
              type="submit"
              className="btn login-submit"
              data-testid="login-submit"
              disabled={!canSubmit}
            >
              {busy ? "Signing in…" : locked ? `Wait ${lockedFor}s` : "Sign in"}
            </button>
          </form>

          <p className="login-foot">
            <button type="button" className="login-back" onClick={onBack}>
              ← Back to the overview
            </button>
          </p>
        </div>
      </main>
    </div>
  );
}

export default LoginPage;
