import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { Penguin } from "./Penguin";
import { Showcase } from "./Showcase";
import { makeGroundTextures } from "./pixel";
import { useDeck } from "./useDeck";
import "./lp.base.css";
import "./lp.css";

/**
 * The public landing page, shown before sign-in. It has one job: get a new user to
 * their first scan. It is a short deck of full-height slides (scroll, arrow keys or
 * the dots to move) that says what Strix does, introduces the agent team, shows a real
 * scan and the overview video, and states plainly what stays on this machine and what
 * does not.
 *
 * Everything on this page is static and public; nothing here touches the API. Every
 * rule in lp.css is scoped under .lp so none can leak into the dashboard or the
 * sign-in pages (which keep landing.css).
 *
 * Copy is checked against the repository, not against older pages:
 *  - telemetry (PostHog, Scarf) is on by default and is disclosed with its opt-out;
 *  - the viewer listens on 127.0.0.1 by default and sign-in is the access control.
 */

interface LandingPageProps {
  /** The user chose to scan a project: continue to sign-in (or the app, if already signed in). */
  onScan: () => void;
}

const PAGE_TITLE = "Strix: local AI pentesting for your own apps";

const CHAPTERS = [
  { label: "Scan targets", at: 9, time: "0:09" },
  { label: "AI agents", at: 22, time: "0:22" },
  { label: "Findings", at: 37, time: "0:37" },
  { label: "Local viewer", at: 52, time: "0:52" },
  { label: "Recap", at: 67, time: "1:07" },
] as const;

const VIDEO_SRC = "./strix-local-intro.mp4";
const POSTER_SRC = "./strix-landing-poster.jpg";

/** Entrance-animation order for an element inside a slide. */
const d = (n: number): CSSProperties => ({ "--i": n }) as CSSProperties;
const pad = (n: number) => (n < 10 ? `0${n}` : String(n));

export function LandingPage({ onScan }: LandingPageProps) {
  const { rootRef, slides, active, seen, go } = useDeck();

  // The landing page has its own title; the dashboard's is restored on the way out.
  useEffect(() => {
    const previous = document.title;
    document.title = PAGE_TITLE;
    return () => {
      document.title = previous;
    };
  }, []);

  // Pixel ground tiles are drawn once into CSS variables on the page root.
  useEffect(() => {
    const root = rootRef.current;
    const textures = makeGroundTextures();
    if (!root || !textures) return;
    root.style.setProperty("--t-ground", textures.ground);
    root.style.setProperty("--t-grass-side", textures.grassSide);
    root.style.setProperty("--t-stone", textures.stone);
  }, [rootRef]);

  // A slide's number is its position on the page: each call takes the next one, in the
  // order the sections are written below. Moving a section therefore needs no renumbering.
  let slideNumber = 0;
  const slideClass = (extra = "") => {
    const index = slideNumber++;
    return ["slide", extra, active === index ? "active" : "", seen.has(index) ? "seen" : ""]
      .filter(Boolean)
      .join(" ");
  };

  const total = slides.length || 7;
  // Until the slides are measured, name the first one (the page opens on it).
  const crumb = slides[active]?.name ?? "Introduction";

  return (
    <div className="lp" ref={rootRef} data-testid="landing-page">
      <header className="topbar">
        <div className="wrap in">
          <button type="button" className="brand" onClick={() => go(0)} aria-label="Strix, back to the start">
            <span className="glyph" aria-hidden="true">
              <i />
            </span>
            Strix
          </button>
          <span className="crumb">{crumb}</span>
          <div className="tb-right">
            <button type="button" className="btn btn-primary btn-sm" onClick={onScan}>
              Scan your project
            </button>
            <button
              type="button"
              className="icon-btn"
              aria-label="Toggle full screen (F)"
              title="Full screen (F)"
              onClick={() => {
                try {
                  if (!document.fullscreenElement) void document.documentElement.requestFullscreen();
                  else void document.exitFullscreen();
                } catch {
                  /* full screen is optional */
                }
              }}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" />
              </svg>
            </button>
          </div>
        </div>
      </header>

      <nav className="dots" aria-label="Slides">
        {slides.map((slide, index) => (
          <button
            key={slide.id}
            type="button"
            aria-label={`${index + 1}. ${slide.name}`}
            title={slide.name}
            aria-current={active === index ? "true" : undefined}
            onClick={() => go(index)}
          />
        ))}
      </nav>

      <main>
        {/* ================= 1. INTRO ================= */}
        <section className={slideClass("s-title")} id="s1" data-slide data-name="Introduction">
          <div className="wrap">
            <span className="eyebrow rv">Local AI pentesting · runs on your machine</span>
            <h1 className="title rv" style={d(1)}>
              Find the security bugs in your web app <span className="grad-text">before attackers do.</span>
            </h1>
            <p className="lead rv" style={d(2)}>
              Strix is an AI pentester. Give it the address of your app and a team of AI agents tries to break in,
              proves every problem with a working exploit, and shows you the results right here.
            </p>
            <div className="start-hint rv" style={d(3)}>
              <button type="button" className="btn btn-primary" data-testid="landing-scan-cta" onClick={onScan}>
                Scan your project
              </button>
              <button type="button" className="btn btn-ghost" onClick={() => go(active + 1)}>
                Take the tour
              </button>
            </div>
            <p className="kbd-hint rv" style={d(4)}>
              or press <kbd>→</kbd> / <kbd>Space</kbd>
            </p>
          </div>
          <div className="world" aria-hidden="true">
            <div className="cloud" style={{ width: 110, top: "10%", animationDelay: "-12s" }} />
            <div className="cloud" style={{ width: 80, top: "28%", animationDelay: "-40s", animationDuration: "80s" }} />

            <div className="actor" style={{ left: "6%" }}>
              <Penguin />
            </div>
            <div className="bubble talk first" style={{ left: "5%", bottom: 200, animationDelay: "0s" }}>
              Surface mapped <span className="ok">✓</span>
            </div>

            <div className="actor" style={{ left: "20%" }}>
              <Penguin motion="hop" />
            </div>
            <div className="rock" style={{ left: "calc(20% + 104px)" }} />
            <div className="bubble talk" style={{ left: "19%", bottom: 200, animationDelay: "4s" }}>
              Trying injection
            </div>

            <div className="walker hide-sm hide-short">
              <div className="flip">
                <Penguin motion="waddle" style={{ width: 92 }} />
              </div>
            </div>
            <div className="bubble talk hide-sm hide-short" style={{ left: "42%", bottom: 200, animationDelay: "8s" }}>
              Exploit <span className="ok">VALIDATED</span>
            </div>

            <div className="actor hide-sm" style={{ right: "22%" }}>
              <Penguin />
            </div>
            <div className="actor" style={{ right: "6%" }}>
              <Penguin />
            </div>
            <div className="bubble talk" style={{ right: "4%", bottom: 200, animationDelay: "12s" }}>
              Sandbox <span className="ok">ON</span>
            </div>

            <div className="ground" />
          </div>
        </section>

        {/* ================= 2. AGENTS ================= */}
        <section className={slideClass()} id="s2" data-slide data-name="The agents">
          <div className="wrap">
            <span className="eyebrow rv">The agent team</span>
            <h2 className="title rv" style={d(1)}>
              A root agent and its specialists.
            </h2>
            <p className="lead rv" style={d(2)}>
              The root agent maps the attack surface and starts focused sub-agents: a recon pass, a CVE hunter, an
              injection prober. Each keeps its own transcript you can open and read.
            </p>
            <div className="card relay-card rv" style={d(3)}>
              <div className="relay-scroll">
                <div
                  className="relay"
                  role="img"
                  aria-label="Animated flow from the root agent through recon, CVE hunter and injection prober to validation and the report"
                >
                  <div className="belt" aria-hidden="true" />
                  <div className="parcel-track" aria-hidden="true">
                    <div className="parcel" />
                  </div>
                  <div className="stations">
                    {(
                      [
                        ["Root agent", "Attack map", "0.4s"],
                        ["Recon", "Routes & inputs", "2.4s"],
                        ["CVE hunter", "CVE leads", "4.4s"],
                        ["Injection prober", "Candidate bugs", "6.4s"],
                        ["Validate", "Working PoC", "8.4s"],
                        ["Report", "SARIF & HTML", "10.4s"],
                      ] as const
                    ).map(([name, artifact, delay]) => (
                      <div className="station" key={name} style={{ "--d": delay } as CSSProperties}>
                        <Penguin />
                        <b>{name}</b>
                        <span className="art">{artifact}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
            <div className="principles">
              <div className="card pr rv" style={d(4)}>
                <b>Proof over patterns</b>
                <span>A finding is reported with the exploit that worked.</span>
              </div>
              <div className="card pr rv" style={d(5)}>
                <b>Every agent leaves a transcript</b>
                <span>Open any agent in the viewer to read its reasoning.</span>
              </div>
              <div className="card pr rv" style={d(6)}>
                <b>Specialists work in parallel</b>
                <span>Sub-agents cover different parts of the target at once.</span>
              </div>
              <div className="card pr rv" style={d(7)}>
                <b>You set the limits</b>
                <span>
                  Scan modes and <code>--max-budget-usd</code> cap time and spend.
                </span>
              </div>
            </div>
          </div>
        </section>

        {/* ================= 3. WHAT YOU GET ================= */}
        <section className={slideClass()} id="s3" data-slide data-name="What you get">
          <div className="wrap">
            <span className="eyebrow v rv">What you get</span>
            <h2 className="title rv" style={d(1)}>
              A pentest team, not a scanner.
            </h2>
            <p className="lead rv" style={d(2)}>
              Strix proves what’s real, keeps the work on your machine, and fits the way you already ship.
            </p>
            <div className="fw-grid">
              <article className="card fw rv" style={d(2)}>
                <div className="av bg-green">
                  <Penguin />
                </div>
                <div>
                  <h3>Every finding is proven</h3>
                  <p>Agents build a working exploit and save the request and response, so you triage facts instead of guesses.</p>
                </div>
              </article>
              <article className="card fw rv" style={d(3)}>
                <div className="av bg-red">
                  <Penguin />
                </div>
                <div>
                  <h3>Runs in a local sandbox</h3>
                  <p>The agents attack from a Docker container on your machine. No vendor cloud runs the scan.</p>
                </div>
              </article>
              <article className="card fw rv" style={d(4)}>
                <div className="av bg-violet">
                  <Penguin />
                </div>
                <div>
                  <h3>The viewer explains itself</h3>
                  <p>A live agent graph, validated vulnerabilities, run history and reports, all in the browser.</p>
                </div>
              </article>
              <article className="card fw rv" style={d(5)}>
                <div className="av bg-blue">
                  <Penguin />
                </div>
                <div>
                  <h3>Drops into your pipeline</h3>
                  <p>
                    <code>-n</code> runs headless and exits <code>2</code> when it finds vulnerabilities. <code>--fail-on</code>{" "}
                    sets the severity that fails the build.
                  </p>
                </div>
              </article>
              <article className="card fw wide rv" style={d(6)}>
                <div className="av bg-amber">
                  <Penguin />
                </div>
                <div>
                  <h3>Reports you can hand off</h3>
                  <p>
                    Each run writes Markdown, HTML, per-finding notes, <code>vulnerabilities.json</code> and{" "}
                    <code>findings.sarif</code> (SARIF 2.1.0) into <code>strix_runs/</code>, so results drop straight into the
                    tools your team already uses.
                  </p>
                </div>
              </article>
            </div>
          </div>
        </section>

        {/* ================= 4. WHAT A SCAN LOOKS LIKE ================= */}
        <section className={slideClass()} id="s4" data-slide data-name="What a scan looks like">
          <Showcase />
        </section>

        {/* ================= 5. VIDEO ================= */}
        <section className={slideClass()} id="s5" data-slide data-name="Overview video">
          <VideoStage />
        </section>

        {/* ================= 6. DATA & SAFETY ================= */}
        <section className={slideClass("safety")} id="s6" data-slide data-name="Data and safety">
          <div className="wrap">
            <span className="eyebrow r rv">Data and safety</span>
            <h2 className="title rv" style={d(1)}>
              Know where your data goes.
            </h2>
            <p className="lead rv" style={d(2)}>
              Strix runs on your machine, but it isn’t air-gapped. Here is what stays, what leaves, and what to be careful
              with.
            </p>
            <div className="versus mt">
              <div className="card vs good rv" style={d(2)}>
                <h3>
                  <span className="tag">stays local</span>On your machine
                </h3>
                <ul className="dl">
                  <li>The sandbox, where the agents run and attack</li>
                  <li>
                    Every finding, log and report in <code>strix_runs/</code>
                  </li>
                  <li>
                    This viewer, which listens on <code>127.0.0.1</code> by default and asks you to sign in
                  </li>
                </ul>
                <div className="who" aria-hidden="true">
                  <Penguin />
                </div>
              </div>
              <div className="card vs bad rv" style={d(3)}>
                <h3>
                  <span className="tag">leaves</span>Goes off the machine
                </h3>
                <ul className="dl">
                  <li>
                    Agent prompts, including the code and responses they analyse, go to your model provider. Point{" "}
                    <code>LLM_API_BASE</code> at an approved internal endpoint or a local model such as Ollama if the code
                    is sensitive.
                  </li>
                  <li>
                    Anonymous usage counts go to PostHog and Scarf by default: versions, model name, scan mode, finding
                    counts, token and cost totals, and that the viewer was opened. Set <code>STRIX_TELEMETRY=0</code> to
                    turn it off.
                  </li>
                </ul>
                <div className="who" aria-hidden="true">
                  <Penguin />
                </div>
              </div>
            </div>
            <div className="card note rv" style={d(4)}>
              <Penguin />
              <p>
                <b>Test only what you own.</b> Strix runs real exploits, so prefer a staging environment and a dedicated test
                account over production data and real credentials.
              </p>
            </div>
          </div>
        </section>

        {/* ================= 7. NEXT STEP ================= */}
        <section className={slideClass("s-close")} id="s7" data-slide data-name="Next step">
          <div className="wrap">
            <div className="lineup rv" aria-hidden="true">
              <Penguin motion="wiggle" />
              <Penguin />
              <Penguin motion="wiggle" />
              <Penguin />
              <Penguin motion="wiggle" />
              <Penguin />
              <Penguin motion="wiggle" />
            </div>
            <span className="eyebrow v rv" style={d(1)}>
              Next step
            </span>
            <h2 className="title rv" style={d(2)}>
              Point it at your app. See what it can prove.
            </h2>
            <p className="lead rv" style={d(3)}>
              Sign in, enter an address, and start a Quick scan. Findings are written to your disk, in{" "}
              <code>strix_runs/</code>.
            </p>
            <div className="cta-row rv" style={d(4)}>
              <button type="button" className="btn btn-primary" onClick={onScan}>
                Scan your project
              </button>
              <button type="button" className="btn btn-ghost" onClick={() => go(0)}>
                Back to start
              </button>
            </div>
            <p className="sign rv" style={d(5)}>
              Internal fork of{" "}
              <a href="https://github.com/usestrix/strix" rel="noopener noreferrer">
                usestrix/strix
              </a>
              , licensed under Apache 2.0. ·{" "}
              <a href="https://github.com/vankhangfet/strix" rel="noopener noreferrer">
                Source repository
              </a>{" "}
              ·{" "}
              <a href="https://docs.strix.ai" rel="noopener noreferrer">
                Upstream documentation
              </a>
            </p>
          </div>
        </section>
      </main>

      <footer className="bottombar">
        <div className="progress">
          <i style={{ width: `${((active + 1) / total) * 100}%` }} />
        </div>
        <div className="wrap in">
          <span className="counter">
            <b>{pad(active + 1)}</b> / {pad(total)}
          </span>
          <span className="hint">
            Navigate with <kbd>←</kbd> <kbd>→</kbd> · <kbd>F</kbd> full screen
          </span>
          <div className="bb-right">
            <button type="button" className="icon-btn" aria-label="Previous slide" onClick={() => go(active - 1)} disabled={active === 0}>
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M15 6l-6 6 6 6" />
              </svg>
            </button>
            <button
              type="button"
              className="icon-btn"
              aria-label="Next slide"
              onClick={() => go(active + 1)}
              disabled={active === total - 1}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M9 6l6 6-6 6" />
              </svg>
            </button>
          </div>
        </div>
      </footer>
    </div>
  );
}

/** The overview video with chapter buttons that seek and follow playback. */
function VideoStage() {
  const video = useRef<HTMLVideoElement>(null);
  const [current, setCurrent] = useState(-1);

  const syncChapter = useCallback(() => {
    const time = video.current?.currentTime ?? 0;
    let found = -1;
    CHAPTERS.forEach((chapter, index) => {
      if (time >= chapter.at) found = index;
    });
    setCurrent(found);
  }, []);

  const jump = (seconds: number) => {
    const el = video.current;
    if (!el) return;
    el.currentTime = seconds;
    // Autoplay can be refused by the browser; the user can still press play.
    void el.play().catch(() => undefined);
    syncChapter();
  };

  // Pause when the slide scrolls out of view so audio never plays from a hidden slide.
  useEffect(() => {
    const el = video.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) if (!entry.isIntersecting && !el.paused) el.pause();
      },
      { threshold: 0.2 }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return (
    <div className="wrap split">
      <div>
        <span className="eyebrow rv">Overview</span>
        <h2 className="title rv" style={d(1)}>
          See it in 79 seconds.
        </h2>
        <p className="lead rv" style={d(2)}>
          A short produced tour of what Strix does. It plays right here on the page.
        </p>
        <ul className="chapters rv" style={d(3)} aria-label="Jump to a part of the video">
          {CHAPTERS.map((chapter, index) => (
            <li key={chapter.label}>
              <button type="button" aria-current={current === index ? "true" : undefined} onClick={() => jump(chapter.at)}>
                {chapter.label}
                <time>{chapter.time}</time>
              </button>
            </li>
          ))}
        </ul>
      </div>
      <div className="card video-card rv" style={d(3)}>
        <video
          ref={video}
          controls
          preload="metadata"
          playsInline
          poster={POSTER_SRC}
          onTimeUpdate={syncChapter}
          onSeeked={syncChapter}
          data-testid="landing-video"
        >
          <source src={VIDEO_SRC} type="video/mp4" />
          Your browser can’t play this video.
        </video>
      </div>
    </div>
  );
}

export default LandingPage;
