import { useEffect, useId, useRef, useState } from "react";
import { changePassword } from "@/data/loginSource";
import "./landing.css";
import "./login.css";

/**
 * Forced password change, shown right after the first sign-in with the shipped
 * default account. The viewer will not reveal any data until the default is
 * replaced, so this stands between sign-in and the dashboard.
 *
 * The server does all the checking (current password correct, new password long
 * enough and not the default). This form only collects the fields and reports
 * what happened; nothing is kept after the request returns.
 */

/** The shortest new password the server accepts; mirrored here for a live hint. */
const MIN_PASSWORD_CHARS = 8;

interface ChangePasswordPageProps {
  /** The signed-in username, shown for context. */
  username: string | null;
  /** Prefill the current-password field (the default is well known on first run). */
  suggestedCurrent?: string;
  /** The new password is set: continue to the app. */
  onChanged: () => void;
  /** End the session and return to the landing page. */
  onCancel: () => void;
}

type Problem = { message: string } | null;

export function ChangePasswordPage({
  username,
  suggestedCurrent = "",
  onChanged,
  onCancel,
}: ChangePasswordPageProps) {
  const ids = {
    current: useId(),
    next: useId(),
    confirm: useId(),
    problem: useId(),
  };
  const [current, setCurrent] = useState(suggestedCurrent);
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [reveal, setReveal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<Problem>(null);
  const [lockedFor, setLockedFor] = useState(0);
  const firstInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const previous = document.title;
    document.title = "Set a new password · Strix";
    firstInput.current?.focus();
    return () => {
      document.title = previous;
    };
  }, []);

  useEffect(() => {
    if (lockedFor <= 0) return;
    const id = setInterval(() => setLockedFor((n) => Math.max(0, n - 1)), 1000);
    return () => clearInterval(id);
  }, [lockedFor > 0]); // eslint-disable-line react-hooks/exhaustive-deps

  const locked = lockedFor > 0;
  const mismatch = confirm !== "" && next !== confirm;
  const tooShort = next !== "" && next.length < MIN_PASSWORD_CHARS;
  const canSubmit =
    !busy &&
    !locked &&
    current !== "" &&
    next.length >= MIN_PASSWORD_CHARS &&
    next === confirm;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setProblem(null);
    const outcome = await changePassword(current, next);
    setBusy(false);

    if (outcome.kind === "ok") {
      setCurrent("");
      setNext("");
      setConfirm("");
      onChanged();
      return;
    }
    // Never keep rejected secrets around.
    setNext("");
    setConfirm("");
    if (outcome.kind === "locked") {
      setLockedFor(outcome.retryAfterSeconds);
    } else if (outcome.kind === "wrong-current") {
      setCurrent("");
      setProblem({ message: outcome.message });
    } else {
      setProblem({ message: outcome.message });
    }
  };

  const describedBy = problem || locked ? ids.problem : undefined;

  return (
    <div className="landing login-page" data-testid="change-password-page">
      <header className="top">
        <div className="wrap">
          <a className="brand" href="#top" onClick={(e) => e.preventDefault()}>
            Strix<small>Internal guide</small>
          </a>
          <nav aria-label="Navigation">
            <button type="button" className="login-back" onClick={onCancel}>
              Sign out
            </button>
          </nav>
        </div>
      </header>

      <main id="top" className="login-main">
        <div className="login-card">
          <h1>Set a new password</h1>
          <p className="login-lede">
            {username ? (
              <>
                You are signed in as <strong>{username}</strong> with the default password. Choose a
                new one to continue — this is required once, and the viewer stays locked until you
                do.
              </>
            ) : (
              <>
                Choose a new password to continue. This is required once, and the viewer stays
                locked until you do.
              </>
            )}
          </p>

          {(problem || locked) && (
            <p className="login-problem" role="alert" id={ids.problem} data-testid="change-error">
              {locked
                ? `Too many attempts. Try again in ${lockedFor} second${lockedFor === 1 ? "" : "s"}.`
                : problem?.message}
            </p>
          )}

          <form onSubmit={(e) => void submit(e)} noValidate data-testid="change-form">
            <div className="login-field">
              <label htmlFor={ids.current}>Current password</label>
              <input
                ref={firstInput}
                id={ids.current}
                data-testid="change-current"
                type={reveal ? "text" : "password"}
                autoComplete="current-password"
                value={current}
                disabled={busy}
                aria-describedby={describedBy}
                onChange={(e) => setCurrent(e.target.value)}
              />
            </div>

            <div className="login-field">
              <label htmlFor={ids.next}>New password</label>
              <div className="login-password">
                <input
                  id={ids.next}
                  data-testid="change-new"
                  type={reveal ? "text" : "password"}
                  autoComplete="new-password"
                  value={next}
                  disabled={busy}
                  aria-invalid={tooShort ? true : undefined}
                  onChange={(e) => setNext(e.target.value)}
                />
                <button
                  type="button"
                  className="login-reveal"
                  aria-pressed={reveal}
                  onClick={() => setReveal((v) => !v)}
                >
                  {reveal ? "Hide" : "Show"}
                  <span className="sr-only"> passwords</span>
                </button>
              </div>
              <small className="login-hint">At least {MIN_PASSWORD_CHARS} characters.</small>
            </div>

            <div className="login-field">
              <label htmlFor={ids.confirm}>Confirm new password</label>
              <input
                id={ids.confirm}
                data-testid="change-confirm"
                type={reveal ? "text" : "password"}
                autoComplete="new-password"
                value={confirm}
                disabled={busy}
                aria-invalid={mismatch ? true : undefined}
                onChange={(e) => setConfirm(e.target.value)}
              />
              {mismatch && (
                <small className="login-hint login-hint-error" data-testid="change-mismatch">
                  The two passwords do not match.
                </small>
              )}
            </div>

            <button
              type="submit"
              className="btn login-submit"
              data-testid="change-submit"
              disabled={!canSubmit}
            >
              {busy ? "Saving…" : locked ? `Wait ${lockedFor}s` : "Set password & continue"}
            </button>
          </form>

          <p className="login-foot">
            <button type="button" className="login-back" onClick={onCancel}>
              Sign out instead
            </button>
          </p>
        </div>
      </main>
    </div>
  );
}

export default ChangePasswordPage;
