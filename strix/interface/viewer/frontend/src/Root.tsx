import { useCallback, useEffect, useRef, useState } from "react";
import App, { type View } from "@/App";
import { LandingPage } from "@/landing/LandingPage";
import { LoginPage } from "@/landing/LoginPage";
import { fetchLoginStatus, signOut } from "@/data/loginSource";

/**
 * Decides what the browser sees:
 *
 *   landing  ──"Scan your project"──▶  login  ──signed in──▶  app (New scan)
 *      ▲                                                          │
 *      └──────────────────────── Sign out ◀───────────────────────┘
 *
 * The server is the authority. This component only reads `/api/login/status` to
 * choose a stage; it never decides who is allowed in, so tampering with it in
 * the browser cannot unlock anything: every data request is checked again by
 * the server.
 *
 * - A browser that is already signed in (e.g. after a page refresh) goes straight
 *   to the app rather than back to the landing page.
 * - While in the app the status is re-checked, so an expired session returns to
 *   the sign-in page with a notice instead of leaving a dashboard of failures.
 * - An unreadable status (network blip) is "unknown", never "signed out".
 */

type Stage = "checking" | "landing" | "login" | "app";

/** How often the app re-checks that the session is still valid. */
const SESSION_CHECK_MS = 30_000;

export default function Root() {
  const [stage, setStage] = useState<Stage>("checking");
  const [username, setUsername] = useState<string | null>(null);
  const [loginRequired, setLoginRequired] = useState(false);
  const [entryView, setEntryView] = useState<View | undefined>(undefined);
  const [notice, setNotice] = useState<string | null>(null);
  // Bumped to remount the app (and drop all its state) after signing in or out.
  const [appKey, setAppKey] = useState(0);

  // First load: a browser that is already signed in skips the landing page.
  useEffect(() => {
    const controller = new AbortController();
    void fetchLoginStatus(controller.signal).then((status) => {
      if (controller.signal.aborted) return;
      if (status?.authenticated) {
        setUsername(status.username);
        setLoginRequired(status.loginRequired);
        setStage("app");
      } else {
        setLoginRequired(status?.loginRequired ?? false);
        setStage("landing");
      }
    });
    return () => controller.abort();
  }, []);

  const enterApp = useCallback((name: string | null, view: View | undefined) => {
    setUsername(name);
    setEntryView(view);
    setNotice(null);
    setAppKey((k) => k + 1);
    setStage("app");
  }, []);

  // "Scan your project": go to sign-in, or straight to the scanner if already allowed in.
  const handleScan = useCallback(async () => {
    const status = await fetchLoginStatus();
    if (status?.authenticated) {
      setLoginRequired(status.loginRequired);
      enterApp(status.username, "launch");
    } else if (status && !status.loginRequired) {
      enterApp(null, "launch");
    } else {
      // Sign-in needed, or the status could not be read. Show the form; if the
      // viewer is really down the form says so on submit.
      setLoginRequired(true);
      setStage("login");
    }
  }, [enterApp]);

  const handleSignedIn = useCallback(
    (name: string) => {
      setLoginRequired(true);
      enterApp(name, "launch");
    },
    [enterApp]
  );

  const handleSignOut = useCallback(async () => {
    await signOut();
    setUsername(null);
    setEntryView(undefined);
    setNotice(null);
    setStage("landing");
  }, []);

  // While in the app, notice an expired session and return to sign-in.
  const stageRef = useRef(stage);
  stageRef.current = stage;
  useEffect(() => {
    if (stage !== "app" || !loginRequired) return;
    let cancelled = false;

    const check = async () => {
      const status = await fetchLoginStatus();
      if (cancelled || stageRef.current !== "app" || status === null) return; // unknown: stay put
      if (status.loginRequired && !status.authenticated) {
        setUsername(null);
        setNotice("Your session ended. Please sign in again.");
        setStage("login");
      }
    };

    const timer = setInterval(() => void check(), SESSION_CHECK_MS);
    const onVisible = () => {
      if (!document.hidden) void check();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [stage, loginRequired]);

  if (stage === "checking") {
    return <div className="min-h-screen bg-black" role="status" aria-label="Loading" />;
  }
  if (stage === "landing") {
    return <LandingPage onScan={() => void handleScan()} />;
  }
  if (stage === "login") {
    return (
      <LoginPage
        notice={notice}
        onSignedIn={handleSignedIn}
        onBack={() => {
          setNotice(null);
          setStage("landing");
        }}
      />
    );
  }
  return (
    <App
      key={appKey}
      entryView={entryView}
      account={
        loginRequired && username ? { username, onSignOut: () => void handleSignOut() } : undefined
      }
    />
  );
}
